"""The normalized shapes every model implementation returns (ADR-023 S1, ADR-006 contract).

Messages and tool schemas use the OpenAI chat wire format, which both providers accept through
their compatible endpoints. Tool-call arguments stay the raw string the model produced: the turn
parses them, so a parse failure is a malformed call there (ADR-022 S1), not an adapter error.
"""

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Protocol

Message = Mapping[str, Any]
ToolSchema = Mapping[str, Any]
OnText = Callable[[str], None]


class FinishReason(StrEnum):
    STOP = "stop"
    TOOL_CALLS = "tool_calls"
    LENGTH = "length"
    CONTENT_FILTER = "content_filter"
    OTHER = "other"


class Purpose(StrEnum):
    TURN = "turn"
    SUMMARY = "summary"
    WARRANT = "warrant"  # ADR-029 W01


class UsageCompleteness(StrEnum):
    """How much of what the provider may bill is in ``usage`` (ADR-023 Part 4)."""

    FULL = "full"
    PARTIAL = "partial"  # an earlier attempt failed after its stream had started
    NONE = "none"  # the provider reported no usage: uncounted, never zero


@dataclass(frozen=True)
class Usage:
    input_tokens: int
    output_tokens: int


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    arguments: str
    # Opaque data the provider attached to this call and wants back with it in the next
    # request, as JSON text (Gemini's ``extra_content`` carries its thought signature). Only
    # ever sent back to the same provider: a turn never switches provider (ADR-023 Part 3).
    extra_content: str | None = None


@dataclass(frozen=True)
class ModelResponse:
    text: str
    tool_calls: tuple[ToolCall, ...]
    finish_reason: FinishReason
    usage: Usage | None
    completeness: UsageCompleteness
    provider: str
    model: str
    attempts: int
    prompt_chars: int
    output_chars: int
    first_token_seconds: float | None
    latency_seconds: float


class ChatModel(Protocol):
    async def complete(
        self,
        messages: Sequence[Message],
        tools: Sequence[ToolSchema],
        *,
        deadline: float,
        purpose: Purpose,
        on_text: OnText | None = None,
    ) -> ModelResponse:
        """``deadline`` is an absolute ``time.monotonic()`` value; nothing runs past it."""
        ...
