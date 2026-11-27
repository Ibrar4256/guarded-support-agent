"""The warrant step through the turn, with the real check and a scripted warrant model
(ADR-029 S2, W01/W02): the verdict decides between an automatic approval and review, it's
bound to its proposal, every failure is a review, and the call holds no customer lock."""

import asyncio
import json
import time
import uuid
from collections.abc import Sequence
from dataclasses import replace
from typing import Any

import pytest
from agent_testing.fakes import RecordingAlerter
from agent_testing.scripted_llm import ScriptedLLM, Turn, tool_call, warrant_turn
from sqlalchemy import Engine, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine
from turn_helpers import SETTINGS, turn_deps
from worker_harness import POLICY
from worker_world import World

from agent.core.policy import RefundProposal, WarrantStatus
from agent.llm.errors import ProviderUnavailable
from agent.llm.recording import CallRecord
from agent.llm.types import ChatModel, Message, ModelResponse, OnText, Purpose, ToolSchema
from agent.turn.propose import WARRANT_MISMATCH_ALERT
from agent.turn.run import TurnOutcome, TurnResult, run_turn
from agent.turn.warrant_check import (
    WarrantEnv,
    WarrantRequest,
    bound_verdict,
    check_warrant,
)

BOUND = 15
WIDE = replace(SETTINGS, message_max_chars=500)
COMPLAINT = "{order} arrived with the lid cracked in half, please refund 20.00"


def refund_script(order: str, *extra: Turn) -> ScriptedLLM:
    refund = {"order_id": order, "amount_minor": 2_000, "currency": "USD"}
    return ScriptedLLM(
        [
            Turn(
                tool_calls=(tool_call("lookup_order", {"order_id": order}, "c1"),),
                match=lambda v: v.purpose is Purpose.TURN and not v.has_tool_results,
            ),
            Turn(
                tool_calls=(tool_call("propose_refund", refund, "c2"),),
                match=lambda v: v.purpose is Purpose.TURN and v.has_tool_results,
            ),
            *extra,
        ]
    )


async def turn(
    engine: AsyncEngine,
    world: World,
    conversation: uuid.UUID,
    model: ChatModel,
    message: str,
    *,
    records: list[CallRecord] | None = None,
    alerter: RecordingAlerter | None = None,
    warrant: Any = check_warrant,
) -> TurnResult:
    deps = turn_deps(
        engine,
        model,
        alerter=alerter or RecordingAlerter(),
        records=records if records is not None else [],
        settings=WIDE,
        policy=POLICY,
        warrant=warrant,
    )
    async with asyncio.timeout(BOUND):
        return await run_turn(deps, conversation_id=conversation, user=world.user, message=message)


def decision(world: World, result: TurnResult) -> Any:
    (row,) = world.decisions(result.ids.run_id)
    return row


async def test_a_warranted_complaint_is_approved_and_the_verdict_is_logged(
    world: World, async_runtime_engine: AsyncEngine
) -> None:
    order = world.add_order(charged_minor=8_000)
    records: list[CallRecord] = []
    model = refund_script(order, warrant_turn(order, "the lid cracked in half"))

    result = await turn(
        async_runtime_engine,
        world,
        world.add_conversation(),
        model,
        COMPLAINT.format(order=order),
        records=records,
    )

    assert result.outcome is TurnOutcome.APPROVED
    row = decision(world, result)
    assert (row.outcome, row.rule_ids) == ("AUTO", [])
    warrant = row.input_snapshot["warrant"]
    assert (warrant["status"], warrant["problem"]) == ("warranted", "damaged")
    assert warrant["action_id"] == world.row(result.ids.run_id).action_id
    assert warrant["model"] == "scripted:scripted-model"
    assert [r.purpose for r in records] == ["turn", "turn", "warrant"]
    assert "cracked" not in json.dumps(row.input_snapshot)  # the quote is never stored


@pytest.mark.parametrize(
    ("answer", "rule"),
    [
        ({"warranted": False, "problem": "none"}, "W01_NOT_WARRANTED"),
        ({"problem": "none"}, "W01_NOT_WARRANTED"),
        ({"order": "ord_another"}, "W01_NOT_WARRANTED"),
        ({"quote": "it arrived broken"}, "W01_NOT_WARRANTED"),
        ({"problem": "not_received"}, "W02_NOT_RECEIVED_BUT_DELIVERED"),
    ],
    ids=["not-warranted", "no-problem", "other-order", "quote-not-theirs", "not-received"],
)
async def test_a_verdict_that_doesnt_hold_sends_the_refund_to_review(
    world: World, async_runtime_engine: AsyncEngine, answer: dict[str, Any], rule: str
) -> None:
    order = world.add_order(charged_minor=8_000)
    warrant = warrant_turn(
        answer.get("order", order),
        answer.get("quote", "the lid cracked in half"),
        problem=answer.get("problem", "damaged"),
        warranted=answer.get("warranted", True),
    )

    result = await turn(
        async_runtime_engine,
        world,
        world.add_conversation(),
        refund_script(order, warrant),
        COMPLAINT.format(order=order),
    )

    assert result.outcome is TurnOutcome.AWAITING_REVIEW
    assert (decision(world, result).outcome, decision(world, result).rule_ids) == ("REVIEW", [rule])


