"""Every captured provider response parses or classifies as the adapter must (ADR-023 Part 3,
ADR-025 S4/S5). Fixtures come from ``python -m agent_testing.capture``."""

import json
from pathlib import Path
from typing import Any

import httpx2
import pytest
from llm_wire import FakeClock, Wire
from llm_wire import settings as llm_settings

from agent.llm.adapter import OpenAICompatibleModel
from agent.llm.errors import (
    AuthFailed,
    ModelError,
    ProviderUnavailable,
    QuotaExhausted,
    RateLimited,
    RequestTooLarge,
)
from agent.llm.providers import PROVIDERS, Provider
from agent.llm.settings import ProviderChoice
from agent.llm.types import Purpose

FIXTURES = Path(__file__).with_name("llm_fixtures")
ALL = sorted(FIXTURES.rglob("*.json"))
# Captured from providers since dropped, kept because no provider we use has answered the same way.
RETIRED = Path(__file__).with_name("llm_fixtures_retired")
RETIRED_ALL = sorted(RETIRED.rglob("*.json"))
MESSAGES = [{"role": "user", "content": "Where is order ord_demo_1001?"}]
# Error fixtures whose cause isn't in their kind: what each must classify as.
ERRORS: dict[str, type[ModelError]] = {
    # OpenRouter's shared free pool busy upstream: a rate limit, retried, never a cooldown.
    "openrouter/google_gemma-4-26b-a4b-it_free/error.json": RateLimited,
    "openrouter/google_gemma-4-31b-it_free/error.json": RateLimited,
    "openrouter/inclusionai_ling-3.2-flash_free/error.json": RateLimited,
}
# The only captured 402s (SambaNova, dropped 2026-11-19): what pins 402 -> QuotaExhausted.
RETIRED_ERRORS: dict[str, type[ModelError]] = {
    "sambanova/gemma-4-31B-it/error.json": QuotaExhausted,  # 402 payment method required
    "sambanova/MiniMax-M3.1-Flash/error.json": QuotaExhausted,  # 402 payment method required
}
# The host-table entry each retired provider had when its fixtures were captured.
RETIRED_PROVIDERS = {
    "sambanova": Provider("sambanova", "https://api.sambanova.ai/v1", "SAMBANOVA_API_KEY")
}


def load(path: Path) -> dict[str, Any]:
    fixture: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    return fixture


async def replay(fixture: dict[str, Any]) -> Any:
    """The fixture's response through the adapter of its own provider (its markers apply)."""
    response = fixture["response"]
    wire = Wire().then(
        lambda request: httpx2.Response(
            response["status"],
            headers={"content-type": response["content_type"], **response.get("headers", {})},
            content=response["body"].encode(),
        )
    )
    choice = ProviderChoice(
        fixture["provider"], fixture["model"], "https://example.invalid/v1", "k"
    )
    clock = FakeClock()
    model = OpenAICompatibleModel(
        choice,
        llm_settings(max_attempts=1),
        http_client=wire.client(),
        monotonic=clock.monotonic,
        sleep=clock.sleep,
    )
    return await model.complete(MESSAGES, [], deadline=60.0, purpose=Purpose.TURN)


def ids(paths: list[Path], root: Path = FIXTURES) -> list[str]:
    return [str(path.relative_to(root)) for path in paths]


def of_kind(kind: str) -> list[Path]:
    return [path for path in ALL if load(path)["kind"] == kind]


@pytest.mark.parametrize("path", of_kind("tool_call"), ids=ids(of_kind("tool_call")))
async def test_a_captured_tool_call_parses_into_one(path: Path) -> None:
    response = await replay(load(path))

    assert [call.name for call in response.tool_calls] == ["lookup_order"]
    assert json.loads(response.tool_calls[0].arguments) == {"order_id": "ord_demo_1001"}
    assert response.usage is not None


@pytest.mark.parametrize("path", of_kind("auth"), ids=ids(of_kind("auth")))
async def test_a_captured_bad_key_is_an_auth_failure(path: Path) -> None:
    with pytest.raises(AuthFailed):
        await replay(load(path))


@pytest.mark.parametrize("name", ERRORS, ids=list(ERRORS))
async def test_a_captured_error_classifies_by_its_cause(name: str) -> None:
    with pytest.raises(ERRORS[name]):
        await replay(load(FIXTURES / name))


def test_every_error_fixture_has_an_expectation() -> None:
    assert sorted(ids(of_kind("error"))) == sorted(ERRORS)


