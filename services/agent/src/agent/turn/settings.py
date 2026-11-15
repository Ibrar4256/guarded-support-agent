"""The turn's budgets and timeouts (ADR-022 S2), from the environment, validated at startup.

The defaults in .env.example are starting values to tune from telemetry, not measurements.
"""

from collections.abc import Mapping
from dataclasses import dataclass, fields
from datetime import timedelta

from agent import envvars
from agent.llm.settings import LLMSettings


class TurnSettingsError(RuntimeError):
    pass


@dataclass(frozen=True)
class TurnSettings:
    max_model_calls: int
    max_malformed: int
    deadline: timedelta
    lease: timedelta
    heartbeat: timedelta
    statement_timeout: timedelta
    lock_timeout: timedelta
    window_messages: int
    digest_tickets: int
    message_max_chars: int
    window_max_chars: int
    provider_failure_alert_after: int
    summary_trigger_messages: int
    summary_deadline: timedelta

    def __post_init__(self) -> None:
        error = TurnSettingsError
        for field in fields(self):
            value = getattr(self, field.name)
            if (isinstance(value, timedelta) and value <= timedelta(0)) or (
                isinstance(value, int) and value <= 0
            ):
                raise error(f"{field.name} must be positive")
        if self.heartbeat * 3 > self.lease:
            raise error("TURN_HEARTBEAT_SECONDS must be at most a third of TURN_LEASE_SECONDS")
        if self.lock_timeout > self.statement_timeout:
            raise error("TURN_LOCK_TIMEOUT_SECONDS must not exceed TURN_STATEMENT_TIMEOUT_SECONDS")
        if self.statement_timeout >= self.deadline:
            raise error("TURN_STATEMENT_TIMEOUT_SECONDS must be below TURN_DEADLINE_SECONDS")
        if self.message_max_chars >= self.window_max_chars:
            raise error("CUSTOMER_MESSAGE_MAX_CHARS must be below PROMPT_WINDOW_MAX_CHARS")

    def check_against(self, llm: LLMSettings) -> None:
        """Cross-checks with the model settings (ADR-023 S3)."""
        if llm.min_attempt >= self.deadline:
            raise TurnSettingsError(
                "LLM_MIN_ATTEMPT_SECONDS must be below TURN_DEADLINE_SECONDS, "
                "or no call could start"
            )
        if llm.min_attempt >= self.summary_deadline:
            raise TurnSettingsError(
                "LLM_MIN_ATTEMPT_SECONDS must be below SUMMARY_DEADLINE_SECONDS, or no summary "
                "could start"
            )

    @classmethod
    def from_env(cls, environ: Mapping[str, str]) -> "TurnSettings":
        error = TurnSettingsError

        def count(name: str) -> int:
            return envvars.count(environ, name, error)

        def seconds(name: str) -> timedelta:
            return envvars.seconds(environ, name, error)

        return cls(
            max_model_calls=count("TURN_MAX_MODEL_CALLS"),
            max_malformed=count("TURN_MAX_MALFORMED"),
            deadline=seconds("TURN_DEADLINE_SECONDS"),
            lease=seconds("TURN_LEASE_SECONDS"),
            heartbeat=seconds("TURN_HEARTBEAT_SECONDS"),
            statement_timeout=seconds("TURN_STATEMENT_TIMEOUT_SECONDS"),
            lock_timeout=seconds("TURN_LOCK_TIMEOUT_SECONDS"),
            window_messages=count("MEMORY_WINDOW_MESSAGES"),
            digest_tickets=count("DIGEST_TICKETS"),
            message_max_chars=count("CUSTOMER_MESSAGE_MAX_CHARS"),
            window_max_chars=count("PROMPT_WINDOW_MAX_CHARS"),
            provider_failure_alert_after=count("TURN_PROVIDER_FAILURE_ALERT_AFTER"),
            summary_trigger_messages=count("SUMMARY_TRIGGER_MESSAGES"),
            summary_deadline=seconds("SUMMARY_DEADLINE_SECONDS"),
        )
