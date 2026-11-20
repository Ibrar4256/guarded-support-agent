"""C01 at the turn's start (ADR-009 conversation rules, ADR-022 S1).

In AGENT_STEP a hit escalates the run with no model call; after a proposal it flags the run for
reviewers and bumps review_revision, and the refund carries on. The decision log keeps pattern
ids only.
"""

import json
import uuid

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine
from turn_helpers import SETTINGS
from worker_world import World

from agent.core import escalations
from agent.core.lifecycle import RunStatus
from agent.core.replies import ReplyTemplate
from agent.db.clock import DATABASE_CLOCK
from agent.turn.start import TurnStart, start_turn

THREAT = "I am going to sue you, my lawyer is ready"


async def say(
    engine: AsyncEngine, world: World, conversation: uuid.UUID, message: str
) -> TurnStart:
    return await start_turn(
        engine,
        SETTINGS,
        conversation_id=conversation,
        user=world.user,
        message=message,
        clock=DATABASE_CLOCK,
        new_id=uuid.uuid4,
    )


async def test_c01_on_a_first_message_escalates_with_no_model_call(
    world: World, async_runtime_engine: AsyncEngine
) -> None:
    conversation = world.add_conversation()

    started = await say(async_runtime_engine, world, conversation, THREAT)

    run = world.row(started.ids.run_id)
    assert started.answered is ReplyTemplate.NEEDS_PERSON
    assert started.status is RunStatus.ESCALATED
    assert run.status == RunStatus.ESCALATED
    assert run.escalated_at is not None and run.c01_flagged_at is not None
    assert run.escalation_reason == escalations.C01
    assert world.conversation(conversation).active_turn_id is None
    events = world.conversation_events(conversation)
    assert [e.kind for e in events] == ["customer_message", "reply"]
    assert events[-1].payload == {"template": "needs_person"}


async def test_c01_on_a_resumed_run_escalates_that_run(
    world: World, async_runtime_engine: AsyncEngine
) -> None:
    run = world.add_run(RunStatus.AGENT_STEP)
    conversation = world.row(run).conversation_id

    started = await say(async_runtime_engine, world, conversation, "you're useless")

    assert started.ids.run_id == run
    assert world.row(run).status == RunStatus.ESCALATED


@pytest.mark.parametrize(
    ("status", "template"),
    [
        (RunStatus.AWAIT_APPROVAL, ReplyTemplate.AWAITING_REVIEW),
        (RunStatus.APPROVED, ReplyTemplate.PROCESSING),
        (RunStatus.EXECUTING, ReplyTemplate.PROCESSING),
    ],
)
async def test_c01_after_a_proposal_flags_for_reviewers_and_leaves_the_refund_alone(
    world: World, async_runtime_engine: AsyncEngine, status: RunStatus, template: ReplyTemplate
) -> None:
    run = world.add_refund_run(world.add_order(), 2_000, status)
    before = world.row(run)
    conversation = before.conversation_id

    started = await say(async_runtime_engine, world, conversation, THREAT)

    after = world.row(run)
    assert started.answered is template
    assert after.status == status  # not cancelled, not escalated
    assert after.c01_flagged_at is not None and after.escalated_at is not None
    assert after.review_revision == before.review_revision + 1  # A03: stale approvals fail
    (decision,) = world.decisions(run)
    assert (decision.stage, decision.outcome) == ("conversation_escalation", "FLAGGED")
    assert decision.action_id == before.action_id


async def test_the_decision_log_keeps_pattern_ids_never_the_message(
    world: World, async_runtime_engine: AsyncEngine
) -> None:
    conversation = world.add_conversation()

    started = await say(async_runtime_engine, world, conversation, THREAT)

    (decision,) = world.decisions(started.ids.run_id)
    assert decision.rule_ids == ["C01_LEGAL_OR_ABUSE"]
    assert decision.input_snapshot == {"patterns": ["C01_LEGAL_ACTION", "C01_LEGAL_COUNSEL"]}
    stored = json.dumps([decision.input_snapshot, decision.config_snapshot])
    assert "sue" not in stored.replace("C01_", "") and "lawyer" not in stored


async def test_a_clean_message_flags_nothing(
    world: World, async_runtime_engine: AsyncEngine
) -> None:
    conversation = world.add_conversation()

    started = await say(async_runtime_engine, world, conversation, "where is my order?")

    run = world.row(started.ids.run_id)
    assert run.status == RunStatus.AGENT_STEP and run.c01_flagged_at is None
    assert world.decisions(started.ids.run_id) == []
