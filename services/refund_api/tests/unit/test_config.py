"""Startup guards (ADR-004, ADR-010) and the production gate (ADR-004/006)."""

from datetime import timedelta
from typing import Any

import pytest

from refund_api import config
from refund_api.config import ConfigError, Settings

GOOD_TOKEN = "a" * config.MIN_TOKEN_LENGTH
OTHER_TOKEN = "b" * config.MIN_TOKEN_LENGTH


def settings(**overrides: Any) -> Settings:
    base: dict[str, Any] = {
        "database_url": "postgresql+psycopg://u:p@localhost/db",
        "tokens": (GOOD_TOKEN,),
        "processing_budget": timedelta(seconds=5),
        "key_retention": timedelta(hours=24),
        "max_retry_window": timedelta(minutes=10),
        "max_recovery_delay": timedelta(hours=1),
        "pool_timeout": timedelta(seconds=2),
        "app_env": "test",
    }
    return Settings(**(base | overrides))


def environ(**overrides: str) -> dict[str, str]:
    base = {
        config.DATABASE_URL: "postgresql+psycopg://u:p@localhost/db",
        config.TOKEN_CURRENT: GOOD_TOKEN,
        config.PROCESSING_BUDGET: "5",
        config.KEY_RETENTION: "86400",
        config.MAX_RETRY_WINDOW: "600",
        config.MAX_RECOVERY_DELAY: "3600",
        config.POOL_TIMEOUT: "2",
        config.APP_ENV: "test",
    }
    return base | overrides


def test_control_valid_settings_start() -> None:
    assert settings().tokens == (GOOD_TOKEN,)
    assert Settings.from_env(environ(**{config.TOKEN_NEXT: OTHER_TOKEN})).tokens == (
        GOOD_TOKEN,
        OTHER_TOKEN,
    )


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"key_retention": timedelta(hours=1, minutes=10)}, "must exceed"),
        ({"pool_timeout": timedelta(seconds=5)}, "shorter than"),
        ({"tokens": ()}, "is required"),
        ({"tokens": ("short",)}, "at least"),
        ({"tokens": (GOOD_TOKEN, GOOD_TOKEN)}, "must differ"),
        ({"processing_budget": timedelta(0)}, "must be positive"),
    ],
    ids=["retention-too-short", "pool-timeout", "no-token", "weak-token", "same-tokens", "zero"],
)
def test_guard_refuses_to_start(overrides: dict[str, Any], message: str) -> None:
    with pytest.raises(ConfigError, match=message):
        settings(**overrides)


@pytest.mark.parametrize("flag", config.TEST_ONLY_FLAGS)
def test_production_refuses_test_only_flags(flag: str) -> None:
    with pytest.raises(ConfigError, match=flag):
        Settings.from_env(environ(**{config.APP_ENV: config.PRODUCTION, flag: "1"}))


@pytest.mark.parametrize("flag", config.TEST_ONLY_FLAGS)
def test_control_test_env_allows_test_only_flags(flag: str) -> None:
    Settings.from_env(environ(**{flag: "1"}))


@pytest.mark.parametrize("name", [config.DATABASE_URL, config.PROCESSING_BUDGET, config.APP_ENV])
def test_missing_variable_is_named(name: str) -> None:
    with pytest.raises(ConfigError, match=name):
        Settings.from_env({k: v for k, v in environ().items() if k != name})


@pytest.mark.parametrize("next_token", ["", None], ids=["empty", "unset"])
def test_empty_next_token_means_no_second_token(next_token: str | None) -> None:
    extra = {config.TOKEN_NEXT: next_token} if next_token is not None else {}
    assert Settings.from_env(environ(**extra)).tokens == (GOOD_TOKEN,)


def test_whitespace_next_token_is_refused_not_ignored() -> None:
    with pytest.raises(ConfigError, match="at least"):
        Settings.from_env(environ(**{config.TOKEN_NEXT: "   "}))


def test_non_numeric_duration_is_refused() -> None:
    with pytest.raises(ConfigError, match="number of seconds"):
        Settings.from_env(environ(**{config.POOL_TIMEOUT: "two"}))
