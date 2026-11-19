"""The real-model tools take only credentials from the developer's environment, and their model
settings are the .env.example values (ADR-025 S1)."""

import re
from pathlib import Path

import pytest
from agent_testing.env import CREDENTIAL_VARS, example_llm_env
from agent_testing.smoke_turn import SmokeRefused, smoke, smoke_env

from agent.llm.providers import PROVIDERS

ENV_EXAMPLE = Path(__file__).parents[4] / ".env.example"


def example_values() -> dict[str, str]:
    pairs = (
        re.match(r"^([A-Z_0-9]+)=(.*)$", line) for line in ENV_EXAMPLE.read_text().splitlines()
    )
    return {m[1]: m[2] for m in pairs if m}


def test_the_llm_defaults_are_the_env_example_values() -> None:
    example = example_values()
    defaults = example_llm_env(turn_models="groq:m", summary_models="groq:m")

    numeric = {k: v for k, v in defaults.items() if not k.endswith("_MODELS")}
    assert numeric == {k: example[k] for k in numeric}


def test_every_provider_credential_is_copied_and_nothing_else() -> None:
    named = {p.key_var for p in PROVIDERS.values()} | {p.url_var for p in PROVIDERS.values()}
    assert set(CREDENTIAL_VARS) == named - {None}

    environ = {
        "AGENT_DB_RUNTIME_URL": "postgresql+psycopg://u:p@localhost:1/agent",
        "GROQ_API_KEY": "k",
        "LLM_TEMPERATURE": "1.9",
        "UNRELATED_SECRET": "s",
    }
    env = smoke_env(environ, "groq:m", "groq:m")

    assert env["GROQ_API_KEY"] == "k" and env["LLM_TEMPERATURE"] == "0.2"
    assert "UNRELATED_SECRET" not in env


async def test_the_smoke_run_refuses_production() -> None:
    with pytest.raises(SmokeRefused, match="APP_ENV=production"):
        await smoke({"APP_ENV": "production"}, "groq:m", "groq:m", 0)
