"""Fixture capture (ADR-025 S4, S5): one response per request, no key ever written, and what it
writes is what the proof binding accepts."""

import gzip
import json
from datetime import date
from pathlib import Path
from typing import Any

import httpx2
import pytest
from agent_testing.capture import INVALID_KEY, CaptureRefused, capture, fixture_path
from llm_wire import error_body
from test_tool_call_proofs import TEXT_BODY, TOOL_CALL_BODY, problem_with

from agent.llm.verified import ToolCallProof

KEY = "groq-secret-key-do-not-write"
ENV = {"GROQ_API_KEY": KEY}
MODEL = "groq:openai/gpt-oss-120b"


class Server:
    """A strict fake provider: answers each request with the next response."""

    def __init__(self, *responses: httpx2.Response) -> None:
        self.responses = list(responses)
        self.requests: list[httpx2.Request] = []

    def handle(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        if not self.responses:
            raise AssertionError(f"unexpected request {len(self.requests)}")
        return self.responses.pop(0)

    def transport(self) -> httpx2.MockTransport:
        return httpx2.MockTransport(self.handle)


def streamed(body: str, **headers: str) -> httpx2.Response:
    return httpx2.Response(
        200, headers={"content-type": "text/event-stream", **headers}, content=body.encode()
    )


async def run(server: Server, kind: str = "tool_call", env: dict[str, str] = ENV) -> dict[str, Any]:
    return await capture(env, MODEL, kind, transport=server.transport())


async def test_a_captured_tool_call_is_a_fixture_the_proof_binding_accepts(tmp_path: Path) -> None:
    fixture = await run(Server(streamed(TOOL_CALL_BODY)))
    path = fixture_path(tmp_path, fixture["provider"], fixture["model"], fixture["kind"])
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(fixture), encoding="utf-8")
    captured_on = date.fromisoformat(fixture["captured_on"])
    proof = ToolCallProof(MODEL, str(path.relative_to(tmp_path)), captured_on)

    assert fixture["kind"] == "tool_call"
    assert await problem_with(proof, tmp_path) is None


async def test_a_text_answer_is_saved_as_text_not_as_a_proof() -> None:
    fixture = await run(Server(streamed(TEXT_BODY)))

    assert fixture["kind"] == "text"


async def test_no_key_and_no_cookie_is_written_rate_limit_headers_are() -> None:
    server = Server(
        streamed(
            TOOL_CALL_BODY,
            **{"set-cookie": f"session={KEY}", "x-ratelimit-remaining-tokens": "7000"},
        )
    )
    fixture = await run(server)
    written = json.dumps(fixture)

    assert KEY not in written
    assert fixture["response"]["headers"] == {"x-ratelimit-remaining-tokens": "7000"}
    assert server.requests[0].headers["authorization"] == f"Bearer {KEY}"  # it was sent, not kept


@pytest.mark.parametrize(
    "echo",
    [f"Incorrect API key provided: {KEY[:6]}*****{KEY[-4:]}.", f"Invalid key {KEY}"],
    ids=["masked", "whole"],
)
async def test_a_key_echoed_in_an_error_body_is_redacted(echo: str) -> None:
    server = Server(httpx2.Response(401, json=error_body(echo, code="invalid_api_key")))
    fixture = await run(server)
    written = json.dumps(fixture)

    assert KEY not in written and f"{KEY[:6]}*****" not in written
    assert "<redacted>" in fixture["response"]["body"]


async def test_an_auth_capture_sends_only_the_invalid_key() -> None:
    server = Server(
        httpx2.Response(401, json=error_body("Invalid API Key", code="invalid_api_key"))
    )
    fixture = await run(server, kind="auth")

    assert server.requests[0].headers["authorization"] == f"Bearer {INVALID_KEY}"
    assert fixture["kind"] == "auth" and fixture["response"]["status"] == 401
    assert fixture["adapter"] == {"error": "AuthFailed"}


async def test_one_request_only_even_for_a_retryable_error() -> None:
    server = Server(
        httpx2.Response(429, json=error_body("Rate limit reached"), headers={"retry-after": "1"})
    )
    fixture = await run(server)

    assert len(server.requests) == 1
    assert fixture["kind"] == "error" and fixture["adapter"] == {"error": "RateLimited"}
    assert fixture["response"]["headers"] == {"retry-after": "1"}


async def test_a_compressed_response_is_saved_and_parsed_decoded() -> None:
    body = gzip.compress(TOOL_CALL_BODY.encode())
    server = Server(
        httpx2.Response(
            200,
            headers={"content-type": "text/event-stream", "content-encoding": "gzip"},
            content=body,
        )
    )
    fixture = await run(server)

    assert fixture["kind"] == "tool_call"
    assert fixture["response"]["body"] == TOOL_CALL_BODY


async def test_capture_refuses_production() -> None:
    with pytest.raises(CaptureRefused, match="APP_ENV=production"):
        await run(Server(), env={**ENV, "APP_ENV": "production"})


def test_fixture_paths_are_ones_the_registry_accepts(tmp_path: Path) -> None:
    path = fixture_path(tmp_path, "cloudflare", "@cf/openai/gpt-oss-20b", "tool_call")

    assert str(path.relative_to(tmp_path)) == "cloudflare/_cf_openai_gpt-oss-20b/tool_call.json"
