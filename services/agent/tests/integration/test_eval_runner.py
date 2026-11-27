"""The eval runner end to end (ADR-026 Part 1, S1-S5, its test table; ADR-028 S7): a cloned
database per case run, the real turn under the logical clock, recordings, the timestamp
invariant and the judges. The model is a scripted fake, so nothing leaves the machine; the
recording layer can't tell it from a provider."""

import asyncio
import os
import subprocess
import sys
import time
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
import yaml
from agent_testing.evals.cases import Case, load_suite
from agent_testing.evals.databases import EvalDatabases
from agent_testing.evals.invariant import timestamp_columns
from agent_testing.evals.judges import check_args
from agent_testing.evals.recording import Mode
from agent_testing.evals.runner import CaseRunResult, RunConfig, run_case
from agent_testing.scripted_llm import ScriptedLLM, Turn, tool_call, warrant_turn
from sqlalchemy import Connection, insert

from agent.db.schema import customers, llm_calls
from agent.llm.settings import LLMSettings
from agent.llm.types import ChatModel, Message, ModelResponse, OnText, Purpose, ToolSchema
from agent.turn.start import TurnIds

AGENT = Path(__file__).resolve().parents[2]
MODEL = "groq:openai/gpt-oss-120b"
ORDER = "ord_eval_1"
BOUND = 120  # seconds for any one case run
DELAY = 0.2  # the slow model's seconds per call
# The model is a fake, so record mode needs no real key; never depend on the developer's .env.
ENVIRON = {**os.environ, "GROQ_API_KEY": "fake-model-needs-no-key"}
DELIVERED = {
    "id": ORDER,
    "charged_minor": 4000,
    "currency": "USD",
    "status": "delivered",
    "delivered": "-3d",
}


@pytest.fixture(scope="module")
def databases() -> Iterator[EvalDatabases]:
    if not os.environ.get("AGENT_DB_OWNER_URL"):
        pytest.skip("the agent database settings aren't set")
    if not os.environ.get("POSTGRES_SUPERUSER"):
        pytest.fail("evals clone databases as the superuser: POSTGRES_SUPERUSER isn't set")
    found = EvalDatabases.from_env(os.environ)
    with found.hold():
        found.create_template(os.environ)
        yield found
        found.drop(found.template)
    assert found.clones() == [], "a case database was left behind"


def write_case(root: Path, case_id: str, **data: Any) -> Case:
    folder = root / "suite"
    folder.mkdir(parents=True, exist_ok=True)
    body = {
        "category": "smoke",
        "setup": {"customer": "cust_eval", "orders": [DELIVERED]},
        "messages": ["Where is my order ord_eval_1?"],
        "expect": [{"looked_up": {"order": ORDER}}],
        **data,
    }
    (folder / f"{case_id}.yaml").write_text(yaml.safe_dump(body), encoding="utf-8")
    (loaded,) = [c for c in load_suite(root, "suite") if c.id == case_id]
    check_args(loaded)
    return loaded


def lookup_then(final: Turn) -> ScriptedLLM:
    """Looks the order up when it hasn't yet, then answers with ``final``; summaries say so."""
    return ScriptedLLM(
        [
            Turn(text="A summary.", match=lambda v: v.purpose is Purpose.SUMMARY, repeat=True),
            Turn(
                tool_calls=(tool_call("lookup_order", {"order_id": ORDER}, "c1"),),
                match=lambda v: v.purpose is Purpose.TURN and not v.has_tool_results,
                repeat=True,
            ),
            replace(
                final, match=lambda v: v.purpose is Purpose.TURN and v.has_tool_results, repeat=True
            ),
            # The warrant model agrees; every message here names the order, so the quote is the
            # customer's own words (ADR-029 W01 checks only that, and the order).
            warrant_turn(ORDER, ORDER),
        ]
    )


REPLY = Turn(text="Your order ord_eval_1 was delivered three days ago.")
PROPOSE = Turn(
    tool_calls=(
        tool_call(
            "propose_refund", {"order_id": ORDER, "amount_minor": 2000, "currency": "USD"}, "c2"
        ),
    )
)


