"""The read-only loop through ``run_turn`` (ADR-022 S1 step 3, S2, S4, Part 2, Part 3; ADR-023 S1a).

ScriptedLLM behind the real provider pool and call recording, against Postgres. Every scenario
asserts what the model was shown, not just what it said.
"""

import asyncio
import json
import uuid
from typing import Any

import pytest
from agent_testing.fakes import RecordingAlerter
from agent_testing.scripted_llm import ScriptedLLM, Turn, tool_call
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine
from turn_helpers import SETTINGS, turn_deps
from worker_world import World

from agent.alerts import Severity
from agent.core.lifecycle import RunStatus
from agent.core.replies import ReplyTemplate
from agent.db import order_lookup as order_lookup_db
from agent.llm.errors import ContextTooLong, InputRejected, ProviderUnavailable
from agent.llm.recording import CallRecord
from agent.llm.types import (
    ChatModel,
    FinishReason,
    Message,
    ModelResponse,
    OnText,
    Purpose,
    ToolSchema,
)
from agent.turn import loop
from agent.turn.finish import TryAgainCause, reply_and_wait
from agent.turn.run import PROVIDER_FAILURES_ALERT, TurnOutcome, TurnResult, run_turn
from agent.turn.settings import TurnSettings
from agent.turn.start import TurnIds

BOUND = 15


def after_tools(view: Any) -> bool:
    return bool(view.has_tool_results)


async def turn(
    engine: AsyncEngine,
    world: World,
    conversation: uuid.UUID,
    model: ChatModel,
    *,
    message: str = "where is my order?",
    alerter: RecordingAlerter | None = None,
    records: list[CallRecord] | None = None,
    settings: TurnSettings = SETTINGS,
) -> TurnResult:
    deps = turn_deps(
        engine,
        model,
        alerter=alerter or RecordingAlerter(),
        records=records if records is not None else [],
        settings=settings,
    )
    async with asyncio.timeout(BOUND):
        return await run_turn(deps, conversation_id=conversation, user=world.user, message=message)


def replies(world: World, conversation: uuid.UUID) -> list[dict[str, Any]]:
    return [e.payload for e in world.conversation_events(conversation) if e.kind == "reply"]


# --- Replies and read tools --------------------------------------------------------------------


async def test_a_text_reply_completes_the_run_and_is_recorded(
    world: World, async_runtime_engine: AsyncEngine
) -> None:
    conversation = world.add_conversation()
    model = ScriptedLLM([Turn(text="Happy to help.")])
    records: list[CallRecord] = []

    result = await turn(async_runtime_engine, world, conversation, model, records=records)

    assert result.outcome is TurnOutcome.REPLIED
    assert world.row(result.ids.run_id).status == RunStatus.COMPLETED
    assert replies(world, conversation) == [{"text": "Happy to help."}]
    assert world.conversation(conversation).active_turn_id is None
    (record,) = records
    assert (record.run_id, record.turn_id, record.step) == (
        result.ids.run_id,
        result.ids.turn_id,
        1,
    )
    model.assert_finished()


async def test_read_calls_run_in_order_and_reach_the_model_only_as_tool_messages(
    world: World, async_runtime_engine: AsyncEngine
) -> None:
    order = world.add_order(charged_minor=5_000)
    conversation = world.add_conversation()
    model = ScriptedLLM(
        [
            Turn(
                tool_calls=(
                    tool_call("lookup_order", {"order_id": order}, "c1"),
                    tool_call("lookup_order", {"order_id": "ord_never_minted_9"}, "c2"),
                )
            ),
            Turn(text="It was delivered.", match=after_tools),
        ]
    )

    result = await turn(async_runtime_engine, world, conversation, model)

    assert result.outcome is TurnOutcome.REPLIED
    second = model.calls[1].messages
    assert [m["role"] for m in second[-3:]] == ["assistant", "tool", "tool"]
    assert [m["tool_call_id"] for m in second[-2:]] == ["c1", "c2"]
    assert json.loads(second[-2]["content"])["charged_display"] == "50.00 USD"
    assert json.loads(second[-1]["content"])["error"] == "not_found"
    for message in second:
        if message["role"] in ("user", "system"):
            assert "charged_display" not in str(message["content"])
    assert world.lookups(result.ids.run_id) == [order]


