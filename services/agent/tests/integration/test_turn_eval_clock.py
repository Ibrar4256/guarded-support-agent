"""Time, ids and nonces on the turn path (ADR-026 Part 2, S3), against Postgres.

Production stamps stay each transaction's own ``now()``; under the eval clock every stamp a
turn writes sits at the anchor, the refund window follows the anchor rather than the real day,
and the same case run for two customers sends byte-identical prompts.
"""

import asyncio
import re
import uuid
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from agent_testing.evals.clock import ANCHOR, LogicalClock, SeededIds, SeededNonces
from agent_testing.fakes import RecordingAlerter
from agent_testing.scripted_llm import ScriptedLLM, Turn, tool_call
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine
from turn_helpers import SETTINGS, turn_deps
from worker_world import World

from agent.db.clock import DATABASE_CLOCK
from agent.db.schema import conversation_summaries, decisions, events, run_order_lookups, runs
from agent.llm.types import Purpose
from agent.turn.run import TurnDeps, TurnOutcome, TurnResult, run_turn
from agent.turn.settings import TurnSettings
from agent.turn.start import start_turn

BOUND = 15
SUMMARIZING = replace(SETTINGS, window_messages=2, summary_trigger_messages=8)
THREAT = "I am going to sue you, my lawyer is ready"
# A turn writes a handful of stamps; an hour of one-second ticks is far more than a case uses.
CASE_SPAN = timedelta(hours=1)


def eval_deps(
    engine: AsyncEngine, model: ScriptedLLM, seed: str, settings: TurnSettings = SETTINGS
) -> TurnDeps:
    deps = turn_deps(engine, model, alerter=RecordingAlerter(), records=[], settings=settings)
    return replace(deps, clock=LogicalClock(), new_id=SeededIds(seed), nonce=SeededNonces(seed))


async def say(deps: TurnDeps, world: World, conversation: uuid.UUID, message: str) -> TurnResult:
    async with asyncio.timeout(BOUND):
        result = await run_turn(
            deps, conversation_id=conversation, user=world.user, message=message
        )
    await deps.background.drain(BOUND)
    assert len(deps.background) == 0, "a summary is still running after the bound"
    return result


def look_up_then_propose(order: str, amount: int) -> ScriptedLLM:
    return ScriptedLLM(
        [
            Turn(tool_calls=(tool_call("lookup_order", {"order_id": order}, "c1"),)),
            Turn(
                tool_calls=(
                    tool_call(
                        "propose_refund",
                        {"order_id": order, "amount_minor": amount, "currency": "USD"},
                        "c2",
                    ),
                ),
                match=lambda view: view.has_tool_results,
            ),
        ]
    )


def chatty() -> ScriptedLLM:
    return ScriptedLLM(
        [
            Turn(text="ok", match=lambda v: v.purpose is Purpose.TURN, repeat=True),
            Turn(text="A summary.", match=lambda v: v.purpose is Purpose.SUMMARY, repeat=True),
        ]
    )


def stamps(world: World, conversations: list[uuid.UUID]) -> dict[str, list[datetime]]:
    """Every timestamp the turn path wrote for these conversations, by column."""
    with world.owner.connect() as connection:
        run_rows = connection.execute(select(runs).where(runs.c.conversation_id.in_(conversations)))
        run_list = list(run_rows)
        run_ids = [row.id for row in run_list]
        found: dict[str, list[datetime]] = {}
        for column in ("created_at", "approved_at", "escalated_at", "c01_flagged_at"):
            found[f"runs.{column}"] = [getattr(r, column) for r in run_list if getattr(r, column)]
        queries: dict[str, Any] = {
            "events.created_at": select(events.c.created_at).where(
                events.c.conversation_id.in_(conversations)
            ),
            "decisions.decided_at": select(decisions.c.decided_at).where(
                decisions.c.run_id.in_(run_ids)
            ),
            "run_order_lookups.looked_up_at": select(run_order_lookups.c.looked_up_at).where(
                run_order_lookups.c.run_id.in_(run_ids)
            ),
            "conversation_summaries.created_at": select(conversation_summaries.c.created_at).where(
                conversation_summaries.c.conversation_id.in_(conversations)
            ),
        }
        for name, query in queries.items():
            found[name] = list(connection.execute(query).scalars())
        found["runs.review_deadline"] = [r.review_deadline for r in run_list if r.review_deadline]
    return found


# --- Production: unchanged ------------------------------------------------------------------------


async def test_production_stamps_are_the_writing_transactions_own_now(
    world: World, async_runtime_engine: AsyncEngine
) -> None:
    """The run, its first event and the lease are written in one transaction: under SQL
    ``now()`` all three share its start time to the microsecond, as before ADR-026."""
    conversation = world.add_conversation()
    started = await start_turn(
        async_runtime_engine,
        SETTINGS,
        conversation_id=conversation,
        user=world.user,
        message="hi",
        clock=DATABASE_CLOCK,
        new_id=uuid.uuid4,
    )

    run = world.row(started.ids.run_id)
    (event,) = world.conversation_events(conversation)
    lease_taken_at = world.conversation(conversation).turn_expires_at - SETTINGS.lease
    assert run.created_at == event.created_at == lease_taken_at


