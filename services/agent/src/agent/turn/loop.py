"""The bounded tool loop inside one turn (ADR-001, ADR-006 rule 6, ADR-022 S1 step 3, S2).

Each model response is one of:
- **no tool calls** with text: the reply;
- **read calls only**, all valid: each runs in order on a connection with the turn's timeouts
  (S3), and its result goes back only as a tool message, never as user or system text;
- **malformed** (an unknown tool, arguments that aren't a JSON object or fail validation, tool
  calls cut off by the output limit, an empty reply): every call gets an error result and
  nothing in the response runs. Past ``TURN_MAX_MALFORMED`` the run escalates.

Every provider call counts toward ``TURN_MAX_MODEL_CALLS`` (S2); past it the run escalates. A
model error ends the loop with the reply ADR-023 S1a gives it. Text that comes with tool calls is
interim: kept in this turn's transcript, never a reply.
"""

import json
import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncEngine

from agent.core.replies import ReplyTemplate
from agent.llm.errors import ContextTooLong, InputRejected, ModelError
from agent.llm.types import ChatModel, FinishReason, ModelResponse, Purpose, ToolCall
from agent.tools.contract import ToolArgs, ToolContext, canonical
from agent.turn.finish import TryAgainCause
from agent.turn.settings import TurnSettings
from agent.turn.start import TurnIds
from agent.turn.toolconn import tool_connection
from agent.turn.tools import READ_TOOLS, ReadTool

logger = logging.getLogger(__name__)

CONTEXT_TOO_LONG_ALERT = "turn_context_too_long"
INPUT_REJECTED_ALERT = "turn_input_rejected"
EMPTY_REPLY_NOTE = (
    "[automated] Your last response was empty. Reply to the customer, or call a tool."
)


@dataclass(frozen=True)
class Reply:
    text: str


@dataclass(frozen=True)
class Wait:
    template: ReplyTemplate
    cause: TryAgainCause | None
    alert: str | None = None


@dataclass(frozen=True)
class Escalate:
    reason: str


LoopOutcome = Reply | Wait | Escalate

PROVIDER_FAILURE = Wait(ReplyTemplate.TRY_AGAIN, TryAgainCause.PROVIDER)


class Malformed(Exception):
    pass


@dataclass(frozen=True)
class LoopContext:
    engine: AsyncEngine
    settings: TurnSettings
    ids: TurnIds
    user: str
    model: ChatModel
    deadline: float
    monotonic: Callable[[], float]


def outcome_for(error: ModelError) -> Wait:
    """ADR-023 S1a. ``InvalidRequest`` pages through the provider pool; here it's a try-again."""
    if isinstance(error, InputRejected):
        return Wait(ReplyTemplate.REPHRASE, None, INPUT_REJECTED_ALERT)
    if isinstance(error, ContextTooLong):
        return Wait(ReplyTemplate.TRY_AGAIN, TryAgainCause.PROVIDER, CONTEXT_TOO_LONG_ALERT)
    return PROVIDER_FAILURE


def parse_calls(response: ModelResponse) -> list[tuple[ToolCall, ReadTool, ToolArgs]]:
    """The response's read calls, validated; ``Malformed`` names the first problem."""
    if response.finish_reason is FinishReason.LENGTH:
        raise Malformed("the response was cut off by the output limit")
    parsed = []
    for call in response.tool_calls:
        tool = READ_TOOLS.get(call.name)
        if tool is None:
            raise Malformed(f"unknown tool {call.name!r}")
        try:
            raw = json.loads(call.arguments)
        except json.JSONDecodeError as exc:
            raise Malformed(f"{call.name}: arguments are not valid JSON") from exc
        if not isinstance(raw, dict):
            raise Malformed(f"{call.name}: arguments must be a JSON object")
        try:
            args = tool.args.model_validate(raw)
        except ValidationError as exc:
            fields = ", ".join(".".join(map(str, e["loc"])) or "arguments" for e in exc.errors())
            raise Malformed(f"{call.name}: invalid arguments ({fields})") from exc
        parsed.append((call, tool, args))
    return parsed


def assistant_message(response: ModelResponse) -> dict[str, Any]:
    return {
        "role": "assistant",
        "content": response.text or None,
        "tool_calls": [
            {
                "id": call.id,
                "type": "function",
                "function": {"name": call.name, "arguments": call.arguments},
            }
            for call in response.tool_calls
        ],
    }


def tool_message(call: ToolCall, content: str) -> dict[str, Any]:
    return {"role": "tool", "tool_call_id": call.id, "content": content}


async def run_read_tool(ctx: LoopContext, tool: ReadTool, args: ToolArgs) -> str:
    async with tool_connection(ctx.engine, ctx.settings) as conn:
        tool_ctx = ToolContext(
            session_user=ctx.user,
            conversation_id=ctx.ids.conversation_id,
            turn_id=ctx.ids.turn_id,
            run_id=ctx.ids.run_id,
            conn=conn,
        )
        return await tool.run(tool_ctx, args)


async def run_loop(ctx: LoopContext, messages: list[dict[str, Any]]) -> LoopOutcome:
    tools = [tool.schema() for tool in READ_TOOLS.values()]
    malformed = 0
    for _ in range(ctx.settings.max_model_calls):
        if ctx.deadline - ctx.monotonic() <= 0:
            return PROVIDER_FAILURE
        try:
            response = await ctx.model.complete(
                messages, tools, deadline=ctx.deadline, purpose=Purpose.TURN
            )
        except ModelError as error:
            logger.warning("turn %s: model call failed: %s", ctx.ids.turn_id, type(error).__name__)
            return outcome_for(error)
        try:
            if not response.tool_calls:
                if not response.text.strip():
                    raise Malformed("empty reply")
                if response.finish_reason is FinishReason.LENGTH:
                    logger.info("turn %s: reply truncated by the output limit", ctx.ids.turn_id)
                return Reply(response.text)
            calls = parse_calls(response)
        except Malformed as problem:
            malformed += 1
            logger.info("turn %s: malformed response %d: %s", ctx.ids.turn_id, malformed, problem)
            if malformed > ctx.settings.max_malformed:
                return Escalate("malformed_budget")
            messages.extend(correction(response, str(problem)))
            continue
        messages.append(assistant_message(response))
        for call, tool, args in calls:
            messages.append(tool_message(call, await run_read_tool(ctx, tool, args)))
    return Escalate("step_budget")


def correction(response: ModelResponse, problem: str) -> list[dict[str, Any]]:
    """What the model sees after a malformed response: every call answered, nothing run."""
    if not response.tool_calls:
        return [{"role": "system", "content": EMPTY_REPLY_NOTE}]
    error = canonical({"error": "not_run", "message": f"Nothing in that response ran: {problem}."})
    return [
        assistant_message(response),
        *(tool_message(call, error) for call in response.tool_calls),
    ]
