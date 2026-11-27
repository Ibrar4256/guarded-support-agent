"""The turn's settings are validated at startup (ADR-022 S2, ADR-023 S3 cross-check)."""

from datetime import timedelta
from typing import Any

import pytest
from llm_wire import settings as llm_settings

from agent.turn.settings import TurnSettings, TurnSettingsError

ENV = {
    "TURN_MAX_MODEL_CALLS": "6",
    "TURN_MAX_MALFORMED": "2",
    "TURN_DEADLINE_SECONDS": "60",
    "TURN_LEASE_SECONDS": "30",
    "TURN_HEARTBEAT_SECONDS": "10",
    "TURN_STATEMENT_TIMEOUT_SECONDS": "5",
    "TURN_LOCK_TIMEOUT_SECONDS": "2",
    "MEMORY_WINDOW_MESSAGES": "12",
    "DIGEST_TICKETS": "5",
    "CUSTOMER_MESSAGE_MAX_CHARS": "4000",
    "PROMPT_WINDOW_MAX_CHARS": "24000",
    "TURN_PROVIDER_FAILURE_ALERT_AFTER": "3",
    "SUMMARY_TRIGGER_MESSAGES": "8",
    "SUMMARY_DEADLINE_SECONDS": "30",
    "TURN_PROPOSAL_LOCK_ATTEMPTS": "3",
    "REVIEW_DEADLINE_SECONDS": "86400",
    "WARRANT_DEADLINE_SECONDS": "8",
}


def test_the_defaults_load() -> None:
    settings = TurnSettings.from_env(ENV)

    assert settings.deadline == timedelta(seconds=60) and settings.max_model_calls == 6


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"TURN_HEARTBEAT_SECONDS": "11"}, "a third of TURN_LEASE_SECONDS"),
        ({"TURN_LOCK_TIMEOUT_SECONDS": "6"}, "must not exceed TURN_STATEMENT_TIMEOUT"),
        ({"TURN_STATEMENT_TIMEOUT_SECONDS": "60"}, "below TURN_DEADLINE_SECONDS"),
        ({"CUSTOMER_MESSAGE_MAX_CHARS": "24000"}, "below PROMPT_WINDOW_MAX_CHARS"),
        ({"TURN_MAX_MALFORMED": "0"}, "max_malformed must be positive"),
        ({"SUMMARY_DEADLINE_SECONDS": "-1"}, "summary_deadline must be positive"),
        ({"TURN_DEADLINE_SECONDS": "soon"}, "number of seconds"),
        ({"TURN_PROPOSAL_LOCK_ATTEMPTS": "10"}, "must be below TURN_LEASE_SECONDS"),
        ({"REVIEW_DEADLINE_SECONDS": "0"}, "review_deadline must be positive"),
        ({"WARRANT_DEADLINE_SECONDS": "0"}, "warrant_deadline must be positive"),
        ({"WARRANT_DEADLINE_SECONDS": "60"}, "WARRANT_DEADLINE_SECONDS must be below"),
    ],
)
def test_bad_settings_are_refused(changes: dict[str, Any], message: str) -> None:
    with pytest.raises(TurnSettingsError, match=message):
        TurnSettings.from_env({**ENV, **changes})


def test_a_model_attempt_must_fit_in_the_turn_the_summary_and_the_warrant() -> None:
    turn = TurnSettings.from_env(ENV)

    turn.check_against(llm_settings())  # 5 s minimum attempt fits 60 s and 30 s
    with pytest.raises(TurnSettingsError, match="TURN_DEADLINE_SECONDS"):
        turn.check_against(llm_settings(min_attempt=timedelta(seconds=60)))
    with pytest.raises(TurnSettingsError, match="SUMMARY_DEADLINE_SECONDS"):
        turn.check_against(llm_settings(min_attempt=timedelta(seconds=30)))
    with pytest.raises(TurnSettingsError, match="WARRANT_DEADLINE_SECONDS"):
        turn.check_against(llm_settings(min_attempt=timedelta(seconds=8)))
