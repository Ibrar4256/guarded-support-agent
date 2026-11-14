"""ScriptedLLM: the strict scripted model behind the ``ChatModel`` interface (ADR-006).

- Strict playback: an unexpected call, or unused turns at ``assert_finished()``, fail the test.
- A turn is positional (the next call) or matched by a predicate over the prompt's *structure*
  (roles, tool names offered, tool results present, the last user message); more than one
  matched turn eligible for a call is an error.
- Every call is recorded, so tests assert what the model was shown.
- Text arrives in configurable chunks through the same ``Delivery`` the adapter uses.
- A turn without token counts is uncounted (``UsageCompleteness.NONE``), never zero.
- Errors are raised as the adapter would raise them after its retries: never by sleeping.

The model gate (ADR-006 layer 1) is ``agent.llm.settings``: ``scripted`` needs ``TESTING=true``.
"""

import copy
import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from agent.llm.adapter import prompt_chars
from agent.llm.assembly import Delivery
from agent.llm.errors import ModelError
from agent.llm.types import (
    FinishReason,
    Message,
    ModelResponse,
    OnText,
    Purpose,
    ToolCall,
    ToolSchema,
    Usage,
    UsageCompleteness,
)


@dataclass(frozen=True)
class PromptView:
    """What a matcher may look at: structure, never our own prompt wording (ADR-006 rule 2)."""

    roles: tuple[str, ...]
    tool_names: tuple[str, ...]
    has_tool_results: bool
    last_user_text: str
    purpose: Purpose

    @classmethod
    def of(
        cls, messages: Sequence[Message], tools: Sequence[ToolSchema], purpose: Purpose
    ) -> "PromptView":
        roles = tuple(str(message.get("role", "")) for message in messages)
        users = [m for m in messages if m.get("role") == "user"]
        last_user = users[-1].get("content", "") if users else ""
        return cls(
            roles=roles,
            tool_names=tuple(str(t.get("function", {}).get("name", "")) for t in tools),
            has_tool_results="tool" in roles,
            last_user_text=last_user if isinstance(last_user, str) else json.dumps(last_user),
            purpose=purpose,
        )


Matcher = Callable[[PromptView], bool]


@dataclass(frozen=True)
class Turn:
    text: str = ""
    tool_calls: tuple[ToolCall, ...] = ()
    chunks: tuple[str, ...] | None = None  # text chunk boundaries; None is one chunk
    raises: ModelError | None = None
    usage: Usage | None = None  # None: uncounted
    finish_reason: FinishReason | None = None  # default: from the content
    match: Matcher | None = None
    repeat: bool = False
    label: str = ""

    def __post_init__(self) -> None:
        if self.chunks is not None and "".join(self.chunks) != self.text:
            raise ValueError(f"turn {self.label!r}: chunks don't join to the text")
        if self.raises is not None and (self.text or self.tool_calls):
            raise ValueError(f"turn {self.label!r}: a raising turn has no content")


def tool_call(
    name: str, arguments: str | Mapping[str, Any], call_id: str | None = None
) -> ToolCall:
    raw = arguments if isinstance(arguments, str) else json.dumps(arguments)
    return ToolCall(id=call_id or f"call_{name}", name=name, arguments=raw)


@dataclass(frozen=True)
class RecordedCall:
    number: int
    messages: tuple[Message, ...]
    tools: tuple[ToolSchema, ...]
    deadline: float
    purpose: Purpose
    view: PromptView


@dataclass
class ScriptedLLM:
    turns: list[Turn]
    provider: str = "scripted"
    model: str = "scripted"
    calls: list[RecordedCall] = field(default_factory=list)
    _used: set[int] = field(default_factory=set)

    async def complete(
        self,
        messages: Sequence[Message],
        tools: Sequence[ToolSchema],
        *,
        deadline: float,
        purpose: Purpose,
        on_text: OnText | None = None,
    ) -> ModelResponse:
        view = PromptView.of(messages, tools, purpose)
        call = RecordedCall(
            number=len(self.calls) + 1,
            messages=tuple(copy.deepcopy(dict(m)) for m in messages),
            tools=tuple(copy.deepcopy(dict(t)) for t in tools),
            deadline=deadline,
            purpose=purpose,
            view=view,
        )
        self.calls.append(call)
        turn = self._take(call)
        if turn.raises is not None:
            raise turn.raises
        delivery = Delivery(on_text)
        for chunk in turn.chunks if turn.chunks is not None else (turn.text,):
            delivery.send(chunk)
        output_chars = len(turn.text) + sum(len(c.arguments) for c in turn.tool_calls)
        return ModelResponse(
            text=turn.text,
            tool_calls=turn.tool_calls,
            finish_reason=turn.finish_reason
            or (FinishReason.TOOL_CALLS if turn.tool_calls else FinishReason.STOP),
            usage=turn.usage,
            completeness=UsageCompleteness.FULL if turn.usage else UsageCompleteness.NONE,
            provider=self.provider,
            model=self.model,
            attempts=1,
            prompt_chars=prompt_chars(messages),
            output_chars=output_chars,
            first_token_seconds=0.0 if output_chars else None,
            latency_seconds=0.0,
        )

    def _take(self, call: RecordedCall) -> Turn:
        unused = [(i, t) for i, t in enumerate(self.turns) if i not in self._used or t.repeat]
        matched = [(i, t) for i, t in unused if t.match is not None and t.match(call.view)]
        if len(matched) > 1:
            labels = ", ".join(repr(t.label or i) for i, t in matched)
            raise AssertionError(f"call {call.number}: ambiguous script, turns {labels} all match")
        if matched:
            chosen = matched[0]
        else:
            positional = [(i, t) for i, t in unused if t.match is None]
            if not positional:
                raise AssertionError(self._unexpected(call))
            chosen = positional[0]
        index, turn = chosen
        if not turn.repeat:
            self._used.add(index)
        return turn

    def _unexpected(self, call: RecordedCall) -> str:
        view = call.view
        return (
            f"unexpected model call {call.number} ({view.purpose}): roles={list(view.roles)}, "
            f"tools offered={list(view.tool_names)}, last user message={view.last_user_text!r}; "
            f"unused turns: {self._unused_labels()}"
        )

    def _unused_labels(self) -> list[str]:
        return [
            t.label or f"#{i}"
            for i, t in enumerate(self.turns)
            if i not in self._used and not t.repeat
        ]

    def assert_finished(self) -> None:
        unused = self._unused_labels()
        if unused:
            raise AssertionError(f"script ended with unused turns: {unused}")
