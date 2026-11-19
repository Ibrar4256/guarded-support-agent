"""``python -m agent_testing.capture``: real provider responses saved as fixtures (ADR-025 S4, S5).

    python -m agent_testing.capture --model groq:openai/gpt-oss-120b [--model ...] \\
        --kind tool_call|auth --out tests/unit/llm_fixtures

- ``tool_call``: one turn request (the real system prompt and turn tools, a synthetic customer
  asking about an order). The fixture's kind is what came back: ``tool_call``, or ``text`` when
  the model answered without a tool, which proves nothing for S4.
- ``auth``: the same request with a deliberately invalid key, to pin the provider's
  auth-failure body; it costs no quota.

One attempt per request (no retries), so the fixture is exactly one response. Only synthetic
data is sent (S11). Refuses ``APP_ENV=production``. Never writes a key: the request isn't
stored, and of the response only the status, content type, rate-limit headers and body are.
Read every fixture before committing it.
"""

import argparse
import asyncio
import dataclasses
import json
import os
import re
import sys
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx2

from agent.llm.adapter import OpenAICompatibleModel
from agent.llm.errors import ModelError
from agent.llm.settings import LLMSettings, ProviderChoice
from agent.llm.types import ModelResponse, Purpose
from agent.turn.loop import turn_tools
from agent.turn.prompt import MessageEvent, build_messages, new_nonce, window
from agent_testing.env import credentials, example_llm_env

MESSAGE = "Hi, can you check on my order ord_demo_1001? It still hasn't arrived."
INVALID_KEY = "invalid-key-for-fixture-capture"
DEADLINE_SECONDS = 60
KINDS = ("tool_call", "auth")
DROPPED_HEADERS = {"content-encoding", "content-length", "transfer-encoding", "set-cookie"}
KEPT_HEADER = re.compile(r"(x-)?ratelimit-.*|retry-after", re.IGNORECASE)
# A key echoed back masked, e.g. "Incorrect API key provided: ab12cd*****9f0e".
MASKED_KEY = re.compile(r"[A-Za-z0-9_-]{2,}\*{3,}[A-Za-z0-9_-]{2,}")
REDACTED = "<redacted>"


class CaptureRefused(RuntimeError):
    pass


@dataclass
class Captured:
    status: int
    content_type: str
    headers: dict[str, str]
    body: str


@dataclass
class RecordingTransport(httpx2.AsyncBaseTransport):
    """Reads each response in full, keeps a copy, and hands the client an identical one."""

    inner: httpx2.AsyncBaseTransport = field(default_factory=httpx2.AsyncHTTPTransport)
    captured: list[Captured] = field(default_factory=list)

    async def handle_async_request(self, request: httpx2.Request) -> httpx2.Response:
        response = await self.inner.handle_async_request(request)
        body = await response.aread()  # decoded: the copy must not claim an encoding
        headers = {k: v for k, v in response.headers.items() if k.lower() not in DROPPED_HEADERS}
        self.captured.append(
            Captured(
                status=response.status_code,
                content_type=response.headers.get("content-type", ""),
                headers={k.lower(): v for k, v in headers.items() if KEPT_HEADER.fullmatch(k)},
                body=body.decode("utf-8", errors="replace"),
            )
        )
        return httpx2.Response(response.status_code, headers=headers, content=body, request=request)


def capture_env(environ: Mapping[str, str], model: str) -> dict[str, str]:
    return {**example_llm_env(turn_models=model, summary_models=model), **credentials(environ)}


def turn_request() -> list[dict[str, Any]]:
    events = [MessageEvent("customer_message", {"text": MESSAGE}, 1)]
    return build_messages(window(events, max_chars=24_000), nonce=new_nonce())


