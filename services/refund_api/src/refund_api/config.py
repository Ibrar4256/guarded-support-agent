"""refund-api settings, read once from the environment, with the startup guards.

Guards (the service refuses to start if any fails):
- ADR-004: key retention > maximum retry window + maximum recovery delay.
- ADR-010: the pool's acquisition timeout < the processing budget, so waiting for a
  connection can't use up the budget unnoticed.
- ADR-010 Part 6: at least one service token, each long enough to resist guessing.
- ADR-004/006: in production, no test-only flag may be set.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import timedelta

MIN_TOKEN_LENGTH = 32
PRODUCTION = "production"
TEST_ONLY_FLAGS = ("DEBUG_FAULT_INJECTION", "CRASH_POINT", "TESTING")

DATABASE_URL = "REFUND_DB_RUNTIME_URL"
TOKEN_CURRENT = "REFUND_API_TOKEN_CURRENT"
TOKEN_NEXT = "REFUND_API_TOKEN_NEXT"
PROCESSING_BUDGET = "REFUND_API_PROCESSING_BUDGET_SECONDS"
KEY_RETENTION = "REFUND_API_KEY_RETENTION_SECONDS"
MAX_RETRY_WINDOW = "REFUND_API_MAX_RETRY_WINDOW_SECONDS"
MAX_RECOVERY_DELAY = "REFUND_API_MAX_RECOVERY_DELAY_SECONDS"
POOL_TIMEOUT = "REFUND_API_POOL_TIMEOUT_SECONDS"
APP_ENV = "APP_ENV"


class ConfigError(RuntimeError):
    """The service must not start with this configuration."""


@dataclass(frozen=True)
class Settings:
    database_url: str
    tokens: tuple[str, ...]
    processing_budget: timedelta
    key_retention: timedelta
    max_retry_window: timedelta
    max_recovery_delay: timedelta
    pool_timeout: timedelta
    app_env: str

    def __post_init__(self) -> None:
        if not self.database_url:
            raise ConfigError(f"{DATABASE_URL} is empty")
        if not self.tokens:
            raise ConfigError(f"{TOKEN_CURRENT} is required")
        if any(len(token) < MIN_TOKEN_LENGTH for token in self.tokens):
            raise ConfigError(f"service tokens must be at least {MIN_TOKEN_LENGTH} characters")
        if len(set(self.tokens)) != len(self.tokens):
            raise ConfigError(f"{TOKEN_NEXT} must differ from {TOKEN_CURRENT}")
        durations = {
            PROCESSING_BUDGET: self.processing_budget,
            KEY_RETENTION: self.key_retention,
            MAX_RETRY_WINDOW: self.max_retry_window,
            MAX_RECOVERY_DELAY: self.max_recovery_delay,
            POOL_TIMEOUT: self.pool_timeout,
        }
        for name, value in durations.items():
            if value <= timedelta(0):
                raise ConfigError(f"{name} must be positive")
        if self.key_retention <= self.max_retry_window + self.max_recovery_delay:
            raise ConfigError(
                f"{KEY_RETENTION} must exceed {MAX_RETRY_WINDOW} + {MAX_RECOVERY_DELAY} (ADR-004)"
            )
        if self.pool_timeout >= self.processing_budget:
            raise ConfigError(f"{POOL_TIMEOUT} must be shorter than {PROCESSING_BUDGET} (ADR-010)")

    @classmethod
    def from_env(cls, environ: Mapping[str, str]) -> "Settings":
        check_production_gate(environ)
        tokens = tuple(
            token
            for token in (environ.get(TOKEN_CURRENT, ""), environ.get(TOKEN_NEXT, ""))
            if token
        )
        return cls(
            database_url=required(environ, DATABASE_URL),
            tokens=tokens,
            processing_budget=seconds(environ, PROCESSING_BUDGET),
            key_retention=seconds(environ, KEY_RETENTION),
            max_retry_window=seconds(environ, MAX_RETRY_WINDOW),
            max_recovery_delay=seconds(environ, MAX_RECOVERY_DELAY),
            pool_timeout=seconds(environ, POOL_TIMEOUT),
            app_env=required(environ, APP_ENV),
        )


def check_production_gate(environ: Mapping[str, str]) -> None:
    """Test-only switches can never be live in production (ADR-004, ADR-006)."""
    if environ.get(APP_ENV) != PRODUCTION:
        return
    live = [flag for flag in TEST_ONLY_FLAGS if environ.get(flag)]
    if live:
        raise ConfigError(f"test-only flags set in production: {', '.join(live)}")


def required(environ: Mapping[str, str], name: str) -> str:
    value = environ.get(name, "")
    if not value:
        raise ConfigError(f"{name} is not set")
    return value


def seconds(environ: Mapping[str, str], name: str) -> timedelta:
    raw = required(environ, name)
    try:
        return timedelta(seconds=float(raw))
    except ValueError as exc:
        raise ConfigError(f"{name} must be a number of seconds, got {raw!r}") from exc
