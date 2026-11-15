"""One call record per model call, success or failure (ADR-023 Part 4).

``RecordingModel`` wraps a model for one turn (or one summary), numbers its calls and hands a
``CallRecord`` to a recorder after each. Recording is best-effort: a failure is logged and never
fails the call. It is deliberately not fenced by the turn lease: a lost turn's calls still cost
money. Records carry counts and timings only, never prompt or response text.
"""

import logging
import time
import uuid
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from decimal import Decimal

from agent.llm.adapter import prompt_chars
from agent.llm.errors import OUTCOME_OK, ModelError, outcome_of
from agent.llm.prices import PriceTable
from agent.llm.types import (
    ChatModel,
    Message,
    ModelResponse,
    OnText,
    Purpose,
    ToolSchema,
    UsageCompleteness,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class CallContext:
    conversation_id: uuid.UUID | None = None
    run_id: uuid.UUID | None = None
    turn_id: uuid.UUID | None = None


@dataclass(frozen=True)
class CallRecord:
    conversation_id: uuid.UUID | None
    run_id: uuid.UUID | None
    turn_id: uuid.UUID | None
    step: int
    purpose: str
    provider: str
    model: str
    attempts: int
    outcome: str
    input_tokens: int | None
    output_tokens: int | None
    usage_completeness: str
    prompt_chars: int
    output_chars: int
    first_token_ms: int | None
    latency_ms: int
    cost_usd: Decimal | None
    price_version: str | None


Recorder = Callable[[CallRecord], Awaitable[None]]


class RecordingModel:
    def __init__(
        self,
        inner: ChatModel,
        *,
        provider: str,
        model: str,
        record: Recorder,
        context: CallContext,
        prices: PriceTable,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._inner = inner
        self._provider = provider
        self._model = model
        self._record = record
        self._context = context
        self._prices = prices
        self._monotonic = monotonic
        self._step = 0

    async def complete(
        self,
        messages: Sequence[Message],
        tools: Sequence[ToolSchema],
        *,
        deadline: float,
        purpose: Purpose,
        on_text: OnText | None = None,
    ) -> ModelResponse:
        self._step += 1
        started = self._monotonic()
        try:
            response = await self._inner.complete(
                messages, tools, deadline=deadline, purpose=purpose, on_text=on_text
            )
        except ModelError as error:
            await self._save(self._failure(error, purpose, messages, started))
            raise
        await self._save(self._success(response, purpose))
        return response

    def _success(self, response: ModelResponse, purpose: Purpose) -> CallRecord:
        usage = response.usage
        cost = self._prices.cost(response.provider, response.model, usage)
        first = response.first_token_seconds
        return CallRecord(
            conversation_id=self._context.conversation_id,
            run_id=self._context.run_id,
            turn_id=self._context.turn_id,
            step=self._step,
            purpose=purpose.value,
            provider=response.provider,
            model=response.model,
            attempts=response.attempts,
            outcome=OUTCOME_OK,
            input_tokens=usage.input_tokens if usage else None,
            output_tokens=usage.output_tokens if usage else None,
            usage_completeness=response.completeness.value,
            prompt_chars=response.prompt_chars,
            output_chars=response.output_chars,
            first_token_ms=None if first is None else _millis(first),
            latency_ms=_millis(response.latency_seconds),
            cost_usd=cost,
            price_version=self._prices.version if cost is not None else None,
        )

    def _failure(
        self, error: ModelError, purpose: Purpose, messages: Sequence[Message], started: float
    ) -> CallRecord:
        # The provider reports no usage for a failed call: uncounted, never zero.
        return CallRecord(
            conversation_id=self._context.conversation_id,
            run_id=self._context.run_id,
            turn_id=self._context.turn_id,
            step=self._step,
            purpose=purpose.value,
            provider=self._provider,
            model=self._model,
            attempts=error.attempts,
            outcome=outcome_of(error),
            input_tokens=None,
            output_tokens=None,
            usage_completeness=UsageCompleteness.NONE.value,
            prompt_chars=prompt_chars(messages),
            output_chars=0,
            first_token_ms=None,
            latency_ms=_millis(self._monotonic() - started),
            cost_usd=None,
            price_version=None,
        )

    async def _save(self, record: CallRecord) -> None:
        try:
            await self._record(record)
        except Exception:
            logger.warning(
                "llm call record lost: run %s step %s (%s)",
                record.run_id,
                record.step,
                record.outcome,
                exc_info=True,
            )


def _millis(seconds: float) -> int:
    return max(0, round(seconds * 1000))
