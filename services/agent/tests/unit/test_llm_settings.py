"""Model settings are validated at startup (ADR-023 S3, ADR-025 S1/S2) and gate the scripted
model (ADR-006)."""

from typing import Any

import pytest

from agent.llm.providers import PROVIDERS
from agent.llm.settings import LLMSettings, LLMSettingsError
from agent.llm.types import Purpose

ACCOUNT_ID = "0123456789abcdef0123456789abcdef"
BASE = {
    "LLM_TURN_MODELS": "groq:openai/gpt-oss-120b,cloudflare:@cf/openai/gpt-oss-20b",
    "LLM_SUMMARY_MODELS": "gemini:some-flash-lite",
    "GROQ_API_KEY": "groq-test-key",
    "GEMINI_API_KEY": "gemini-test-key",
    "CLOUDFLARE_API_TOKEN": "cloudflare-test-token",
    "CLOUDFLARE_ACCOUNT_ID": ACCOUNT_ID,
    "LLM_MAX_OUTPUT_TOKENS": "1024",
    "LLM_MAX_OUTPUT_CHARS": "16000",
    "LLM_TEMPERATURE": "0.2",
    "LLM_MAX_ATTEMPTS": "3",
    "LLM_BACKOFF_BASE_SECONDS": "0.5",
    "LLM_BACKOFF_CAP_SECONDS": "8",
    "LLM_MIN_ATTEMPT_SECONDS": "5",
    "LLM_QUOTA_COOLDOWN_SECONDS": "900",
}
KEYS = ("groq-test-key", "gemini-test-key", "cloudflare-test-token")


def env(**changes: Any) -> dict[str, str]:
    values = dict(BASE)
    for name, value in changes.items():
        if value is None:
            values.pop(name, None)
        else:
            values[name] = value
    return values


def test_valid_settings_load_each_purposes_list_in_order() -> None:
    settings = LLMSettings.from_env(BASE)

    turn = settings.models[Purpose.TURN]
    assert [c.label for c in turn] == [
        "groq:openai/gpt-oss-120b",
        "cloudflare:@cf/openai/gpt-oss-20b",
    ]
    assert [c.label for c in settings.models[Purpose.SUMMARY]] == ["gemini:some-flash-lite"]
    assert turn[0].base_url == PROVIDERS["groq"].base_url
    assert turn[1].base_url == f"https://api.cloudflare.com/client/v4/accounts/{ACCOUNT_ID}/ai/v1"
    assert turn[1].api_key == "cloudflare-test-token"


def test_no_key_appears_in_repr() -> None:
    shown = repr(LLMSettings.from_env(BASE))

    assert not [key for key in KEYS if key in shown]


def test_a_model_name_keeps_its_own_colons() -> None:
    settings = LLMSettings.from_env(
        env(LLM_SUMMARY_MODELS="openrouter:meta/llama:free", OPENROUTER_API_KEY="k")
    )

    assert settings.models[Purpose.SUMMARY][0].model == "meta/llama:free"


def test_providers_not_listed_need_no_credentials() -> None:
    settings = LLMSettings.from_env(
        env(LLM_TURN_MODELS="groq:m", CLOUDFLARE_API_TOKEN=None, CLOUDFLARE_ACCOUNT_ID=None)
    )

    assert [c.provider for c in settings.models[Purpose.TURN]] == ["groq"]


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"LLM_TURN_MODELS": None}, "LLM_TURN_MODELS is not set"),
        ({"LLM_SUMMARY_MODELS": ""}, "LLM_SUMMARY_MODELS is not set"),
        ({"LLM_TURN_MODELS": "groq:m,"}, "LLM_TURN_MODELS has an empty entry"),
        ({"LLM_TURN_MODELS": "groq"}, "'groq' is not provider:model"),
        ({"LLM_TURN_MODELS": "groq:"}, "'groq:' is not provider:model"),
        ({"LLM_TURN_MODELS": ":m"}, "':m' is not provider:model"),
        ({"LLM_TURN_MODELS": "nope:m"}, "LLM_TURN_MODELS: unknown provider 'nope'"),
        ({"GROQ_API_KEY": None}, "groq is listed but GROQ_API_KEY is not set"),
        ({"GEMINI_API_KEY": ""}, "gemini is listed but GEMINI_API_KEY is not set"),
        ({"CLOUDFLARE_ACCOUNT_ID": None}, "CLOUDFLARE_ACCOUNT_ID is not set"),
        ({"LLM_TURN_MODELS": "groq:a,groq:b"}, "LLM_TURN_MODELS lists groq twice"),
        ({"LLM_TURN_MODELS": "ollama:m"}, "ollama is listed but OLLAMA_BASE_URL is not set"),
        ({"LLM_TEMPERATURE": "hot"}, "LLM_TEMPERATURE must be a number"),
        ({"LLM_TEMPERATURE": "nan"}, "between 0 and 2"),
        ({"LLM_MAX_ATTEMPTS": "0"}, "at least 1"),
        ({"LLM_BACKOFF_BASE_SECONDS": "10"}, "at most the cap"),
        ({"LLM_MIN_ATTEMPT_SECONDS": "0"}, "must be positive"),
        # ADR-025 S5: OpenRouter served and billed a paid id at a $0 balance (2026-11-19).
        (
            {"LLM_TURN_MODELS": "openrouter:openai/gpt-oss-120b", "OPENROUTER_API_KEY": "k"},
            "openrouter models must end with ':free'; 'openai/gpt-oss-120b' is a paid model",
        ),
        (
            {"LLM_SUMMARY_MODELS": "openrouter:x/free-model", "OPENROUTER_API_KEY": "k"},
            "openrouter models must end with ':free'",
        ),
    ],
)
def test_bad_settings_are_refused_naming_the_variable(
    changes: dict[str, Any], message: str
) -> None:
    with pytest.raises(LLMSettingsError, match=message):
        LLMSettings.from_env(env(**changes))


