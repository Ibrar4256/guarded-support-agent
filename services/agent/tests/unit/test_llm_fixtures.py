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
)
from agent.llm.settings import ProviderChoice
from agent.llm.types import Purpose

FIXTURES = Path(__file__).with_name("llm_fixtures")
ALL = sorted(FIXTURES.rglob("*.json"))
MESSAGES = [{"role": "user", "content": "Where is order ord_demo_1001?"}]
# Error fixtures whose cause isn't in their kind: what each must classify as.
ERRORS: dict[str, type[ModelError]] = {
    "sambanova/gemma-4-31B-it/error.json": QuotaExhausted,  # 402 payment method required
    "sambanova/MiniMax-M3.1-Flash/error.json": QuotaExhausted,  # 402 payment method required
    "sambanova/GLM-5.3/error.json": RateLimited,  # 429 "high demand", no quota marker
    "sambanova/gpt-oss-120b/error.json": AuthFailed,  # the earlier key was refused
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


def ids(paths: list[Path]) -> list[str]:
    return [str(path.relative_to(FIXTURES)) for path in paths]


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


def test_no_fixture_holds_a_key_fragment() -> None:
    masked = [p for p in ALL if "*****" in p.read_text(encoding="utf-8")]

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
