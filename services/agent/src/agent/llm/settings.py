"""Model settings from the environment, validated at startup (ADR-023 S3, ADR-006 production gate).

The defaults in .env.example are starting values, not measurements. Keys are never logged.
"""

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import timedelta

from agent import envvars
from agent.llm.providers import PROVIDERS

SCRIPTED = "scripted"
FALLBACK_VARS = ("LLM_FALLBACK_PROVIDER", "LLM_FALLBACK_MODEL", "LLM_FALLBACK_API_KEY")


class LLMSettingsError(RuntimeError):
    pass


@dataclass(frozen=True)
class ProviderChoice:
    provider: str
    model: str
    api_key: str = field(repr=False)


@dataclass(frozen=True)
class LLMSettings:
    primary: ProviderChoice
    fallback: ProviderChoice | None
    max_output_tokens: int
    max_output_chars: int
    temperature: float
    max_attempts: int
    backoff_base: timedelta
    backoff_cap: timedelta
    min_attempt: timedelta
    quota_cooldown: timedelta

    @classmethod
    def from_env(cls, environ: Mapping[str, str]) -> "LLMSettings":
        error = LLMSettingsError
        provider = envvars.required(environ, "LLM_PROVIDER", error)
        if provider == SCRIPTED:
            # ADR-006 layer 1: the scripted model needs an explicit test flag as well.
            if environ.get("TESTING") != "true":
                raise error("LLM_PROVIDER=scripted needs TESTING=true")
            primary = ProviderChoice(SCRIPTED, environ.get("LLM_MODEL", SCRIPTED), "")
        else:
            primary = _choice(environ, provider, "LLM_MODEL", "LLM_API_KEY")
        fallback = _fallback(environ)
        settings = cls(
            primary=primary,
            fallback=fallback,
            max_output_tokens=envvars.count(environ, "LLM_MAX_OUTPUT_TOKENS", error),
            max_output_chars=envvars.count(environ, "LLM_MAX_OUTPUT_CHARS", error),
            temperature=_number(environ, "LLM_TEMPERATURE"),
            max_attempts=envvars.count(environ, "LLM_MAX_ATTEMPTS", error),
            backoff_base=envvars.seconds(environ, "LLM_BACKOFF_BASE_SECONDS", error),
            backoff_cap=envvars.seconds(environ, "LLM_BACKOFF_CAP_SECONDS", error),
            min_attempt=envvars.seconds(environ, "LLM_MIN_ATTEMPT_SECONDS", error),
            quota_cooldown=envvars.seconds(environ, "LLM_QUOTA_COOLDOWN_SECONDS", error),
        )
        settings.validate()
        return settings

    def validate(self) -> None:
        error = LLMSettingsError
        if self.max_output_tokens <= 0 or self.max_output_chars <= 0:
            raise error("LLM_MAX_OUTPUT_TOKENS and LLM_MAX_OUTPUT_CHARS must be positive")
        if not 0 <= self.temperature <= 2:
            raise error("LLM_TEMPERATURE must be between 0 and 2")
        if self.max_attempts < 1:
            raise error("LLM_MAX_ATTEMPTS must be at least 1")
        if self.backoff_base <= timedelta(0) or self.backoff_cap < self.backoff_base:
            raise error("LLM_BACKOFF_BASE_SECONDS must be positive and at most the cap")
        if self.min_attempt <= timedelta(0) or self.quota_cooldown <= timedelta(0):
            raise error("LLM_MIN_ATTEMPT_SECONDS and LLM_QUOTA_COOLDOWN_SECONDS must be positive")


def _choice(
    environ: Mapping[str, str], provider: str, model_var: str, key_var: str
) -> ProviderChoice:
    if provider not in PROVIDERS:
        known = ", ".join(sorted(PROVIDERS))
        raise LLMSettingsError(f"unknown provider {provider!r}; expected one of: {known}")
    return ProviderChoice(
        provider,
        envvars.required(environ, model_var, LLMSettingsError),
        envvars.required(environ, key_var, LLMSettingsError),
    )


def _fallback(environ: Mapping[str, str]) -> ProviderChoice | None:
    present = [name for name in FALLBACK_VARS if environ.get(name)]
    if not present:
        return None
    if len(present) != len(FALLBACK_VARS):
        missing = ", ".join(name for name in FALLBACK_VARS if name not in present)
        raise LLMSettingsError(f"a fallback provider needs all of its settings; missing: {missing}")
    return _choice(environ, environ["LLM_FALLBACK_PROVIDER"], *FALLBACK_VARS[1:])


def _number(environ: Mapping[str, str], name: str) -> float:
    raw = envvars.required(environ, name, LLMSettingsError)
    try:
        return float(raw)
    except ValueError as exc:
        raise LLMSettingsError(f"{name} must be a number, got {raw!r}") from exc
