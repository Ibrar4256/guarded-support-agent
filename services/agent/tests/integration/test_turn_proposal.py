"""The write tools and ADR-009's proposal path through ``run_turn`` (ADR-022 S1, S7; ADR-009
proposal phase, Concurrency, Fail closed; ADR-004 step 1; ADR-008 transitions).

ScriptedLLM behind the real pool, against Postgres as the runtime role. A strict script also
proves the model is never called after a proposal: one call too many fails the test.
"""

import asyncio
import json
import threading
import uuid
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from agent_testing.fakes import RecordingAlerter
from agent_testing.scripted_llm import PromptView, ScriptedLLM, Turn, tool_call
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine
from turn_helpers import SETTINGS, turn_deps
from worker_harness import POLICY
from worker_world import World

from agent.alerts import Severity
from agent.core import policy as core_policy
from agent.core.capacity import ApprovalSource
from agent.core.lifecycle import RunStatus
from agent.core.policy import POLICY_VERSION, FulfillmentStatus, PolicyConfig, RefundProposal
from agent.core.replies import ReplyTemplate
from agent.core.review_hash import review_hash
from agent.db import proposal as proposal_db
from agent.db.schema import policy_controls
from agent.refund_client.payload import canonical_payload
from agent.turn import loop, propose
from agent.turn import run as run_module
from agent.turn.run import TurnOutcome, TurnResult, run_turn
from agent.turn.settings import TurnSettings

BOUND = 15
LOCKING = replace(
    SETTINGS,
    statement_timeout=timedelta(seconds=1),
    lock_timeout=timedelta(seconds=0.3),
)


async def turn(
    engine: AsyncEngine,
    world: World,
    conversation: uuid.UUID,
    model: ScriptedLLM,
    *,
    message: str = "I'd like a refund",
    alerter: RecordingAlerter | None = None,
    settings: TurnSettings = SETTINGS,
    policy: PolicyConfig = POLICY,
) -> TurnResult:
    deps = turn_deps(
        engine,
        model,
        alerter=alerter or RecordingAlerter(),
        records=[],
        settings=settings,
        policy=policy,
    )
    async with asyncio.timeout(BOUND):
        return await run_turn(deps, conversation_id=conversation, user=world.user, message=message)


def replies(world: World, conversation: uuid.UUID) -> list[dict[str, Any]]:
    return [e.payload for e in world.conversation_events(conversation) if e.kind == "reply"]


def tool_results(view: PromptView, count: int) -> bool:
    return view.roles.count("tool") == count


def lookup(order: str, call_id: str = "c_lookup") -> Any:
    return tool_call("lookup_order", {"order_id": order}, call_id)


def proposal(order: str, amount: int, currency: str = "USD", call_id: str = "c_propose") -> Any:
    return tool_call(
        "propose_refund",
        {"order_id": order, "amount_minor": amount, "currency": currency},
        call_id,
    )


def look_up_then_propose(order: str, amount: int) -> ScriptedLLM:
    return ScriptedLLM(
        [
            Turn(tool_calls=(lookup(order),), label="lookup"),
            Turn(
                tool_calls=(proposal(order, amount),),
                match=lambda v: tool_results(v, 1),
                label="propose",
            ),
        ]
    )


def last_tool_result(model: ScriptedLLM, call: int) -> dict[str, Any]:
    message = model.calls[call].messages[-1]
    assert message["role"] == "tool"
    result: dict[str, Any] = json.loads(message["content"])
    return result


def controls_row(world: World) -> Any:
    with world.owner.connect() as connection:
        return connection.execute(select(policy_controls).where(policy_controls.c.id == 1)).one()


# --- The three outcomes ------------------------------------------------------------------------