async def test_a_quote_only_the_assistant_wrote_is_unproven_and_never_shown_to_the_judge(
    world: World, async_runtime_engine: AsyncEngine
) -> None:
    """The model is talked into saying "it arrived broken"; the customer never wrote it."""
    order = world.add_order(charged_minor=8_000)
    conversation = world.add_conversation()
    first = ScriptedLLM([Turn(text="So the order arrived broken, is that right?")])
    await turn(async_runtime_engine, world, conversation, first, f"what about {order}?")
    model = refund_script(order, warrant_turn(order, "the order arrived broken"))

    result = await turn(async_runtime_engine, world, conversation, model, "yes, refund it")

    assert decision(world, result).rule_ids == ["W01_NOT_WARRANTED"]
    assert decision(world, result).input_snapshot["warrant"]["status"] == "unproven_quote"
    (call,) = [c for c in model.calls if c.purpose is Purpose.WARRANT]
    prompt = "\n".join(str(m["content"]) for m in call.messages)
    assert "arrived broken" not in prompt  # the assistant's words aren't evidence
    assert "yes, refund it" in prompt and f"what about {order}?" in prompt
    assert "charged_minor" not in prompt and "8000" not in prompt  # no tool results, no amount


async def test_a_provider_failure_is_unavailable_and_goes_to_review(
    world: World, async_runtime_engine: AsyncEngine
) -> None:
    order = world.add_order(charged_minor=8_000)
    failing = Turn(raises=ProviderUnavailable("503"), match=lambda v: v.purpose is Purpose.WARRANT)

    result = await turn(
        async_runtime_engine,
        world,
        world.add_conversation(),
        refund_script(order, failing),
        COMPLAINT.format(order=order),
    )

    assert result.outcome is TurnOutcome.AWAITING_REVIEW
    row = decision(world, result)
    assert (row.outcome, row.rule_ids) == ("REVIEW", ["W01_NOT_WARRANTED"])
    assert row.input_snapshot["warrant"]["status"] == "unavailable"


async def test_with_less_time_left_than_its_deadline_the_check_is_skipped(
    world: World, async_runtime_engine: AsyncEngine
) -> None:
    proposal = RefundProposal("act_skip", world.add_order(), 2_000, "USD")
    strict = ScriptedLLM([])  # any call fails the test
    deps = turn_deps(async_runtime_engine, strict, alerter=RecordingAlerter(), records=[])
    env = WarrantEnv(
        deps.engine, WIDE, deps.pool, deps.record, deps.prices, time.monotonic, lambda: "0" * 16
    )
    left = WIDE.warrant_deadline.total_seconds() - 1
    request = WarrantRequest(
        uuid.uuid4(), uuid.uuid4(), uuid.uuid4(), world.user, proposal, time.monotonic() + left
    )

    verdict = await check_warrant(env, request)

    assert strict.calls == []  # no call was even tried
    assert verdict == bound_verdict(proposal, WarrantStatus.UNAVAILABLE)


class LockProbe:
    """Delegates every call; during the warrant call, tries the customer lock from another
    connection without waiting. It gets it only if the turn doesn't hold it."""

    def __init__(self, inner: ScriptedLLM, owner: Engine, user: str) -> None:
        self.inner, self.owner, self.user = inner, owner, user
        self.lock_free: bool | None = None

    def _try_lock(self) -> bool:
        with self.owner.connect() as conn, conn.begin():
            try:
                conn.execute(
                    text("SELECT id FROM customers WHERE id = :u FOR NO KEY UPDATE NOWAIT"),
                    {"u": self.user},
                )
            except DBAPIError:
                return False
            return True

    async def complete(
        self,
        messages: Sequence[Message],
        tools: Sequence[ToolSchema],
        *,
        deadline: float,
        purpose: Purpose,
        on_text: OnText | None = None,
    ) -> ModelResponse:
        if purpose is Purpose.WARRANT:
            self.lock_free = await asyncio.to_thread(self._try_lock)
        return await self.inner.complete(
            messages, tools, deadline=deadline, purpose=purpose, on_text=on_text
        )


async def test_the_warrant_call_holds_no_customer_lock(
    world: World, async_runtime_engine: AsyncEngine
) -> None:
    order = world.add_order(charged_minor=8_000)
    probe = LockProbe(
        refund_script(order, warrant_turn(order, "the lid cracked in half")),
        world.owner,
        world.user,
    )

    result = await turn(
        async_runtime_engine, world, world.add_conversation(), probe, COMPLAINT.format(order=order)
    )

    assert result.outcome is TurnOutcome.APPROVED
    assert probe.lock_free is True


async def test_a_verdict_for_another_proposal_fires_w01_and_alerts(
    world: World, async_runtime_engine: AsyncEngine
) -> None:
    order = world.add_order(charged_minor=8_000)
    alerter = RecordingAlerter()

    async def misbound(env: WarrantEnv, request: WarrantRequest) -> Any:
        other = replace(request.proposal, action_id="act_someone_else")
        return bound_verdict(other, WarrantStatus.WARRANTED, problem="damaged")

    result = await turn(
        async_runtime_engine,
        world,
        world.add_conversation(),
        refund_script(order),
        COMPLAINT.format(order=order),
        alerter=alerter,
        warrant=misbound,
    )

    assert decision(world, result).rule_ids == ["W01_NOT_WARRANTED"]
    assert [r.kind for r in alerter.raised] == [WARRANT_MISMATCH_ALERT]