class Slow:
    """A provider that takes ``DELAY`` seconds per call."""

    def __init__(self, inner: ScriptedLLM) -> None:
        self.inner = inner

    async def complete(
        self,
        messages: Sequence[Message],
        tools: Sequence[ToolSchema],
        *,
        deadline: float,
        purpose: Purpose,
        on_text: OnText | None = None,
    ) -> ModelResponse:
        await asyncio.sleep(DELAY)
        return await self.inner.complete(
            messages, tools, deadline=deadline, purpose=purpose, on_text=on_text
        )


async def run(
    case: Case,
    databases: EvalDatabases,
    recordings: Path,
    mode: Mode,
    model: ChatModel | None = None,
    *,
    env_overrides: Mapping[str, str] | None = None,
    before_judging: Any = None,
) -> CaseRunResult:
    config = RunConfig(
        "suite", mode, MODEL, recordings=recordings, env_overrides=dict(env_overrides or {})
    )

    def inner(_: LLMSettings) -> ChatModel:
        assert model is not None, "this run must not build a model"
        return model

    async with asyncio.timeout(BOUND):
        return await run_case(
            case,
            1,
            config=config,
            environ=ENVIRON,
            databases=databases,
            recording_run="test",
            inner=inner,
            before_judging=before_judging,
        )


# --- record and replay ---------------------------------------------------------------------------


async def test_record_then_replay_gives_the_same_keys_verdicts_and_stamps(
    databases: EvalDatabases, tmp_path: Path
) -> None:
    case = write_case(
        tmp_path,
        "status",
        expect=[
            {"looked_up": {"order": ORDER}},
            {"reply_template": {"one_of": ["model"]}},
            {"no_refund_approved": {}},
        ],
    )
    fake = lookup_then(REPLY)

    recorded = await run(case, databases, tmp_path / "rec", Mode.RECORD, fake)
    replayed = await run(case, databases, tmp_path / "rec", Mode.REPLAY)

    assert recorded.error is None and recorded.passed, recorded
    assert len(fake.calls) == 2 and len(recorded.keys) == 2
    assert sorted(p.stem for p in (tmp_path / "rec").glob("*.json")) == sorted(recorded.keys)
    assert (replayed.keys, replayed.verdicts) == (recorded.keys, recorded.verdicts)
    assert replayed.stamps == recorded.stamps and recorded.stamps["runs.created_at"]


async def test_replay_without_a_recording_fails_the_run_naming_the_key(
    databases: EvalDatabases, tmp_path: Path
) -> None:
    case = write_case(tmp_path, "unrecorded")

    result = await run(case, databases, tmp_path / "empty", Mode.REPLAY)

    assert result.error is not None and result.error.startswith("MissingRecording: no recording")
    assert result.keys == [] or result.keys[-1] in result.error
    assert not result.passed


# --- isolation (ADR-026 Part 1) ------------------------------------------------------------------


async def test_a_case_runs_twice_with_the_same_ids_and_the_template_stays_empty(
    databases: EvalDatabases, tmp_path: Path
) -> None:
    case = write_case(
        tmp_path,
        "twice",
        setup={
            "customer": "cust_eval",
            "orders": [DELIVERED],
            "past_tickets": [{"at": "-20d", "status": "completed"}],
        },
    )

    first = await run(case, databases, tmp_path / "rec", Mode.RECORD, lookup_then(REPLY))
    second = await run(case, databases, tmp_path / "rec", Mode.REPLAY)

    assert first.error is None and second.error is None
    assert first.keys == second.keys  # same seeded ids, so the digest and prompts match
    assert template_rows(databases) == 0