async def test_an_in_policy_proposal_is_approved_with_key_payload_and_review_hash(
    world: World, async_runtime_engine: AsyncEngine
) -> None:
    order = world.add_order(charged_minor=8_000)
    conversation = world.add_conversation()
    model = look_up_then_propose(order, 2_000)

    result = await turn(async_runtime_engine, world, conversation, model)

    assert result.outcome is TurnOutcome.APPROVED
    row = world.row(result.ids.run_id)
    assert row.status == RunStatus.APPROVED
    assert (row.order_id, row.amount_minor, row.currency) == (order, 2_000, "USD")
    assert row.approval_source == ApprovalSource.POLICY and row.approved_by is None
    assert row.approved_at is not None and row.action_id.startswith("act_")
    assert row.idempotency_key and len(row.idempotency_key) >= 43
    assert row.canonical_payload == canonical_payload(
        order_id=order, amount_minor=2_000, currency="USD", action_id=row.action_id
    )
    expected_hash = review_hash(
        RefundProposal(row.action_id, order, 2_000, "USD"), (), POLICY_VERSION
    )
    assert row.review_hash == expected_hash
    (decision,) = world.decisions(result.ids.run_id)
    assert (decision.stage, decision.outcome, decision.rule_ids) == ("proposal", "AUTO", [])
    assert decision.action_id == row.action_id and decision.review_hash == expected_hash
    assert decision.input_snapshot["looked_up_order_ids"] == [order]
    assert replies(world, conversation) == [{"template": ReplyTemplate.PROCESSING.value}]
    assert world.conversation(conversation).active_turn_id is None
    assert len(model.calls) == 2
    model.assert_finished()


async def test_a_proposal_over_the_auto_approve_maximum_waits_for_a_reviewer(
    world: World, async_runtime_engine: AsyncEngine
) -> None:
    order = world.add_order(charged_minor=8_000)
    conversation = world.add_conversation()

    result = await turn(
        async_runtime_engine, world, conversation, look_up_then_propose(order, 6_000)
    )

    assert result.outcome is TurnOutcome.AWAITING_REVIEW
    run = result.ids.run_id
    row = world.row(run)
    assert row.status == RunStatus.AWAIT_APPROVAL
    assert row.idempotency_key is None and row.canonical_payload is None
    assert row.approval_source is None and row.approved_at is None
    assert row.review_hash == review_hash(
        RefundProposal(row.action_id, order, 6_000, "USD"),
        ("R07_AUTO_APPROVE_MAX",),
        POLICY_VERSION,
    )
    remaining = world.seconds_until(run, "review_deadline")
    assert 86_400 - 60 < remaining <= 86_400
    (decision,) = world.decisions(run)
    assert (decision.outcome, decision.rule_ids) == ("REVIEW", ["R07_AUTO_APPROVE_MAX"])
    pending = [e.payload for e in world.events(run) if e.kind == "pending_review"]
    assert pending == [{"action_id": row.action_id}]
    assert replies(world, conversation) == [{"template": ReplyTemplate.AWAITING_REVIEW.value}]


async def test_an_out_of_policy_proposal_escalates_and_nothing_is_queued(
    world: World, async_runtime_engine: AsyncEngine
) -> None:
    order = world.add_order(charged_minor=8_000, status=FulfillmentStatus.SHIPPED)
    conversation = world.add_conversation()

    result = await turn(
        async_runtime_engine, world, conversation, look_up_then_propose(order, 2_000)
    )

    assert result.outcome is TurnOutcome.ESCALATED
    row = world.row(result.ids.run_id)
    assert row.status == RunStatus.ESCALATED and row.escalated_at is not None
    assert row.action_id is not None
    assert (row.order_id, row.amount_minor, row.idempotency_key) == (None, None, None)
    (decision,) = world.decisions(result.ids.run_id)
    assert decision.outcome == "OUT_OF_POLICY"
    assert {"R08_REFUND_WINDOW", "R09_ORDER_STATUS"} <= set(decision.rule_ids)
    assert decision.input_snapshot["proposal"]["amount_minor"] == 2_000
    # Non-disclosure: the customer sees the template, never which rule fired.
    assert replies(world, conversation) == [{"template": ReplyTemplate.NEEDS_PERSON.value}]


async def test_a_refused_amount_escalates_without_tripping_the_runs_own_check(
    world: World, async_runtime_engine: AsyncEngine
) -> None:
    """R01 refuses ``amount_minor = 0``; storing it on the run would break ``amount_positive``
    and turn a policy decision into a database failure."""
    order = world.add_order(charged_minor=8_000)
    conversation = world.add_conversation()

    result = await turn(async_runtime_engine, world, conversation, look_up_then_propose(order, 0))

    assert result.outcome is TurnOutcome.ESCALATED
    (decision,) = world.decisions(result.ids.run_id)
    assert (decision.outcome, decision.rule_ids) == ("OUT_OF_POLICY", ["R01_AMOUNT_POSITIVE"])


