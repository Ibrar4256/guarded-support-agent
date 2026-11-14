"""Wire-level helpers for the adapter's tests: OpenAI-format stream chunks served through an
``httpx2.MockTransport`` (openai 3.x uses httpx2, so respx can't intercept it; ADR-006 pointer).

``Wire`` is strict like ScriptedLLM: a request with nothing scripted fails the test.
"""

import asyncio
import json
from collections import deque
from collections.abc import AsyncIterator, Callable, Iterable
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

import httpx2

from agent.llm.adapter import OpenAICompatibleModel
from agent.llm.settings import LLMSettings, ProviderChoice

Responder = Callable[[httpx2.Request], httpx2.Response]


def settings(**overrides: Any) -> LLMSettings:
    values: dict[str, Any] = {
        "primary": ProviderChoice("groq", "test-model", "test-key"),
        "fallback": None,
        "max_output_tokens": 256,
        "max_output_chars": 10_000,
        "temperature": 0.2,
        "max_attempts": 3,
        "backoff_base": timedelta(seconds=0.5),
        "backoff_cap": timedelta(seconds=8),
        "min_attempt": timedelta(seconds=5),
        "quota_cooldown": timedelta(seconds=900),
    }
    values.update(overrides)
    return LLMSettings(**values)


def chunk(
    *,
    content: str | None = None,
    tool_calls: list[dict[str, Any]] | None = None,
    finish: str | None = None,
    usage: tuple[int, int] | None = None,
    choices: bool = True,
) -> dict[str, Any]:
    delta: dict[str, Any] = {}
    if content is not None:
        delta["content"] = content
    if tool_calls is not None:
        delta["tool_calls"] = tool_calls
    body: dict[str, Any] = {
        "id": "chatcmpl-test",
        "object": "chat.completion.chunk",
        "created": 0,
        "model": "test-model",
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish}] if choices else [],
    }
    if usage is not None:
        body["usage"] = {
            "prompt_tokens": usage[0],
            "completion_tokens": usage[1],
            "total_tokens": sum(usage),
        }
    return body


def call_delta(
    index: int, *, call_id: str | None = None, name: str | None = None, arguments: str | None = None
) -> dict[str, Any]:
    function: dict[str, Any] = {}
    if name is not None:
        function["name"] = name
    if arguments is not None:
        function["arguments"] = arguments
    delta: dict[str, Any] = {"index": index, "function": function}
    if call_id is not None:
        delta.update(id=call_id, type="function")
    return delta


def usage_chunk(prompt: int, completion: int) -> dict[str, Any]:
    return chunk(usage=(prompt, completion), choices=False)


def sse(chunks: Iterable[dict[str, Any]], *, done: bool = True) -> bytes:
    lines = [f"data: {json.dumps(c)}\n\n" for c in chunks]
    if done:
        lines.append("data: [DONE]\n\n")
    return "".join(lines).encode()


def stream_ok(chunks: Iterable[dict[str, Any]]) -> Responder:
    body = sse(chunks)
    return lambda request: httpx2.Response(
        200, headers={"content-type": "text/event-stream"}, content=body
    )


def stream_then(chunks: Iterable[dict[str, Any]], failure: BaseException | None) -> Responder:
    """Sends ``chunks``, then raises ``failure`` mid-stream, or stalls forever when None."""
    body = sse(chunks, done=False)

    async def parts() -> AsyncIterator[bytes]:
        yield body
        if failure is None:
            await asyncio.Event().wait()
        else:
            raise failure

    return lambda request: httpx2.Response(
        200, headers={"content-type": "text/event-stream"}, content=parts()
    )


def status(code: int, body: dict[str, Any], headers: dict[str, str] | None = None) -> Responder:
    return lambda request: httpx2.Response(code, json=body, headers=headers or {})


def raises(make: Callable[[httpx2.Request], BaseException]) -> Responder:
    def respond(request: httpx2.Request) -> httpx2.Response:
        raise make(request)

    return respond


def error_body(message: str, *, code: str | None = None, kind: str | None = None) -> dict[str, Any]:
    return {"error": {"message": message, "type": kind, "code": code}}


@dataclass
class Wire:
    responders: deque[Responder] = field(default_factory=deque)
    requests: list[dict[str, Any]] = field(default_factory=list)

    def then(self, *responders: Responder) -> "Wire":
        self.responders.extend(responders)
        return self

    def handle(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(json.loads(request.content))
        if not self.responders:
            raise AssertionError(f"unexpected provider request {len(self.requests)}")
        return self.responders.popleft()(request)

    def client(self) -> httpx2.AsyncClient:
        return httpx2.AsyncClient(transport=httpx2.MockTransport(self.handle))


@dataclass
class FakeClock:
    now: float = 0.0
    sleeps: list[float] = field(default_factory=list)

    def monotonic(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


def adapter(
    wire: Wire, clock: FakeClock | None = None, **setting_overrides: Any
) -> OpenAICompatibleModel:
    llm_settings = settings(**setting_overrides)
    if clock is None:
        return OpenAICompatibleModel(llm_settings.primary, llm_settings, http_client=wire.client())
    return OpenAICompatibleModel(
        llm_settings.primary,
        llm_settings,
        http_client=wire.client(),
        monotonic=clock.monotonic,
        sleep=clock.sleep,
        jitter=lambda ceiling: ceiling,  # deterministic: the full backoff
    )
