"""Turning a provider's stream into one normalized response (ADR-023 Part 2, S6).

Text deltas go to the caller's ``on_text`` through ``Delivery``, which isolates the callback:
an exception from it is logged and switches it off, and is never a provider failure. Once any
delta has been delivered, the call must not be retried (the customer would see the answer
start twice); the adapter reads ``Delivery.delivered`` for that.
"""

import logging
from collections.abc import Callable
from dataclasses import dataclass, field

from openai.types.chat import ChatCompletionChunk

from agent.llm.errors import ProviderUnavailable
from agent.llm.types import FinishReason, OnText, ToolCall, Usage

logger = logging.getLogger(__name__)

FINISH_REASONS = {reason.value: reason for reason in FinishReason}


class Delivery:
    def __init__(self, on_text: OnText | None) -> None:
        self._on_text = on_text
        self.delivered = False

    def send(self, delta: str) -> None:
        if self._on_text is None or not delta:
            return
        self.delivered = True
        try:
            self._on_text(delta)
        except Exception:
            logger.warning(
                "on_text raised; text delivery is off for the rest of the call", exc_info=True
            )
            self._on_text = None


@dataclass
class _PartialCall:
    id: str = ""
    name: str = ""
    arguments: list[str] = field(default_factory=list)


class StreamAssembler:
    def __init__(self, *, max_output_chars: int, monotonic: Callable[[], float]) -> None:
        self._max_output_chars = max_output_chars
        self._monotonic = monotonic
        self.first_token_at: float | None = None
        self._text: list[str] = []
        self._calls: dict[int, _PartialCall] = {}
        self._output_chars = 0
        self.finish_reason: FinishReason | None = None
        self.usage: Usage | None = None
        self.started = False  # any content arrived: a failure from here may already be billed

    def feed(self, chunk: ChatCompletionChunk, delivery: Delivery) -> None:
        if chunk.usage is not None:
            self.usage = Usage(chunk.usage.prompt_tokens, chunk.usage.completion_tokens)
        for choice in chunk.choices:
            delta = choice.delta
            if delta.content:
                self._grow(len(delta.content))
                self._text.append(delta.content)
                delivery.send(delta.content)
            for position, call_delta in enumerate(delta.tool_calls or ()):
                index = call_delta.index if call_delta.index is not None else position
                call = self._calls.setdefault(index, _PartialCall())
                if call_delta.id and not call.id:
                    call.id = call_delta.id
                function = call_delta.function
                if function is not None:
                    if function.name and function.name != call.name:
                        # Names normally arrive once; a split name continues the first part.
                        call.name += function.name
                    if function.arguments:
                        self._grow(len(function.arguments))
                        call.arguments.append(function.arguments)
                self._mark_started()
            if choice.finish_reason is not None:
                self.finish_reason = FINISH_REASONS.get(
                    str(choice.finish_reason).lower(), FinishReason.OTHER
                )

    def _mark_started(self) -> None:
        if not self.started:
            self.started = True
            self.first_token_at = self._monotonic()

    def _grow(self, chars: int) -> None:
        self._mark_started()
        self._output_chars += chars
        if self._output_chars > self._max_output_chars:
            raise ProviderUnavailable(
                f"response exceeded {self._max_output_chars} characters; "
                "the output cap wasn't honored"
            )

    @property
    def output_chars(self) -> int:
        return self._output_chars

    def text(self) -> str:
        return "".join(self._text)

    def tool_calls(self) -> tuple[ToolCall, ...]:
        return tuple(
            ToolCall(
                id=call.id or f"call_{index}",
                name=call.name,
                arguments="".join(call.arguments),
            )
            for index, call in sorted(self._calls.items())
        )