# --- The lookup check ("yes, refund it") ---------------------------------------------------------


async def test_yes_refund_it_looks_the_order_up_in_this_run_before_the_proposal(
    world: World, async_runtime_engine: AsyncEngine
) -> None:
    """ADR-022 S1: the model's own earlier reply names the order, but this run never looked it
    up. The proposal is bounced back as malformed; the model looks it up; the proposal passes."""
    order = world.add_order(charged_minor=8_000)
    conversation = world.add_conversation()
    first = ScriptedLLM(
        [
            Turn(tool_calls=(lookup(order),)),
            Turn(
                text=f"Order {order} was delivered three days ago.",
                match=lambda v: tool_results(v, 1),
            ),
        ]
    )
    answered = await turn(
        async_runtime_engine, world, conversation, first, message="was my order delivered?"
    )
    assert answered.outcome is TurnOutcome.REPLIED
    second = ScriptedLLM(
        [
            Turn(tool_calls=(proposal(order, 2_000, call_id="c_early"),), label="early proposal"),
            Turn(tool_calls=(lookup(order),), match=lambda v: tool_results(v, 1), label="lookup"),
            Turn(
                tool_calls=(proposal(order, 2_000),),
                match=lambda v: tool_results(v, 2),
                label="proposal",
            ),
        ]
    )

    result = await turn(async_runtime_engine, world, conversation, second, message="yes, refund it")

    run = result.ids.run_id
    assert run != answered.ids.run_id
    assert world.row(run).status == RunStatus.APPROVED
    assert result.outcome is TurnOutcome.APPROVED
    bounced = last_tool_result(second, 1)
    assert bounced["error"] == "not_run" and loop.NOT_LOOKED_UP in bounced["message"]
    assert world.lookups(run) == [order]
    (decision,) = world.decisions(run)
    assert decision.outcome == "AUTO"
    second.assert_finished()


async def test_a_bounced_proposal_counts_toward_the_malformed_budget(
    world: World, async_runtime_engine: AsyncEngine
) -> None:
    order = world.add_order(charged_minor=8_000)
    model = ScriptedLLM([Turn(tool_calls=(proposal(order, 2_000),), repeat=True)])

    result = await turn(async_runtime_engine, world, world.add_conversation(), model)

    assert result.outcome is TurnOutcome.ESCALATED
    assert len(model.calls) == SETTINGS.max_malformed + 1
    assert world.decisions(result.ids.run_id) == []  # policy never saw it


# --- Write calls must be alone -----------------------------------------------------------------


async def test_a_write_call_mixed_with_another_call_is_malformed_and_nothing_runs(
    world: World, async_runtime_engine: AsyncEngine
) -> None:
    looked_up, other = world.add_order(charged_minor=8_000), world.add_order()
    conversation = world.add_conversation()
    model = ScriptedLLM(
        [
            Turn(tool_calls=(lookup(looked_up),)),
            Turn(
                tool_calls=(proposal(looked_up, 2_000), lookup(other, "c_other")),
                match=lambda v: tool_results(v, 1),
                label="mixed",
            ),
            Turn(text="Let me check that again.", match=lambda v: tool_results(v, 3)),
        ]
    )

    result = await turn(async_runtime_engine, world, conversation, model)

    assert world.row(result.ids.run_id).status == RunStatus.COMPLETED
    assert result.outcome is TurnOutcome.REPLIED
    errors = [json.loads(m["content"]) for m in model.calls[2].messages[-2:]]
    assert all(e["error"] == "not_run" and loop.NOT_ALONE in e["message"] for e in errors)
    assert world.lookups(result.ids.run_id) == [looked_up]
    assert world.decisions(result.ids.run_id) == []


async def test_two_write_calls_in_one_response_are_malformed(
    world: World, async_runtime_engine: AsyncEngine
) -> None:
    order = world.add_order(charged_minor=8_000)
    escalate = tool_call("escalate", {"reason_code": "other"}, "c_escalate")
    model = ScriptedLLM(
        [
            Turn(tool_calls=(lookup(order),)),
            Turn(tool_calls=(escalate, proposal(order, 2_000)), match=lambda v: tool_results(v, 1)),
            Turn(text="Sorry, let me try again.", match=lambda v: tool_results(v, 3)),
        ]
    )

    result = await turn(async_runtime_engine, world, world.add_conversation(), model)

    assert result.outcome is TurnOutcome.REPLIED
    assert world.decisions(result.ids.run_id) == []


