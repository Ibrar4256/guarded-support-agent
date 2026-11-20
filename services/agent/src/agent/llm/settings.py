"""Model settings from the environment, validated at startup (ADR-023 S3, ADR-025 S1/S2,
ADR-006 production gate).

Each provider has one optional credential set (``GROQ_API_KEY``, ``CLOUDFLARE_API_TOKEN`` with
``CLOUDFLARE_ACCOUNT_ID``, ...). Each purpose has an ordered list of ``provider:model`` entries,
primary first: ``LLM_TURN_MODELS`` and ``LLM_SUMMARY_MODELS``. Refused at startup: an empty
list, an unknown provider, a listed provider without its credentials, the same provider twice in
one list (cooldowns are per provider, so a second entry would be benched with the first), and
``ollama`` or ``scripted`` with ``APP_ENV=production``. The defaults in .env.example are starting
values, not measurements. Keys are never logged.
"""

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import timedelta

from agent import envvars
from agent.llm.providers import NO_KEY, PROVIDERS
from agent.llm.types import Purpose

SCRIPTED = "scripted"
MODEL_LISTS: dict[Purpose, str] = {
    Purpose.TURN: "LLM_TURN_MODELS",
    Purpose.SUMMARY: "LLM_SUMMARY_MODELS",
}


class LLMSettingsError(RuntimeError):
    pass


def is_production(environ: Mapping[str, str]) -> bool:
    return environ.get("APP_ENV") == "production"


@dataclass(frozen=True)
class ProviderChoice:
    provider: str
    model: str
    base_url: str
    api_key: str = field(repr=False)

    @property
    def label(self) -> str:
        """``provider:model``, as listed in the environment and in verified_models.json."""
        return f"{self.provider}:{self.model}"


@dataclass(frozen=True)
class LLMSettings:
    models: Mapping[Purpose, tuple[ProviderChoice, ...]]
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
        settings = cls(
            models={purpose: model_list(environ, purpose) for purpose in Purpose},
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


def model_list(environ: Mapping[str, str], purpose: Purpose) -> tuple[ProviderChoice, ...]:
    name = MODEL_LISTS[purpose]
    raw = envvars.required(environ, name, LLMSettingsError)
    entries = [entry.strip() for entry in raw.split(",")]
    if not all(entries):
        raise LLMSettingsError(f"{name} has an empty entry; expected provider:model,...")
    choices = tuple(_choice(environ, name, entry) for entry in entries)
    seen: set[str] = set()
    for choice in choices:
        if choice.provider in seen:
            raise LLMSettingsError(
                f"{name} lists {choice.provider} twice; cooldowns are per provider, so list "
                "each provider at most once"
            )
        seen.add(choice.provider)
    return choices


def _choice(environ: Mapping[str, str], list_name: str, entry: str) -> ProviderChoice:
    """One ``provider:model`` entry. The model is everything after the first colon, so names
    with their own colons (OpenRouter's ``...:free``) keep them."""
    error = LLMSettingsError
    provider_name, colon, model = entry.partition(":")
    if not colon or not provider_name or not model:
        raise error(f"{list_name}: {entry!r} is not provider:model")
    if provider_name == SCRIPTED:
        # ADR-006 layer 1: the scripted model needs an explicit test flag, never in production.
        if environ.get("TESTING") != "true":
            raise error(f"{list_name}: scripted models need TESTING=true")
        if is_production(environ):
            raise error(f"{list_name}: scripted models are refused with APP_ENV=production")
        return ProviderChoice(SCRIPTED, model, "", "")
    provider = PROVIDERS.get(provider_name)
    if provider is None:
        known = ", ".join(sorted(PROVIDERS))
        raise error(f"{list_name}: unknown provider {provider_name!r}; expected one of: {known}")
    if provider.local_only and is_production(environ):
        raise error(f"{list_name}: {provider_name} is for local use only, refused in production")
    if provider.model_suffix is not None and not model.endswith(provider.model_suffix):
        raise error(
            f"{list_name}: {provider_name} models must end with {provider.model_suffix!r}; "
            f"{model!r} is a paid model, and {provider_name} bills it even at a zero balance"
        )
    api_key = NO_KEY
    if provider.key_var is not None:
        api_key = _credential(environ, provider.key_var, provider_name)
    base_url = provider.base_url
    if provider.url_var is not None and provider.url_from is not None:
        raw = _credential(environ, provider.url_var, provider_name)
        try:
            base_url = provider.url_from(raw)
        except ValueError as exc:
            raise error(f"{provider.url_var} {exc}") from exc
    if base_url is None:
        raise error(f"{provider_name} has no base URL")  # a broken table entry
    return ProviderChoice(provider_name, model, base_url, api_key)


def _credential(environ: Mapping[str, str], name: str, provider: str) -> str:
    if not environ.get(name):
        raise LLMSettingsError(f"{provider} is listed but {name} is not set")
    return environ[name]


def _number(environ: Mapping[str, str], name: str) -> float:
    raw = envvars.required(environ, name, LLMSettingsError)
    try:
        return float(raw)
    except ValueError as exc:
        raise LLMSettingsError(f"{name} must be a number, got {raw!r}") from exc
