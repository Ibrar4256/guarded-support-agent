"""Refund-client settings and their startup guard (ADR-012, ADR-013).

refund-api is the single source of the retry window: the worker reads it from
``GET /config`` before each send (ADR-013). This module holds only the agent's own side:
the endpoint, the token, the retry policy, and a hard cap on any provider window the agent
will accept. The startup guard refuses a cap that couldn't fit the agent's own worst case,
because then every retried refund would end in reconciliation.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import timedelta

from agent.core.refund_outcome import RetryPolicy

MIN_TOKEN_LENGTH = 32
BASE_URL = "REFUND_API_URL"
TOKEN = "REFUND_API_TOKEN"
CLIENT_TIMEOUT = "REFUND_CLIENT_TIMEOUT_SECONDS"
MAX_ATTEMPTS = "REFUND_CLIENT_MAX_ATTEMPTS"
BACKOFF_BASE = "REFUND_CLIENT_BACKOFF_BASE_SECONDS"
BACKOFF_CAP = "REFUND_CLIENT_BACKOFF_CAP_SECONDS"
MAX_PROVIDER_WINDOW = "REFUND_CLIENT_MAX_PROVIDER_WINDOW_SECONDS"


class RefundClientConfigError(RuntimeError):
    """The agent must not start with this refund-client configuration."""


@dataclass(frozen=True)
class RefundClientSettings:
    base_url: str
    token: str
    policy: RetryPolicy
    max_provider_window: timedelta

    def __post_init__(self) -> None:
        if not self.base_url.startswith(("http://", "https://")):
            raise RefundClientConfigError(f"{BASE_URL} must be an http(s) URL")
        if len(self.token) < MIN_TOKEN_LENGTH:
            raise RefundClientConfigError(f"{TOKEN} must be at least {MIN_TOKEN_LENGTH} characters")
        worst = self.policy.worst_case_window()
        if worst > self.max_provider_window:
            raise RefundClientConfigError(
                f"worst-case retry window {worst.total_seconds():g}s exceeds "
                f"{MAX_PROVIDER_WINDOW} ({self.max_provider_window.total_seconds():g}s): "
                "no provider window the agent accepts could fit a full retry schedule"
            )

    @classmethod
    def from_env(cls, environ: Mapping[str, str]) -> "RefundClientSettings":
        try:
            policy = RetryPolicy(
                attempts=int(_required(environ, MAX_ATTEMPTS)),
                backoff_base=_seconds(environ, BACKOFF_BASE),
                backoff_cap=_seconds(environ, BACKOFF_CAP),
                client_timeout=_seconds(environ, CLIENT_TIMEOUT),
            )
        except ValueError as exc:
            raise RefundClientConfigError(str(exc)) from exc
        return cls(
            base_url=_required(environ, BASE_URL),
            token=_required(environ, TOKEN),
            policy=policy,
            max_provider_window=_seconds(environ, MAX_PROVIDER_WINDOW),
        )


def _required(environ: Mapping[str, str], name: str) -> str:
    value = environ.get(name, "")
    if not value:
        raise RefundClientConfigError(f"{name} is not set")
    return value


def _seconds(environ: Mapping[str, str], name: str) -> timedelta:
    raw = _required(environ, name)
    try:
        return timedelta(seconds=float(raw))
    except ValueError as exc:
        raise RefundClientConfigError(f"{name} must be a number of seconds, got {raw!r}") from exc