# --- escalate --------------------------------------------------------------------------------


async def test_the_escalate_tool_escalates_with_the_needs_person_template(
    world: World, async_runtime_engine: AsyncEngine
) -> None:
    conversation = world.add_conversation()
    model = ScriptedLLM(
        [
            Turn(tool_calls=(tool_call("escalate", {"reason_code": "frustrated"}, "c_bad"),)),
            Turn(
                tool_calls=(tool_call("escalate", {"reason_code": "customer_requested_person"}),),
                match=lambda v: tool_results(v, 1),
            ),
        ]
    )

    result = await turn(
        async_runtime_engine, world, conversation, model, message="get me a human please"
    )

    assert result.outcome is TurnOutcome.ESCALATED
    assert "reason_code" in last_tool_result(model, 1)["message"]
    row = world.row(result.ids.run_id)
    assert row.status == RunStatus.ESCALATED and row.action_id is None
    assert world.decisions(result.ids.run_id) == []
    assert replies(world, conversation) == [{"template": ReplyTemplate.NEEDS_PERSON.value}]
    model.assert_finished()


# --- Lock timeout ----------------------------------------------------------------------------


class CustomerLock:
    """Another transaction holds the customer row (ADR-005's lock), from its own thread so the
    turn's event loop never blocks on it. Taken only once the proposal path starts: held from the
    turn's start, it would also block ``create_run`` (the FK check takes ``FOR KEY SHARE`` on the
    customer row)."""

    def __init__(self, world: World) -> None:
        self.world = world
        self.locked = threading.Event()
        self.release = threading.Event()
        self.thread = threading.Thread(target=self._hold, daemon=True)

    def _hold(self) -> None:
        with self.world.owner.begin() as connection:
            connection.execute(
                text("SELECT id FROM customers WHERE id = :u FOR UPDATE"), {"u": self.world.user}
            )
            self.locked.set()
            self.release.wait(BOUND)

    def __enter__(self) -> "CustomerLock":
        self.thread.start()
        assert self.locked.wait(BOUND)
        return self

    def __exit__(self, *_: object) -> None:
        self.release.set()
        self.thread.join(BOUND)
        assert not self.thread.is_alive()