# --- The eval clock -------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("days_before_anchor", "outcome"),
    [(29, TurnOutcome.APPROVED), (31, TurnOutcome.ESCALATED)],
)
async def test_the_refund_window_follows_the_eval_clock_whatever_the_real_date(
    world: World, async_runtime_engine: AsyncEngine, days_before_anchor: int, outcome: TurnOutcome
) -> None:
    """The anchor is months in the past: on the real clock both orders are out of window."""
    order = world.add_order(delivered_at=ANCHOR - timedelta(days=days_before_anchor))
    conversation = world.add_conversation()
    deps = eval_deps(async_runtime_engine, look_up_then_propose(order, 2_000), "window")

    result = await say(deps, world, conversation, "I'd like a refund")

    assert result.outcome is outcome
    (decision,) = world.decisions(result.ids.run_id)
    assert ("R08_REFUND_WINDOW" in decision.rule_ids) is (outcome is TurnOutcome.ESCALATED)


async def test_on_the_database_clock_an_order_29_days_before_the_anchor_is_out_of_window(
    world: World, async_runtime_engine: AsyncEngine
) -> None:
    """The control for the test above: it proves the anchor is far enough from today."""
    order = world.add_order(delivered_at=ANCHOR - timedelta(days=29))
    conversation = world.add_conversation()
    deps = turn_deps(
        async_runtime_engine,
        look_up_then_propose(order, 2_000),
        alerter=RecordingAlerter(),
        records=[],
    )

    result = await say(deps, world, conversation, "I'd like a refund")

    (decision,) = world.decisions(result.ids.run_id)
    assert result.outcome is TurnOutcome.ESCALATED and "R08_REFUND_WINDOW" in decision.rule_ids


async def test_every_stamp_a_turn_writes_comes_from_the_eval_clock(
    world: World, async_runtime_engine: AsyncEngine
) -> None:
    """An approved proposal, a review proposal (lookup, decision, deadline, pending event), a
    C01 escalation, an escalate call and a background summary: every stamp sits just after
    the anchor."""
    order = world.add_order(delivered_at=ANCHOR - timedelta(days=3))
    approved_order = world.add_order(delivered_at=ANCHOR - timedelta(days=3))
    review, approved, threat, handover, summarized = (world.add_conversation() for _ in range(5))
    seed = f"stamps-{uuid.uuid4()}"
    await say(
        eval_deps(async_runtime_engine, look_up_then_propose(order, 6_000), f"{seed}:a"),
        world,
        review,
        "I'd like a refund",
    )
    await say(
        eval_deps(async_runtime_engine, look_up_then_propose(approved_order, 2_000), f"{seed}:e"),
        world,
        approved,
        "I'd like a refund",
    )
    await say(eval_deps(async_runtime_engine, ScriptedLLM([]), f"{seed}:b"), world, threat, THREAT)
    escalate = ScriptedLLM(
        [Turn(tool_calls=(tool_call("escalate", {"reason_code": "customer_requested_person"}),))]
    )
    await say(
        eval_deps(async_runtime_engine, escalate, f"{seed}:c"), world, handover, "a person, please"
    )
    summarizer = chatty()
    summary_deps = eval_deps(async_runtime_engine, summarizer, f"{seed}:d", SUMMARIZING)
    for index in range(6):
        await say(summary_deps, world, summarized, f"question {index}")

    found = stamps(world, [review, approved, threat, handover, summarized])
    deadlines = found.pop("runs.review_deadline")
    assert all(found.values()), f"a column wrote nothing to check: {found}"
    late = {
        column: values
        for column, values in found.items()
        if not all(ANCHOR < value < ANCHOR + CASE_SPAN for value in values)
    }
    assert late == {}
    seeded = SeededNonces(f"{seed}:d")
    expected_nonces = {seeded() for _ in range(20)}
    (summary_call,) = [c for c in summarizer.calls if c.purpose is Purpose.SUMMARY]
    fences = set(re.findall(r"<untrusted-([0-9a-f]{16})>", str(summary_call.messages)))
    assert fences and fences <= expected_nonces, "the summary's fence isn't the seeded nonce"
    review_window = SETTINGS.review_deadline
    assert all(ANCHOR + review_window < d < ANCHOR + review_window + CASE_SPAN for d in deadlines)


async def test_the_digest_and_the_fences_come_from_the_seeded_sources_never_the_real_day(
    world: World, async_runtime_engine: AsyncEngine
) -> None:
    """A reply, then a second conversation whose digest shows the first ticket: exactly the
    seeded id and the anchor's date, fenced with the seeded nonce. Identical prompts for the
    same case twice need a fresh database per case (the runner, ADR-026 Part 1); here each
    execution takes its own seed, since the shared test database keeps earlier runs' ids."""
    seed = f"digest-{uuid.uuid4()}"
    model = ScriptedLLM([Turn(text="Glad to help.", repeat=True)])
    deps = eval_deps(async_runtime_engine, model, seed)
    await say(deps, world, world.add_conversation(), "hello")
    await say(deps, world, world.add_conversation(), "hello again")

    ids, nonces = SeededIds(seed), SeededNonces(seed)
    first_ticket = ids()
    _, second_nonce = nonces(), nonces()
    system = model.calls[1].messages[0]["content"]
    assert f"ticket {first_ticket.hex[:8]} ({ANCHOR.date().isoformat()}): resolved" in system
    assert f"<untrusted-{second_nonce}>" in system
    today = datetime.now(UTC).date().isoformat()
    assert today not in str([call.messages for call in model.calls])