async def test_no_tool_offered_to_the_model_has_an_identity_field(
    world: World, async_runtime_engine: AsyncEngine
) -> None:
    model = ScriptedLLM([Turn(text="ok")])

    await turn(async_runtime_engine, world, world.add_conversation(), model)

    for tool in model.calls[0].tools:
        properties = tool["function"]["parameters"]["properties"]
        assert not {"user", "user_id", "customer", "customer_id", "session_user"} & set(properties)


async def test_a_tool_runs_on_a_connection_with_the_turn_timeouts(
    world: World, async_runtime_engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[tuple[str, str]] = []
    original = order_lookup_db.find_owned_order

    async def reading(conn: AsyncConnection, order_id: str, user: str) -> Any:
        row = (
            await conn.execute(
                text("SELECT current_setting('statement_timeout'), current_setting('lock_timeout')")
            )
        ).one()
        seen.append((row[0], row[1]))
        return await original(conn, order_id, user)

    monkeypatch.setattr("agent.tools.order_lookup.find_owned_order", reading)
    model = ScriptedLLM(
        [
            Turn(tool_calls=(tool_call("lookup_order", {"order_id": "ord_x1"}),)),
            Turn(text="done", match=after_tools),
        ]
    )

    await turn(async_runtime_engine, world, world.add_conversation(), model)

    assert seen == [("5s", "2s")]


# --- Malformed responses and budgets -------------------------------------------------------------


@pytest.mark.parametrize(
    ("bad", "problem"),
    [
        (Turn(tool_calls=(tool_call("issue_refund", {"order_id": "o1"}),)), "unknown tool"),
        (Turn(tool_calls=(tool_call("lookup_order", "{not json"),)), "not valid JSON"),
        (Turn(tool_calls=(tool_call("lookup_order", "[1, 2]"),)), "must be a JSON object"),
        (Turn(tool_calls=(tool_call("lookup_order", {"order": "o1"}),)), "invalid arguments"),
        (
            Turn(
                tool_calls=(tool_call("lookup_order", {"order_id": "o1"}),),
                finish_reason=FinishReason.LENGTH,
            ),
            "cut off",
        ),
    ],
)
async def test_a_malformed_response_runs_nothing_and_gets_an_error_result(
    world: World, async_runtime_engine: AsyncEngine, bad: Turn, problem: str
) -> None:
    conversation = world.add_conversation()
    model = ScriptedLLM([bad, Turn(text="Sorry, let me try again.", match=after_tools)])

    result = await turn(async_runtime_engine, world, conversation, model)

    assert result.outcome is TurnOutcome.REPLIED
    error = json.loads(model.calls[1].messages[-1]["content"])
    assert error["error"] == "not_run" and problem in error["message"]
    assert world.lookups(result.ids.run_id) == []


async def test_an_empty_reply_gets_a_note_and_counts_as_malformed(
    world: World, async_runtime_engine: AsyncEngine
) -> None:
    model = ScriptedLLM([Turn(text=""), Turn(text="Here you go.")])

    result = await turn(async_runtime_engine, world, world.add_conversation(), model)

    assert result.outcome is TurnOutcome.REPLIED
    assert model.calls[1].messages[-1]["content"] == loop.EMPTY_REPLY_NOTE


async def test_the_malformed_budget_plus_one_escalates(
    world: World, async_runtime_engine: AsyncEngine
) -> None:
    conversation = world.add_conversation()
    bad = Turn(tool_calls=(tool_call("issue_refund", {}),), repeat=True)
    model = ScriptedLLM([bad])

    result = await turn(async_runtime_engine, world, conversation, model)

    assert result.outcome is TurnOutcome.ESCALATED
    assert len(model.calls) == SETTINGS.max_malformed + 1
    assert world.row(result.ids.run_id).status == RunStatus.ESCALATED
    assert replies(world, conversation) == [{"template": "needs_person"}]  # no reason disclosed


async def test_the_step_budget_is_exactly_the_configured_calls_then_escalates(
    world: World, async_runtime_engine: AsyncEngine
) -> None:
    lookup = Turn(tool_calls=(tool_call("lookup_order", {"order_id": "ord_loop"}),), repeat=True)
    model = ScriptedLLM([lookup])

    result = await turn(async_runtime_engine, world, world.add_conversation(), model)

    assert result.outcome is TurnOutcome.ESCALATED
    assert len(model.calls) == SETTINGS.max_model_calls


# --- Provider failures (Part 2, ADR-023 S1a) ---------------------------------------------


async def test_a_provider_failure_leaves_the_run_waiting_with_try_again(
    world: World, async_runtime_engine: AsyncEngine
) -> None:
    conversation = world.add_conversation()
    model = ScriptedLLM([Turn(raises=ProviderUnavailable("503"))])

    result = await turn(async_runtime_engine, world, conversation, model)

    assert result.outcome is TurnOutcome.WAITING
    assert world.row(result.ids.run_id).status == RunStatus.AGENT_STEP
    assert replies(world, conversation) == [{"template": "try_again", "cause": "provider"}]
    assert world.conversation(conversation).active_turn_id is None


@pytest.mark.parametrize(
    ("error", "reply", "alert"),
    [
        (InputRejected("filtered"), {"template": "rephrase"}, loop.INPUT_REJECTED_ALERT),
        (
            ContextTooLong("too long"),
            {"template": "try_again", "cause": "provider"},
            loop.CONTEXT_TOO_LONG_ALERT,
        ),
    ],
)
async def test_input_driven_errors_get_their_reply_and_an_alert_never_a_page(
    world: World,
    async_runtime_engine: AsyncEngine,
    error: Exception,
    reply: dict[str, str],
    alert: str,
) -> None:
    conversation = world.add_conversation()
    alerter = RecordingAlerter()

    result = await turn(
        async_runtime_engine,
        world,
        conversation,
        ScriptedLLM([Turn(raises=error)]),  # type: ignore[arg-type]
        alerter=alerter,
    )

    assert result.outcome is TurnOutcome.WAITING
    assert replies(world, conversation) == [reply]
    assert alerter.kinds(Severity.ALERT) == [alert]
    assert alerter.kinds(Severity.PAGE) == []


async def test_repeated_provider_failures_alert_once_the_streak_reaches_the_threshold(
    world: World, async_runtime_engine: AsyncEngine
) -> None:
    conversation = world.add_conversation()
    alerter = RecordingAlerter()
    failing = ScriptedLLM([Turn(raises=ProviderUnavailable("503"), repeat=True)])

    for _ in range(SETTINGS.provider_failure_alert_after - 1):
        await turn(async_runtime_engine, world, conversation, failing, alerter=alerter)
    assert PROVIDER_FAILURES_ALERT not in alerter.kinds(Severity.ALERT)

    await turn(async_runtime_engine, world, conversation, failing, alerter=alerter)
    assert alerter.kinds(Severity.ALERT) == [PROVIDER_FAILURES_ALERT]


async def test_a_lock_timeout_or_an_answer_breaks_the_provider_failure_streak(
    world: World, async_runtime_engine: AsyncEngine
) -> None:
    conversation = world.add_conversation()
    alerter = RecordingAlerter()
    failing = ScriptedLLM([Turn(raises=ProviderUnavailable("503"), repeat=True)])

    first = await turn(async_runtime_engine, world, conversation, failing, alerter=alerter)
    await turn(async_runtime_engine, world, conversation, failing, alerter=alerter)
    # A proposal-time lock timeout (ADR-009) replies try_again too, with cause "lock".
    lock_turn = world.hold_turn(first.ids.run_id)
    lock_ids = TurnIds(conversation, first.ids.run_id, lock_turn)
    await reply_and_wait(
        async_runtime_engine, SETTINGS, lock_ids, ReplyTemplate.TRY_AGAIN, TryAgainCause.LOCK
    )
    await turn(async_runtime_engine, world, conversation, failing, alerter=alerter)

    assert PROVIDER_FAILURES_ALERT not in alerter.kinds(Severity.ALERT)


async def test_a_passed_deadline_waits_without_calling_the_model(
    world: World, async_runtime_engine: AsyncEngine
) -> None:
    conversation = world.add_conversation()
    ticks = iter([0.0] + [10_000.0] * 50)  # the start, then far past the deadline
    model = ScriptedLLM([])
    deps = turn_deps(
        async_runtime_engine,
        model,
        alerter=RecordingAlerter(),
        records=[],
        monotonic=lambda: next(ticks),
    )

    async with asyncio.timeout(BOUND):
        result = await run_turn(deps, conversation_id=conversation, user=world.user, message="hi")

    assert result.outcome is TurnOutcome.WAITING
    assert model.calls == []


# --- The lease, the window and memory ---------------------------------------------------


class TakeoverThenReply:
    """Another turn takes the conversation over while this model is answering."""

    def __init__(self, world: World) -> None:
        self.world = world
        self.run_id: uuid.UUID | None = None

    async def complete(
        self,
        messages: list[Message],
        tools: list[ToolSchema],
        *,
        deadline: float,
        purpose: Purpose,
        on_text: OnText | None = None,
    ) -> ModelResponse:
        assert self.run_id is not None
        await asyncio.to_thread(self.world.hold_turn, self.run_id)
        return await ScriptedLLM([Turn(text="stale answer")]).complete(
            messages, tools, deadline=deadline, purpose=purpose
        )


async def test_a_turn_that_loses_its_lease_mid_loop_writes_no_reply(
    world: World, async_runtime_engine: AsyncEngine
) -> None:
    run = world.add_run(RunStatus.AGENT_STEP)
    conversation = world.row(run).conversation_id
    model = TakeoverThenReply(world)
    model.run_id = run

    result = await turn(async_runtime_engine, world, conversation, model)  # type: ignore[arg-type]

    assert result.outcome is TurnOutcome.LOST
    assert world.row(run).status == RunStatus.AGENT_STEP
    assert replies(world, conversation) == []


async def test_a_later_turn_sees_earlier_messages_but_never_earlier_tool_results(
    world: World, async_runtime_engine: AsyncEngine
) -> None:
    order = world.add_order()
    conversation = world.add_conversation()
    first = ScriptedLLM(
        [
            Turn(tool_calls=(tool_call("lookup_order", {"order_id": order}),)),
            Turn(text="Your order was delivered.", match=after_tools),
        ]
    )
    await turn(async_runtime_engine, world, conversation, first, message="where is it?")
    second = ScriptedLLM([Turn(text="You're welcome.")])

    await turn(async_runtime_engine, world, conversation, second, message="thanks")

    prompt = second.calls[0].messages
    assert [(m["role"], m["content"]) for m in prompt[1:]] == [
        ("user", "where is it?"),
        ("assistant", "Your order was delivered."),
        ("user", "thanks"),
    ]
    assert all(m["role"] != "tool" and "charged_display" not in str(m) for m in prompt)


async def test_the_window_keeps_the_newest_messages_within_its_budget(
    world: World, async_runtime_engine: AsyncEngine, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level("INFO")
    settings = TurnSettings(**{**SETTINGS.__dict__, "window_max_chars": 120})
    conversation = world.add_conversation()
    for index in range(3):
        await turn(
            async_runtime_engine,
            world,
            conversation,
            ScriptedLLM([Turn(text=f"answer {index} " + "x" * 30)]),
            message=f"question {index} " + "y" * 30,
            settings=settings,
        )
    last = ScriptedLLM([Turn(text="ok")])

    await turn(async_runtime_engine, world, conversation, last, message="newest", settings=settings)

    history = last.calls[0].messages[1:]
    assert history[-1] == {"role": "user", "content": "newest"}
    assert sum(len(m["content"]) for m in history) <= 120
    assert "question 0" not in str(history)
    assert any("window_trimmed" in r.getMessage() for r in caplog.records)