async def test_a_proposal_lock_timeout_says_try_again_and_the_next_message_proposes_on_the_same_run(
    world: World, async_runtime_engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    order = world.add_order(charged_minor=8_000)
    conversation = world.add_conversation()
    attempts = 0
    original = propose._evaluate_and_move

    async def counting(*args: Any) -> Any:
        nonlocal attempts
        attempts += 1
        return await original(*args)

    monkeypatch.setattr(propose, "_evaluate_and_move", counting)
    real_proposal = propose.run_proposal

    async def under_lock(*args: Any) -> Any:
        monkeypatch.setattr(run_module, "run_proposal", real_proposal)  # the first one only
        lock = CustomerLock(world)
        await asyncio.to_thread(lock.__enter__)
        try:
            return await real_proposal(*args)
        finally:
            await asyncio.to_thread(lock.__exit__)

    monkeypatch.setattr(run_module, "run_proposal", under_lock)
    alerter = RecordingAlerter()

    waited = await turn(
        async_runtime_engine,
        world,
        conversation,
        look_up_then_propose(order, 2_000),
        settings=LOCKING,
        alerter=alerter,
    )

    run = waited.ids.run_id
    assert world.row(run).status == RunStatus.AGENT_STEP
    assert waited.outcome is TurnOutcome.WAITING
    assert attempts == LOCKING.proposal_lock_attempts
    assert replies(world, conversation) == [
        {"template": ReplyTemplate.TRY_AGAIN.value, "cause": "lock"}
    ]
    assert world.decisions(run) == []
    assert world.conversation(conversation).active_turn_id is None
    assert alerter.raised == []

    retry = ScriptedLLM([Turn(tool_calls=(proposal(order, 2_000),))])
    result = await turn(
        async_runtime_engine, world, conversation, retry, message="ok, try again", settings=LOCKING
    )

    assert result.outcome is TurnOutcome.APPROVED
    assert result.ids.run_id == run  # the persisted lookup log was reused
    assert [r.id for r in world.runs_of(conversation)] == [run]


# --- Fail closed -----------------------------------------------------------------------------


class FakeOrig(Exception):
    def __init__(self, sqlstate: str) -> None:
        super().__init__(sqlstate)
        self.sqlstate = sqlstate


@pytest.mark.parametrize(
    ("failure", "reason"),
    [
        (core_policy.MissingInputError("policy_controls row is missing"), "missing input"),
        (DBAPIError("SELECT 1", {}, FakeOrig("40001")), "database error 40001"),
    ],
    ids=["missing_input", "database_error"],
)
async def test_a_proposal_that_cannot_be_evaluated_fails_closed_in_a_separate_transaction(
    world: World,
    async_runtime_engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
    failure: Exception,
    reason: str,
) -> None:
    order = world.add_order(charged_minor=8_000)
    conversation = world.add_conversation()

    async def failing(*_: Any, **__: Any) -> Any:
        raise failure

    monkeypatch.setattr(proposal_db, "load_proposal_inputs", failing)
    alerter = RecordingAlerter()

    result = await turn(
        async_runtime_engine,
        world,
        conversation,
        look_up_then_propose(order, 2_000),
        alerter=alerter,
    )

    assert result.outcome is TurnOutcome.ESCALATED
    row = world.row(result.ids.run_id)
    assert row.status == RunStatus.ESCALATED and row.idempotency_key is None
    (decision,) = world.decisions(result.ids.run_id)
    assert (decision.stage, decision.outcome) == ("proposal", "FAIL_CLOSED")
    assert reason in decision.input_snapshot["reason"]
    assert decision.action_id == row.action_id
    assert alerter.kinds(Severity.ALERT) == [propose.FAIL_CLOSED_ALERT]
    assert replies(world, conversation) == [{"template": ReplyTemplate.NEEDS_PERSON.value}]
    assert world.conversation(conversation).active_turn_id is None


async def test_a_rule_that_raises_fails_closed_and_the_other_fired_rules_are_still_logged(
    world: World, async_runtime_engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    order = world.add_order(charged_minor=8_000)

    def raising(_: Any) -> bool:
        raise ValueError("unrecognized fulfillment status")

    monkeypatch.setattr(core_policy, "_order_status", raising)

    result = await turn(
        async_runtime_engine, world, world.add_conversation(), look_up_then_propose(order, 6_000)
    )

    assert result.outcome is TurnOutcome.ESCALATED
    (decision,) = world.decisions(result.ids.run_id)
    assert decision.outcome == "FAIL_CLOSED"
    assert set(decision.rule_ids) == {"FAIL_CLOSED:R09_ORDER_STATUS", "R07_AUTO_APPROVE_MAX"}


async def test_the_fail_closed_transaction_is_fenced_on_the_turn(
    world: World, async_runtime_engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ADR-009: a stale turn lease → 0 rows, the run unchanged, nothing from the stale turn."""
    order = world.add_order(charged_minor=8_000)
    conversation = world.add_conversation()
    taken_over: list[uuid.UUID] = []

    async def failing(*_: Any, **__: Any) -> Any:
        raise core_policy.MissingInputError("order vanished")

    original = propose._fail_closed

    async def after_takeover(ctx: Any, *args: Any) -> Any:
        taken_over.append(await asyncio.to_thread(world.hold_turn, ctx.ids.run_id))
        return await original(ctx, *args)

    monkeypatch.setattr(proposal_db, "load_proposal_inputs", failing)
    monkeypatch.setattr(propose, "_fail_closed", after_takeover)

    result = await turn(
        async_runtime_engine, world, conversation, look_up_then_propose(order, 2_000)
    )

    assert world.row(result.ids.run_id).status == RunStatus.AGENT_STEP
    assert result.outcome is TurnOutcome.LOST
    assert world.decisions(result.ids.run_id) == []
    assert replies(world, conversation) == []
    assert world.conversation(conversation).active_turn_id == taken_over[0]


async def test_if_the_fail_closed_transaction_fails_too_it_alerts_and_the_run_waits(
    world: World, async_runtime_engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    order = world.add_order(charged_minor=8_000)

    async def failing(*_: Any, **__: Any) -> Any:
        raise core_policy.MissingInputError("order vanished")

    async def broken(*_: Any) -> None:
        raise RuntimeError("the database went away")

    monkeypatch.setattr(proposal_db, "load_proposal_inputs", failing)
    monkeypatch.setattr(proposal_db, "escalate_proposal", broken)
    alerter = RecordingAlerter()
    conversation = world.add_conversation()

    with pytest.raises(RuntimeError, match="went away"):
        await turn(
            async_runtime_engine,
            world,
            conversation,
            look_up_then_propose(order, 2_000),
            alerter=alerter,
        )

    (run,) = world.runs_of(conversation)
    assert run.status == RunStatus.AGENT_STEP
    assert world.decisions(run.id) == []
    assert alerter.kinds(Severity.ALERT) == [propose.NOT_RECORDED_ALERT]


# --- Concurrency and the breaker -------------------------------------------------------------


async def test_concurrent_proposals_on_one_order_auto_approve_exactly_once(
    world: World, async_runtime_engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ADR-009 R11/R12 under the customer lock: the first proposal pauses inside its locked
    transaction, after its reads; the second, in another conversation, must wait for it and then
    sees it as pending exposure. Released only once the second is blocked on a lock or has read
    its own inputs, so without the lock both read before either commits."""
    order = world.add_order(charged_minor=8_000)
    first_read, release = asyncio.Event(), asyncio.Event()
    reads_done = 0
    real_load = proposal_db.load_proposal_inputs

    async def load(*args: Any, **kwargs: Any) -> Any:
        nonlocal reads_done
        inputs = await real_load(*args, **kwargs)
        reads_done += 1
        if reads_done == 1:
            first_read.set()
            await asyncio.wait_for(release.wait(), BOUND)
        return inputs

    monkeypatch.setattr(proposal_db, "load_proposal_inputs", load)
    # Created up front: an owner insert while the first holds the customer lock would wait on it.
    first_conversation, second_conversation = world.add_conversation(), world.add_conversation()

    async def second_blocked_or_read() -> None:
        while reads_done < 2 and await asyncio.to_thread(world.waiting_locks) == 0:
            await asyncio.sleep(0.02)

    async def run_second() -> TurnResult:
        await asyncio.wait_for(first_read.wait(), BOUND)
        task = asyncio.ensure_future(
            turn(
                async_runtime_engine,
                world,
                second_conversation,
                look_up_then_propose(order, 2_000),
            )
        )
        await asyncio.wait_for(second_blocked_or_read(), BOUND)
        release.set()
        return await task

    first, second = await asyncio.gather(
        turn(
            async_runtime_engine,
            world,
            first_conversation,
            look_up_then_propose(order, 2_000),
        ),
        run_second(),
    )

    assert (first.outcome, second.outcome) == (
        TurnOutcome.APPROVED,
        TurnOutcome.AWAITING_REVIEW,
    )
    (later,) = world.decisions(second.ids.run_id)
    assert "R12_OTHER_ACTION_ON_ORDER" in later.rule_ids


async def test_a_proposal_over_the_breaker_threshold_trips_it_once_and_goes_to_review(
    world: World, async_runtime_engine: AsyncEngine
) -> None:
    policy = replace(POLICY, breaker_threshold_minor=3_000)
    world.add_run(
        RunStatus.APPROVED,
        approved_at=datetime.now(UTC),
        action_id=f"act_{uuid.uuid4().hex}",
        order_id=world.add_order(charged_minor=8_000),
        amount_minor=2_000,
        currency="USD",
    )
    order = world.add_order(charged_minor=8_000)
    alerter = RecordingAlerter()

    result = await turn(
        async_runtime_engine,
        world,
        world.add_conversation(),
        look_up_then_propose(order, 2_000),
        alerter=alerter,
        policy=policy,
    )

    assert result.outcome is TurnOutcome.AWAITING_REVIEW
    assert controls_row(world).breaker_tripped_at is not None
    proposal_row, trip = world.decisions(result.ids.run_id)
    assert "R13_CIRCUIT_BREAKER" in proposal_row.rule_ids
    assert (trip.stage, trip.outcome, trip.rule_ids) == (
        "breaker_trip",
        "TRIPPED",
        ["R13_CIRCUIT_BREAKER"],
    )
    assert alerter.kinds(Severity.ALERT) == [propose.BREAKER_TRIPPED_ALERT]
