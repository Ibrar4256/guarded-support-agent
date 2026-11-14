"""The OpenAI-compatible adapter (ADR-023): always streams, retries only inside the deadline.

The SDK's own retries are off (``max_retries=0``) so this module holds the only retry policy.
One call to ``complete`` is one model call for the turn's budget, however many attempts it took.
"""

import asyncio
import random
import time
from collections.abc import Awaitable, Callable, Sequence
from typing import Any, cast

import httpx2
from openai import AsyncOpenAI, omit

from agent.llm.assembly import Delivery, StreamAssembler
from agent.llm.classify import classify
from agent.llm.errors import (
    RETRYABLE,
    ModelError,
    ProviderTimeout,
    ProviderUnavailable,
    RateLimited,
)
from agent.llm.providers import PROVIDERS, Provider
from agent.llm.settings import LLMSettings, ProviderChoice
from agent.llm.types import (
    FinishReason,
    Message,
    ModelResponse,
    OnText,
    Purpose,
    ToolSchema,
    UsageCompleteness,
)

Sleep = Callable[[float], Awaitable[None]]
Jitter = Callable[[float], float]


def full_jitter(ceiling: float) -> float:
    return random.uniform(0, ceiling)


class OpenAICompatibleModel:
    def __init__(
        self,
        choice: ProviderChoice,
        settings: LLMSettings,
        *,
        http_client: httpx2.AsyncClient | None = None,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Sleep = asyncio.sleep,
        jitter: Jitter = full_jitter,
    ) -> None:
        self.provider: Provider = PROVIDERS[choice.provider]
        self.model = choice.model
        self._settings = settings
        self._monotonic = monotonic
        self._sleep = sleep
        self._jitter = jitter
        self._client = AsyncOpenAI(
            api_key=choice.api_key,
            base_url=self.provider.base_url,
            max_retries=0,
            http_client=http_client,
        )

    async def complete(
        self,
        messages: Sequence[Message],
        tools: Sequence[ToolSchema],
        *,
        deadline: float,
        purpose: Purpose,
        on_text: OnText | None = None,
    ) -> ModelResponse:
        started = self._monotonic()
        delivery = Delivery(on_text)
        attempts = 0
        billed_before = False  # an earlier attempt failed after its stream had started
        while True:
            attempts += 1
            assembler = StreamAssembler(
                max_output_chars=self._settings.max_output_chars, monotonic=self._monotonic
            )
            attempt_started = self._monotonic()
            try:
                remaining = deadline - attempt_started
                if remaining <= 0:
                    raise ProviderTimeout("the deadline passed before the attempt started")
                async with asyncio.timeout(remaining):
                    await self._stream(messages, tools, assembler, delivery)
            except Exception as exc:
                error = classify(exc, self.provider.markers)
                billed_before = billed_before or assembler.started
                delay = self._retry_delay(error, attempts, delivery, deadline)
                if delay is None:
                    raise error from exc
                await self._sleep(delay)
                continue
            if assembler.finish_reason is None:
                # The stream closed without finishing: a drop, handled like any other.
                billed_before = billed_before or assembler.started
                error = ProviderUnavailable("the stream ended without a finish reason")
                delay = self._retry_delay(error, attempts, delivery, deadline)
                if delay is None:
                    raise error
                await self._sleep(delay)
                continue
            return self._response(
                assembler, messages, attempts, billed_before, started, attempt_started
            )

    def _retry_delay(
        self, error: ModelError, attempts: int, delivery: Delivery, deadline: float
    ) -> float | None:
        """Seconds to wait before the next attempt, or None to give up now (ADR-023 Part 3)."""
        if not isinstance(error, RETRYABLE) or delivery.delivered:
            return None
        if attempts >= self._settings.max_attempts:
            return None
        if isinstance(error, RateLimited) and error.retry_after is not None:
            delay = error.retry_after
        else:
            base = self._settings.backoff_base.total_seconds()
            cap = self._settings.backoff_cap.total_seconds()
            delay = self._jitter(min(cap, base * 2 ** (attempts - 1)))
        time_left = deadline - self._monotonic()
        if delay + self._settings.min_attempt.total_seconds() > time_left:
            return None
        return delay

    async def _stream(
        self,
        messages: Sequence[Message],
        tools: Sequence[ToolSchema],
        assembler: StreamAssembler,
        delivery: Delivery,
    ) -> None:
        stream = await self._client.chat.completions.create(
            model=self.model,
            # Our Mapping-typed wire format is exactly the SDK's TypedDicts; cast at the edge.
            messages=cast(Any, list(messages)),
            tools=cast(Any, list(tools)) if tools else omit,
            stream=True,
            stream_options={"include_usage": True},
            max_tokens=self._settings.max_output_tokens,
            temperature=self._settings.temperature,
        )
        async with stream:
            async for chunk in stream:
                assembler.feed(chunk, delivery)

    def _response(
        self,
        assembler: StreamAssembler,
        messages: Sequence[Message],
        attempts: int,
        billed_before: bool,
        started: float,
        attempt_started: float,
    ) -> ModelResponse:
        finish = assembler.finish_reason or FinishReason.OTHER
        if assembler.usage is None:
            completeness = UsageCompleteness.NONE
        elif billed_before:
            completeness = UsageCompleteness.PARTIAL
        else:
            completeness = UsageCompleteness.FULL
        first = assembler.first_token_at
        return ModelResponse(
            text=assembler.text(),
            tool_calls=assembler.tool_calls(),
            finish_reason=finish,
            usage=assembler.usage,
            completeness=completeness,
            provider=self.provider.name,
            model=self.model,
            attempts=attempts,
            prompt_chars=prompt_chars(messages),
            output_chars=assembler.output_chars,
            first_token_seconds=None if first is None else first - attempt_started,
            latency_seconds=self._monotonic() - started,
        )


def prompt_chars(messages: Sequence[Message]) -> int:
    """Characters of message content: a fact recorded beside tokens (ADR-023 Part 4)."""
    total = 0
    for message in messages:
        content: Any = message.get("content")
        if isinstance(content, str):
            total += len(content)
        for call in message.get("tool_calls") or ():
            total += len(str(call.get("function", {}).get("arguments", "")))
    return total