@pytest.mark.parametrize("name", RETIRED_ERRORS, ids=list(RETIRED_ERRORS))
async def test_a_retired_providers_captured_error_still_classifies_by_its_cause(
    name: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Replayed under the provider's own former entry: no markers, so status decides."""
    fixture = load(RETIRED / name)
    monkeypatch.setitem(PROVIDERS, fixture["provider"], RETIRED_PROVIDERS[fixture["provider"]])

    with pytest.raises(RETIRED_ERRORS[name]):
        await replay(fixture)


def test_every_retired_fixture_has_an_expectation_and_its_provider_is_gone() -> None:
    assert sorted(ids(RETIRED_ALL, RETIRED)) == sorted(RETIRED_ERRORS)
    assert {load(path)["provider"] for path in RETIRED_ALL} == set(RETIRED_PROVIDERS)
    assert set(RETIRED_PROVIDERS).isdisjoint(PROVIDERS)


def test_no_fixture_holds_a_key_fragment() -> None:
    masked = [p for p in ALL + RETIRED_ALL if "*****" in p.read_text(encoding="utf-8")]

    assert masked == []


async def test_cloudflares_number_pieces_are_a_provider_error_not_altered_text() -> None:
    """Captured: the model wrote "0.05 9999 0800"; '0' and ' 9999' arrived as JSON numbers.
    Converting them gives "0.059999 0800", so the stream is refused instead."""
    fixture = load(
        FIXTURES / "cloudflare/_cf_meta_llama-3.3-70b-instruct-fp8-fast/numeric_tokens.json"
    )

    with pytest.raises(ProviderUnavailable, match="non-text content"):
        await replay(fixture)


@pytest.mark.parametrize("path", of_kind("tool_call"), ids=ids(of_kind("tool_call")))
async def test_gemini_tool_calls_keep_their_thought_signature_others_carry_nothing(
    path: Path,
) -> None:
    """Gemini refuses a second call without each call's signature (captured 400)."""
    fixture = load(path)
    call = (await replay(fixture)).tool_calls[0]

    if fixture["provider"] == "gemini":
        assert call.extra_content is not None
        assert json.loads(call.extra_content)["google"]["thought_signature"]
    else:
        assert call.extra_content is None


@pytest.mark.parametrize("path", of_kind("system_note"), ids=ids(of_kind("system_note")))
async def test_every_provider_accepts_a_system_note_after_the_customer_message(path: Path) -> None:
    """ADR-028 2A's correction shape: a mid-conversation system note, the malformed response not
    echoed. Accepted: a 200 the adapter parses without an error. What the model then does is an
    eval question (ADR-026 scripted steps), not a fixture one."""
    fixture = load(path)
    await replay(fixture)

    assert fixture["response"]["status"] == 200


async def test_cloudflares_gpt_oss_20b_answered_the_note_with_its_call_in_the_reasoning() -> None:
    """Captured once (k=1): after the note, the call went to the reasoning channel and the
    response had no tool call and no text, which the loop treats as an empty reply (malformed).
    Pinned so a change that starts reading calls out of reasoning shows up here."""
    fixture = load(FIXTURES / "cloudflare/_cf_openai_gpt-oss-20b/system_note.json")
    response = await replay(fixture)

    assert response.tool_calls == () and response.text == ""
    assert '{"order_id":"ord_demo_1001"}' in reasoning(fixture)


def reasoning(fixture: dict[str, Any]) -> str:
    """The stream's reasoning channel, reassembled from its chunks."""
    pieces = []
    for line in fixture["response"]["body"].splitlines():
        if line.startswith("data: {"):
            for choice in json.loads(line.removeprefix("data: "))["choices"]:
                pieces.append(choice["delta"].get("reasoning_content") or "")
    return "".join(pieces)


@pytest.mark.parametrize("path", of_kind("byte_ceiling"), ids=ids(of_kind("byte_ceiling")))
async def test_input_tokens_stay_under_one_per_utf8_byte(path: Path) -> None:
    """ADR-028's ceiling, checked against the provider's own count for 1,000 4-byte characters
    (the request's template overhead included). The old estimate of 3 characters per token
    undercounts this text by about ten times."""
    fixture = load(path)
    usage = (await replay(fixture)).usage
    sent = fixture["request"]

    assert usage is not None
    assert usage.input_tokens <= sent["bytes"]
    # More than one token per character: 3 characters per token undercounts by over 3 times.
    assert usage.input_tokens > sent["chars"]


async def test_groqs_request_too_large_is_its_own_error_never_a_page() -> None:
    """Captured 2026-11-20: a request above Groq's 8K tokens a minute is a 413 that names both
    numbers; waiting can't help it. RequestTooLarge, handled like a context that's too long
    (no retry, no page, try again with an alert), not InvalidRequest (a page)."""
    fixture = load(FIXTURES / "groq/openai_gpt-oss-120b/too_large.json")
    body = json.loads(fixture["response"]["body"])["error"]

    assert fixture["response"]["status"] == 413
    assert (body["type"], body["code"]) == ("tokens", "rate_limit_exceeded")
    assert "Limit 8000, Requested" in body["message"]
    with pytest.raises(RequestTooLarge) as raised:
        await replay(fixture)
    assert raised.value.attempts == 1, "retried a request that can never fit"