@pytest.mark.parametrize(
    "account_id",
    [
        "x/../evil",
        "evil.example.com",
        "0123456789abcdef0123456789abcdef/../../x",
        "0123456789ABCDEF0123456789ABCDEF",
        "0123456789abcdef0123456789abcde",
        "0123456789abcdef0123456789abcdef\n",
    ],
)
def test_a_cloudflare_account_id_must_be_an_id(account_id: str) -> None:
    with pytest.raises(LLMSettingsError, match="CLOUDFLARE_ACCOUNT_ID must be a Cloudflare"):
        LLMSettings.from_env(env(CLOUDFLARE_ACCOUNT_ID=account_id))


@pytest.mark.parametrize(
    ("raw", "url"),
    [
        ("http://localhost:11434/v1", "http://localhost:11434/v1"),
        ("http://127.0.0.1:11434/v1/", "http://127.0.0.1:11434/v1/"),
        ("http://[::1]:11434/v1", "http://[::1]:11434/v1"),
        ("http://localhost/v1", "http://localhost/v1"),
    ],
)
def test_ollama_on_a_loopback_host_loads(raw: str, url: str) -> None:
    settings = LLMSettings.from_env(env(LLM_SUMMARY_MODELS="ollama:llama3.2", OLLAMA_BASE_URL=raw))

    assert settings.models[Purpose.SUMMARY][0].base_url == url


@pytest.mark.parametrize(
    "raw",
    [
        "http://evil.example.com:11434/v1",
        "http://localhost@evil.example.com/v1",
        "http://127.0.0.1.evil.example.com/v1",
        "https://localhost:11434/v1",
        "http://user:pass@localhost:11434/v1",
        "http://localhost:11434/v1?next=http://evil.example.com",
        "http://localhost:11434/v1#x",
        "http://localhost:11434/v1/../../x",
        "http://localhost:99999/v1",
        "http://10.0.0.5:11434/v1",
        "localhost:11434",
    ],
)
def test_ollama_anywhere_but_loopback_is_refused(raw: str) -> None:
    with pytest.raises(LLMSettingsError, match="OLLAMA_BASE_URL must"):
        LLMSettings.from_env(env(LLM_SUMMARY_MODELS="ollama:llama3.2", OLLAMA_BASE_URL=raw))


def test_ollama_is_refused_in_production() -> None:
    with pytest.raises(LLMSettingsError, match="ollama is for local use only"):
        LLMSettings.from_env(
            env(
                LLM_SUMMARY_MODELS="ollama:llama3.2",
                OLLAMA_BASE_URL="http://localhost:11434/v1",
                APP_ENV="production",
            )
        )


def test_the_scripted_model_needs_the_test_flag_and_never_runs_in_production() -> None:
    scripted = {"LLM_TURN_MODELS": "scripted:t", "LLM_SUMMARY_MODELS": "scripted:s"}
    with pytest.raises(LLMSettingsError, match="TESTING=true"):
        LLMSettings.from_env(env(**scripted))
    with pytest.raises(LLMSettingsError, match="refused with APP_ENV=production"):
        LLMSettings.from_env(env(**scripted, TESTING="true", APP_ENV="production"))

    settings = LLMSettings.from_env(env(**scripted, TESTING="true"))
    assert settings.models[Purpose.TURN][0].provider == "scripted"
