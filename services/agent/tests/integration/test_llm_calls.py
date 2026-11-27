"""``llm_calls`` against Postgres (ADR-023 Part 4): append-only for the runtime role, written
whatever the turn lease says, and constrained so a row can't claim a cost it can't have.
"""

import uuid
from collections.abc import Iterator
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal

import pytest
from conftest import run_alembic
from sqlalchemy import Connection, Engine, delete, insert, select, text
from sqlalchemy.exc import IntegrityError, ProgrammingError
from sqlalchemy.ext.asyncio import AsyncEngine
from worker_world import World

from agent.core.lifecycle import RunStatus
from agent.db.llm_calls import DbCallRecorder
from agent.db.schema import llm_calls
from agent.llm.recording import CallRecord

TIMEOUTS = {"statement_timeout": timedelta(seconds=5), "lock_timeout": timedelta(seconds=2)}


def record(**changes: object) -> CallRecord:
    base = CallRecord(
        conversation_id=None,
        run_id=uuid.uuid4(),
        turn_id=uuid.uuid4(),
        step=1,
        purpose="turn",
        provider="groq",
        model="test-model",
        attempts=1,
        outcome="ok",
        input_tokens=100,
        output_tokens=20,
        usage_completeness="full",
        prompt_chars=400,
        output_chars=80,
        first_token_ms=120,
        latency_ms=900,
        cost_usd=Decimal("0.00012345"),
        price_version="v1",
    )
    return replace(base, **changes)  # type: ignore[arg-type]


@pytest.fixture
def cleanup(owner_engine: Engine) -> Iterator[list[uuid.UUID]]:
    runs: list[uuid.UUID] = []
    yield runs
    with owner_engine.begin() as owner:
        owner.execute(delete(llm_calls).where(llm_calls.c.run_id.in_(runs)))


def rows(owner_engine: Engine, run: uuid.UUID) -> list[dict[str, object]]:
    with owner_engine.connect() as owner:
        result = owner.execute(select(llm_calls).where(llm_calls.c.run_id == run))
        return [dict(row._mapping) for row in result]


async def test_the_recorder_writes_one_row_per_record(
    async_runtime_engine: AsyncEngine, owner_engine: Engine, cleanup: list[uuid.UUID]
) -> None:
    call = record()
    cleanup.append(call.run_id)  # type: ignore[arg-type]

    await DbCallRecorder(async_runtime_engine, **TIMEOUTS)(call)

    (row,) = rows(owner_engine, call.run_id)  # type: ignore[arg-type]
    assert row["cost_usd"] == Decimal("0.00012345") and row["attempts"] == 1


async def test_a_lost_turns_call_is_still_recorded(
    world: World, async_runtime_engine: AsyncEngine, owner_engine: Engine, cleanup: list[uuid.UUID]
) -> None:
    run = world.add_run(RunStatus.AGENT_STEP)
    stale_turn = world.hold_turn(run)
    world.hold_turn(run)  # another turn takes the conversation over
    cleanup.append(run)

    await DbCallRecorder(async_runtime_engine, **TIMEOUTS)(record(run_id=run, turn_id=stale_turn))

    assert len(rows(owner_engine, run)) == 1


@pytest.mark.parametrize(
    "statement",
    ["UPDATE llm_calls SET outcome = 'ok'", "DELETE FROM llm_calls", "TRUNCATE llm_calls"],
)
def test_the_runtime_role_cannot_change_call_records(runtime: Connection, statement: str) -> None:
    runtime.execute(insert(llm_calls).values(**record().__dict__))

    with pytest.raises(ProgrammingError, match="permission denied"):
        runtime.execute(text(statement))


# Each case breaks exactly one constraint, so the reported name is unambiguous.
@pytest.mark.parametrize(
    ("changes", "constraint"),
    [
        ({"usage_completeness": "none"}, "tokens_iff_counted"),
        (
            {
                "input_tokens": None,
                "output_tokens": None,
                "usage_completeness": "full",
                "cost_usd": None,
                "price_version": None,
            },
            "tokens_iff_counted",
        ),
        ({"price_version": None}, "cost_has_version"),
        (
            {
                "input_tokens": None,
                "output_tokens": None,
                "usage_completeness": "none",
                "cost_usd": Decimal("0.1"),
            },
            "cost_needs_tokens",
        ),
        ({"outcome": "weird"}, "known_outcome"),
        ({"purpose": "chat"}, "known_purpose"),
        ({"attempts": 0}, "attempts_positive"),
        ({"cost_usd": Decimal("-1")}, "non_negative"),
    ],
)
def test_a_row_cant_claim_what_it_cant_have(
    runtime: Connection, changes: dict[str, object], constraint: str
) -> None:
    with pytest.raises(IntegrityError) as failure:
        runtime.execute(insert(llm_calls).values(**record(**changes).__dict__))

    assert failure.value.orig is not None
    assert failure.value.orig.diag.constraint_name == f"ck_llm_calls_{constraint}"  # type: ignore[attr-defined]


def test_an_uncounted_failed_call_is_a_valid_row(runtime: Connection) -> None:
    failed = record(
        outcome="provider_timeout",
        attempts=3,
        input_tokens=None,
        output_tokens=None,
        usage_completeness="none",
        cost_usd=None,
        price_version=None,
        first_token_ms=None,
    )

    runtime.execute(insert(llm_calls).values(**failed.__dict__))


def test_a_request_too_large_is_its_own_valid_outcome(runtime: Connection) -> None:
    """Migration 0007: a provider's 413 isn't recorded as a context that's too long."""
    refused = record(
        outcome="request_too_large",
        input_tokens=None,
        output_tokens=None,
        usage_completeness="none",
        cost_usd=None,
        price_version=None,
        first_token_ms=None,
    )

    runtime.execute(insert(llm_calls).values(**refused.__dict__))


def test_downgrading_0007_folds_request_too_large_into_context_too_long(
    owner_engine: Engine, cleanup: list[uuid.UUID]
) -> None:
    refused = record(
        outcome="request_too_large",
        input_tokens=None,
        output_tokens=None,
        usage_completeness="none",
        cost_usd=None,
        price_version=None,
        first_token_ms=None,
    )
    run = refused.run_id
    assert run is not None
    cleanup.append(run)
    with owner_engine.begin() as owner:
        owner.execute(insert(llm_calls).values(**refused.__dict__))

    try:
        down = run_alembic("downgrade", "0006")
        assert down.returncode == 0, down.stderr
        assert [r["outcome"] for r in rows(owner_engine, run)] == ["context_too_long"]
    finally:
        up = run_alembic("upgrade", "head")
        assert up.returncode == 0, up.stderr


def test_a_warrant_call_is_a_valid_purpose(runtime: Connection) -> None:
    """Migration 0008: the warrant check's calls are recorded like the others (ADR-029 W01)."""
    runtime.execute(insert(llm_calls).values(**record(purpose="warrant").__dict__))


def test_downgrading_0008_refuses_while_warrant_calls_are_recorded(
    owner_engine: Engine, cleanup: list[uuid.UUID]
) -> None:
    call = record(purpose="warrant")
    assert call.run_id is not None
    cleanup.append(call.run_id)
    with owner_engine.begin() as owner:
        owner.execute(insert(llm_calls).values(**call.__dict__))

    try:
        down = run_alembic("downgrade", "0007")
        assert down.returncode != 0
        assert "holds warrant calls" in down.stderr
        assert [r["purpose"] for r in rows(owner_engine, call.run_id)] == ["warrant"]
    finally:
        up = run_alembic("upgrade", "head")
        assert up.returncode == 0, up.stderr
