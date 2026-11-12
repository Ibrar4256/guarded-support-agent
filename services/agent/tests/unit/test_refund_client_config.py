"""Refund-client settings (ADR-012, ADR-013): refund-api is the single source of the window;
the agent only caps what it will accept, and refuses a cap its own worst case can't fit."""

import pytest

from agent.refund_client import config
from agent.refund_client.config import RefundClientConfigError, RefundClientSettings

ENV = {
    config.BASE_URL: "http://refund-api:8000",
    config.TOKEN: "t" * 32,
    config.CLIENT_TIMEOUT: "8",
    config.MAX_ATTEMPTS: "5",
    config.BACKOFF_BASE: "0.5",
    config.BACKOFF_CAP: "8",
    config.MAX_PROVIDER_WINDOW: "900",
    config.MIN_PROVIDER_BUDGET: "1",
}


def test_control_defaults_start() -> None:
    settings = RefundClientSettings.from_env(ENV)
    assert settings.policy.worst_case_window().total_seconds() == 72


def test_a_cap_that_exactly_fits_the_worst_case_starts() -> None:
    RefundClientSettings.from_env(ENV | {config.MAX_PROVIDER_WINDOW: "72"})


def test_a_cap_below_the_worst_case_refuses_to_start() -> None:
    with pytest.raises(RefundClientConfigError, match="exceeds"):
        RefundClientSettings.from_env(ENV | {config.MAX_PROVIDER_WINDOW: "71"})


def test_the_agent_no_longer_reads_the_providers_window() -> None:
    assert not hasattr(config, "PROVIDER_RETRY_WINDOW")


@pytest.mark.parametrize(
    ("override", "message"),
    [
        ({config.TOKEN: "short"}, "at least"),
        ({config.BASE_URL: "refund-api:8000"}, "http"),
        ({config.MAX_ATTEMPTS: "0"}, "attempts"),
        ({config.BACKOFF_BASE: "9"}, "backoff_base"),
        ({config.CLIENT_TIMEOUT: "eight"}, "number of seconds"),
        ({config.MAX_PROVIDER_WINDOW: ""}, "not set"),
    ],
    ids=["weak-token", "no-scheme", "no-attempts", "base-over-cap", "not-a-number", "no-cap"],
)
def test_bad_settings_are_refused(override: dict[str, str], message: str) -> None:
    with pytest.raises(RefundClientConfigError, match=message):
        RefundClientSettings.from_env(ENV | override)


def test_a_non_positive_minimum_budget_is_refused() -> None:
    with pytest.raises(RefundClientConfigError, match=config.MIN_PROVIDER_BUDGET):
        RefundClientSettings.from_env(ENV | {config.MIN_PROVIDER_BUDGET: "0"})


def test_a_missing_minimum_budget_is_refused() -> None:
    with pytest.raises(RefundClientConfigError, match=config.MIN_PROVIDER_BUDGET):
        RefundClientSettings.from_env(
            {k: v for k, v in ENV.items() if k != config.MIN_PROVIDER_BUDGET}
        )
