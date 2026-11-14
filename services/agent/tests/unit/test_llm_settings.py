"""Model settings are validated at startup (ADR-023 S3) and gate the scripted model (ADR-006)."""

from typing import Any

import pytest

from agent.llm.settings import LLMSettings, LLMSettingsError

BASE = {
    "LLM_PROVIDER": "groq",
    "LLM_MODEL": "some-model",
    "LLM_API_KEY": "test-key",
    "LLM_MAX_OUTPUT_TOKENS": "1024",
    "LLM_MAX_OUTPUT_CHARS": "16000",
    "LLM_TEMPERATURE": "0.2",
    "LLM_MAX_ATTEMPTS": "3",
    "LLM_BACKOFF_BASE_SECONDS": "0.5",
    "LLM_BACKOFF_CAP_SECONDS": "8",
    "LLM_MIN_ATTEMPT_SECONDS": "5",
    "LLM_QUOTA_COOLDOWN_SECONDS": "900",
}


def env(**changes: Any) -> dict[str, str]:
    values = dict(BASE)
    for name, value in changes.items():
        if value is None:
            values.pop(name, None)
        else:
            values[name] = value
    return values


def test_valid_settings_load() -> None:
    settings = LLMSettings.from_env(BASE)

    assert settings.primary.provider == "groq" and settings.fallback is None
    assert "test-key" not in repr(settings)


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"LLM_PROVIDER": "openrouter"}, "unknown provider"),
        ({"LLM_API_KEY": None}, "LLM_API_KEY is not set"),
        ({"LLM_TEMPERATURE": "hot"}, "LLM_TEMPERATURE must be a number"),
        ({"LLM_TEMPERATURE": "nan"}, "between 0 and 2"),
        ({"LLM_MAX_ATTEMPTS": "0"}, "at least 1"),
        ({"LLM_BACKOFF_BASE_SECONDS": "10"}, "at most the cap"),
        ({"LLM_MIN_ATTEMPT_SECONDS": "0"}, "must be positive"),
        ({"LLM_FALLBACK_PROVIDER": "gemini"}, "missing: LLM_FALLBACK_MODEL, LLM_FALLBACK_API_KEY"),
        (
            {
                "LLM_FALLBACK_PROVIDER": "nope",
                "LLM_FALLBACK_MODEL": "m",
                "LLM_FALLBACK_API_KEY": "k",
            },
            "unknown provider",
        ),
    ],
)
def test_bad_settings_are_refused_naming_the_variable(
    changes: dict[str, Any], message: str
) -> None:
    with pytest.raises(LLMSettingsError, match=message):
        LLMSettings.from_env(env(**changes))


def test_a_complete_fallback_loads() -> None:
    settings = LLMSettings.from_env(
        env(LLM_FALLBACK_PROVIDER="gemini", LLM_FALLBACK_MODEL="m", LLM_FALLBACK_API_KEY="k")
    )

    assert settings.fallback is not None and settings.fallback.provider == "gemini"


def test_the_scripted_model_needs_the_test_flag() -> None:
    with pytest.raises(LLMSettingsError, match="TESTING=true"):
        LLMSettings.from_env(env(LLM_PROVIDER="scripted", LLM_API_KEY=None))

    settings = LLMSettings.from_env(env(LLM_PROVIDER="scripted", LLM_API_KEY=None, TESTING="true"))
    assert settings.primary.provider == "scripted"