async def capture(
    environ: Mapping[str, str],
    model: str,
    kind: str,
    *,
    transport: httpx2.AsyncBaseTransport | None = None,
) -> dict[str, Any]:
    """One request; returns the fixture (not yet written)."""
    if environ.get("APP_ENV") == "production":
        raise CaptureRefused("fixture capture never runs with APP_ENV=production")
    if kind not in KINDS:
        raise CaptureRefused(f"kind must be one of {', '.join(KINDS)}")
    settings = LLMSettings.from_env(capture_env(environ, model))
    settings = dataclasses.replace(settings, max_attempts=1)
    choice: ProviderChoice = settings.models[Purpose.TURN][0]
    if kind == "auth":
        choice = dataclasses.replace(choice, api_key=INVALID_KEY)
    recorder = RecordingTransport() if transport is None else RecordingTransport(inner=transport)
    async with httpx2.AsyncClient(transport=recorder) as client:
        adapter = OpenAICompatibleModel(choice, settings, http_client=client)
        started = time.monotonic()
        response: ModelResponse | None = None
        error: ModelError | None = None
        try:
            response = await adapter.complete(
                turn_request(),
                turn_tools(),
                deadline=started + DEADLINE_SECONDS,
                purpose=Purpose.TURN,
            )
        except ModelError as failed:
            error = failed
    if len(recorder.captured) != 1:
        raise CaptureRefused(f"expected one response, got {len(recorder.captured)}")
    got = recorder.captured[0]
    secrets = [value for value in credentials(environ).values() if len(value) >= 8]
    return {
        "provider": choice.provider,
        "model": choice.model,
        "kind": fixture_kind(kind, response),
        "source": "captured",
        "captured_on": datetime.now(UTC).date().isoformat(),
        "response": {
            "status": got.status,
            "content_type": got.content_type,
            "headers": got.headers,
            "body": redact(got.body, secrets),
        },
        "adapter": summary(response, error),
    }


def redact(text: str, secrets: Sequence[str]) -> str:
    """Providers may echo a key, masked or whole, in an auth error; neither is written."""
    for secret in secrets:
        text = text.replace(secret, REDACTED)
    return MASKED_KEY.sub(REDACTED, text)


def fixture_kind(requested: str, response: ModelResponse | None) -> str:
    if requested == "auth":
        return "auth"
    if response is None:
        return "error"
    return "tool_call" if response.tool_calls else "text"


def summary(response: ModelResponse | None, error: ModelError | None) -> dict[str, Any]:
    """What our adapter made of it, for the reader; the tests re-derive it from the body."""
    if response is None:
        return {"error": type(error).__name__ if error else None}
    return {
        "tool_calls": [call.name for call in response.tool_calls],
        "finish_reason": response.finish_reason.value,
        "usage": None if response.usage is None else dataclasses.asdict(response.usage),
        "latency_seconds": round(response.latency_seconds, 3),
    }


def fixture_path(out: Path, provider: str, model: str, kind: str) -> Path:
    slug = re.sub(r"[^A-Za-z0-9._-]", "_", model)
    return out / provider / slug / f"{kind}.json"


def main(argv: Sequence[str], environ: Mapping[str, str]) -> int:
    parser = argparse.ArgumentParser(prog="python -m agent_testing.capture")
    parser.add_argument("--model", action="append", required=True, help="provider:model")
    parser.add_argument("--kind", choices=KINDS, default="tool_call")
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)
    failed = 0
    for model in args.model:
        try:
            fixture = asyncio.run(capture(environ, model, args.kind))
        except Exception as problem:  # report and go on to the next model
            print(f"{model}: {type(problem).__name__}: {problem}", file=sys.stderr)
            failed += 1
            continue
        path = fixture_path(args.out, fixture["provider"], fixture["model"], fixture["kind"])
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(fixture, indent=2) + "\n", encoding="utf-8")
        print(
            json.dumps(
                {
                    "model": model,
                    "path": str(path),
                    "status": fixture["response"]["status"],
                    **fixture["adapter"],
                }
            )
        )
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:], os.environ))