async def test_negative_control_seeding_the_template_makes_the_second_run_collide(
    databases: EvalDatabases, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without a fresh copy per run, the case's fixed ids already exist the second time."""
    case = write_case(tmp_path, "shared")

    class Shared:
        def __enter__(self) -> str:
            return databases.template

        def __exit__(self, *_: object) -> None:
            return None

    monkeypatch.setattr(databases.__class__, "clone", lambda self: Shared())
    try:
        await run(case, databases, tmp_path / "rec", Mode.RECORD, lookup_then(REPLY))
        with pytest.raises(Exception, match="pk_customers"):
            await run(case, databases, tmp_path / "rec", Mode.REPLAY)
    finally:
        monkeypatch.undo()
        databases.create_template(os.environ)  # the polluted template is rebuilt


def template_rows(databases: EvalDatabases) -> int:
    import psycopg
    from agent_testing.evals.databases import with_database

    with psycopg.connect(with_database(databases.superuser_url, databases.template)) as conn:
        row = conn.execute(
            "SELECT (SELECT count(*) FROM runs) + (SELECT count(*) FROM customers)"
        ).fetchone()
    assert row is not None
    return int(row[0])


# --- the timestamp invariant (S2a) ---------------------------------------------------------------


async def test_a_stamp_from_a_column_default_fails_the_invariant(
    databases: EvalDatabases, tmp_path: Path
) -> None:
    """The run's own stamps pass (the record/replay test); one row that fell back to the
    database's now() lands on the real date and fails the case."""
    case = write_case(tmp_path, "leaky")

    def forgot_the_clock(conn: Connection, _: Sequence[TurnIds]) -> None:
        conn.execute(insert(customers).values(id="cust_forgot"))  # created_at = now()

    result = await run(
        case,
        databases,
        tmp_path / "rec",
        Mode.RECORD,
        lookup_then(REPLY),
        before_judging=forgot_the_clock,
    )

    assert result.error is not None
    assert result.error.startswith("timestamp invariant: customers.created_at = ")


# --- a multi-message case with a slow model ------------------------------------------------------


async def test_a_multi_message_case_recorded_slowly_replays_at_once_with_the_same_keys_and_stamps(
    databases: EvalDatabases, tmp_path: Path
) -> None:
    """Three messages with a window of two, so a background summary runs too. The logical
    clock makes every stamp a function of the case, not of how long the model took."""
    case = write_case(
        tmp_path,
        "chatty",
        messages=[
            "Where is ord_eval_1?",
            "Thanks. And when did it arrive?",
            "Great, thank you!",
        ],
    )
    settings = {"MEMORY_WINDOW_MESSAGES": "2", "SUMMARY_TRIGGER_MESSAGES": "2"}
    slow = Slow(lookup_then(REPLY))

    started = time.monotonic()
    recorded = await run(
        case, databases, tmp_path / "rec", Mode.RECORD, slow, env_overrides=settings
    )
    recording_took = time.monotonic() - started
    started = time.monotonic()
    replayed = await run(case, databases, tmp_path / "rec", Mode.REPLAY, env_overrides=settings)
    replay_took = time.monotonic() - started

    calls = len(slow.inner.calls)
    assert recorded.error is None and replayed.error is None, (recorded.error, replayed.error)
    assert any(c.purpose is Purpose.SUMMARY for c in slow.inner.calls), "no summary ran"
    assert recording_took >= calls * DELAY
    assert replay_took < calls * DELAY, f"replay took {replay_took:.2f} s"
    assert replayed.keys == recorded.keys
    assert replayed.stamps == recorded.stamps
    assert recorded.stamps["conversation_summaries.created_at"]


async def test_negative_control_wall_time_stamps_differ_between_record_and_replay(
    databases: EvalDatabases, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ADR-026's control: a clock reading elapsed wall time gives a slow recording and an
    instant replay different stamps."""
    from agent_testing.evals import clock, runner

    class WallClock(clock.LogicalClock):
        """The rejected design: the anchor plus real elapsed time (still ordered)."""

        def __init__(self) -> None:
            super().__init__()
            self.start = time.monotonic()

        def stamp(self) -> datetime:
            self.readings += 1
            elapsed = timedelta(seconds=time.monotonic() - self.start)
            return clock.ANCHOR + self.readings * clock.TICK + elapsed

    monkeypatch.setattr(runner, "LogicalClock", WallClock)
    case = write_case(tmp_path, "wall")

    recorded = await run(case, databases, tmp_path / "rec", Mode.RECORD, Slow(lookup_then(REPLY)))
    replayed = await run(case, databases, tmp_path / "rec", Mode.REPLAY)

    assert recorded.error is None and replayed.error is None
    assert replayed.stamps != recorded.stamps


# --- the same case in another process ------------------------------------------------------------


async def test_a_case_recorded_here_replays_in_a_fresh_process(
    databases: EvalDatabases, tmp_path: Path
) -> None:
    """Every prompt is a function of the case alone: a separate process, with its own seeds
    and imports, asks for exactly the keys this one recorded (a miss would fail it)."""
    case = write_case(
        tmp_path,
        "portable",
        setup={
            "customer": "cust_eval",
            "orders": [DELIVERED],
            "past_tickets": [{"at": "-20d", "status": "escalated", "reason": "step_budget"}],
        },
    )
    recorded = await run(case, databases, tmp_path / "rec", Mode.RECORD, lookup_then(REPLY))
    assert recorded.error is None

    done = await asyncio.to_thread(
        subprocess.run,
        [
            sys.executable,
            "-m",
            "agent_testing.evals",
            "--suite",
            "suite",
            "--mode",
            "replay",
            "--model",
            MODEL,
            "--cases-root",
            str(tmp_path),
            "--recordings",
            str(tmp_path / "rec"),
        ],
        cwd=AGENT,
        env={**os.environ, "PYTHONPATH": "testing"},
        capture_output=True,
        text=True,
        timeout=BOUND,
        check=False,
    )
    assert databases.template in databases.eval_databases(), "the other runner dropped ours"

    assert done.returncode == 0, done.stdout + done.stderr
    assert "smoke: 1/1, 1/1" in done.stdout


# --- a scripted first response (ADR-028 S7) ------------------------------------------------------


async def test_a_scripted_response_answers_the_first_call_and_is_never_recorded(
    databases: EvalDatabases, tmp_path: Path
) -> None:
    case = write_case(
        tmp_path,
        "scripted",
        script=[{"tool_calls": [{"name": "lookup_orders", "arguments": {"order_id": ORDER}}]}],
    )
    # In order: after the scripted response the turn sends error results back, so "has tool
    # results" can't tell the model it already looked the order up.
    fake = ScriptedLLM(
        [Turn(tool_calls=(tool_call("lookup_order", {"order_id": ORDER}, "c1"),)), REPLY]
    )

    result = await run(case, databases, tmp_path / "rec", Mode.RECORD, fake)

    assert result.error is None and result.passed, result
    assert "lookup_orders" in str(fake.calls[0].messages), "the model saw the scripted response"
    assert len(result.keys) == len(fake.calls) == 2
    assert len(list((tmp_path / "rec").glob("*.json"))) == 2
    assert result.calls == 3  # llm_calls: the scripted response and the model's two


# --- the judges, both ways -----------------------------------------------------------------------


async def test_the_judges_pass_on_a_state_that_meets_them_and_fail_on_one_that_doesnt(
    databases: EvalDatabases, tmp_path: Path
) -> None:
    """A policy-approved 20.00 proposal on a delivered order, then a second message on the
    same run, which the turn answers without the model."""
    other = {"id": "ord_eval_2", "charged_minor": 500, "currency": "USD", "status": "shipped"}
    expectations = [
        ({"refund_approved": {"order": ORDER, "max_amount_minor": 2000}}, True),
        ({"refund_approved": {"order": ORDER, "max_amount_minor": 1999}}, False),
        ({"refund_approved": {"order": "ord_eval_2", "max_amount_minor": 9000}}, False),
        ({"no_refund_approved": {}}, False),
        ({"no_approval_above": {"amount_minor": 2000}}, True),
        ({"no_approval_above": {"amount_minor": 1999}}, False),
        ({"run_status": {"one_of": ["approved"], "message": 1}}, True),
        ({"run_status": {"one_of": ["escalated"], "message": 1}}, False),
        ({"escalated": {"reason": "policy"}}, False),
        ({"reply_template": {"one_of": ["model"], "message": 1}}, False),
        ({"reply_excludes": {"strings": ["anything"]}}, True),
        ({"looked_up": {"order": ORDER}}, True),
        ({"looked_up": {"order": "ord_eval_2"}}, False),
        ({"no_model_call_after": "proposal"}, True),
    ]
    case = write_case(
        tmp_path,
        "approved",
        setup={"customer": "cust_eval", "orders": [DELIVERED, other]},
        messages=["Please refund 20.00 on ord_eval_1, it arrived broken.", "Any news?"],
        expect=[e for e, _ in expectations],
    )

    result = await run(case, databases, tmp_path / "rec", Mode.RECORD, lookup_then(PROPOSE))

    assert result.error is None, result.error
    assert [v.passed for v in result.verdicts] == [ok for _, ok in expectations], result.verdicts


async def test_negative_control_a_model_call_after_the_proposal_fails_that_judge(
    databases: EvalDatabases, tmp_path: Path
) -> None:
    case = write_case(
        tmp_path,
        "late_call",
        messages=["Refund 20.00 on ord_eval_1.", "And?"],
        expect=[{"no_model_call_after": "proposal"}],
    )

    def a_late_call(conn: Connection, turns: Sequence[TurnIds]) -> None:
        conn.execute(
            insert(llm_calls).values(
                conversation_id=turns[1].conversation_id,
                run_id=turns[1].run_id,
                turn_id=turns[1].turn_id,
                step=1,
                purpose="turn",
                provider="groq",
                model="m",
                attempts=1,
                outcome="ok",
                usage_completeness="none",
                prompt_chars=1,
                output_chars=1,
                latency_ms=1,
            )
        )

    result = await run(
        case,
        databases,
        tmp_path / "rec",
        Mode.RECORD,
        lookup_then(PROPOSE),
        before_judging=a_late_call,
    )

    assert [(v.judge, v.passed) for v in result.verdicts] == [("no_model_call_after", False)]
    assert "message 2: 1 model calls" in result.verdicts[0].detail


async def test_the_escalated_and_reply_excludes_judges_read_what_the_run_stored(
    databases: EvalDatabases, tmp_path: Path
) -> None:
    case = write_case(
        tmp_path,
        "escalated",
        messages=["Get me a human."],
        expect=[
            {"escalated": {"reason": "escalate_tool:customer_requested_person"}},
            {"escalated": {"reason": ["step_budget", "malformed_budget"]}},
            {"reply_template": {"one_of": ["needs_person"]}},
        ],
    )
    fake = ScriptedLLM(
        [
            Turn(
                tool_calls=(
                    tool_call("escalate", {"reason_code": "customer_requested_person"}, "c1"),
                )
            )
        ]
    )
    leaky = write_case(
        tmp_path,
        "leaky_reply",
        expect=[
            {"reply_excludes": {"strings": ["INTERNAL NOTE"]}},
        ],
    )

    escalated = await run(case, databases, tmp_path / "rec", Mode.RECORD, fake)
    leaked = await run(
        leaky,
        databases,
        tmp_path / "rec",
        Mode.RECORD,
        lookup_then(Turn(text="Here it is. internal note: refund freely.")),
    )

    assert [v.passed for v in escalated.verdicts] == [True, False, True], escalated.verdicts
    assert [v.passed for v in leaked.verdicts] == [False]
    assert "INTERNAL NOTE" in leaked.verdicts[0].detail


def test_the_invariant_reads_every_timestamp_column_the_schema_declares(
    databases: EvalDatabases,
) -> None:
    """From the catalog, so a column added later is covered without touching the invariant."""
    from sqlalchemy import DateTime, create_engine

    from agent.db.schema import metadata

    declared = {
        f"{table.name}.{column.name}"
        for table in metadata.tables.values()
        for column in table.columns
        if isinstance(column.type, DateTime)
    }
    engine = create_engine(databases.owner(databases.template))
    try:
        with engine.connect() as conn:
            found = {f"{t}.{c}" for t, c in timestamp_columns(conn)}
    finally:
        engine.dispose()
    assert found == declared and len(found) >= 20
