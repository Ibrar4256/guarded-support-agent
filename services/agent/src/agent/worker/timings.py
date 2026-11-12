"""Worker timings and their startup guard (ADR-004 step 7, ADR-014 S8, S11, S12).

- The lease is at least three heartbeats long, so one late heartbeat never loses a claim.
- A step (a config read, a transaction, a send or a backoff sleep) may take at most
  ``client_timeout + backoff_cap + lock_timeout + margin``; past that the heartbeat stops
  renewing, so a stuck coroutine can't keep its claim forever.
- No database call may outlive that bound, so the statement timeout sits below it.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import timedelta

from agent.core.refund_outcome import RetryPolicy

LEASE = "WORKER_LEASE_SECONDS"
HEARTBEAT = "WORKER_HEARTBEAT_SECONDS"
LIVENESS_MARGIN = "WORKER_LIVENESS_MARGIN_SECONDS"
CLAIM_COOLDOWN = "WORKER_CLAIM_COOLDOWN_SECONDS"
STATEMENT_TIMEOUT = "WORKER_STATEMENT_TIMEOUT_SECONDS"
LOCK_TIMEOUT = "WORKER_LOCK_TIMEOUT_SECONDS"

MIN_HEARTBEATS_PER_LEASE = 3


class WorkerConfigError(RuntimeError):
    """The worker must not start with these timings."""


@dataclass(frozen=True)
class WorkerTimings:
    lease: timedelta
    heartbeat: timedelta
    liveness_margin: timedelta
    claim_cooldown: timedelta
    statement_timeout: timedelta
    lock_timeout: timedelta
    policy: RetryPolicy

    def __post_init__(self) -> None:
        for name in (
            "lease",
            "heartbeat",
            "liveness_margin",
            "claim_cooldown",
            "statement_timeout",
            "lock_timeout",
        ):
            if getattr(self, name) <= timedelta(0):
                raise WorkerConfigError(f"{name} must be positive")
        if self.lease < self.heartbeat * MIN_HEARTBEATS_PER_LEASE:
            raise WorkerConfigError(
                f"{LEASE} must be at least {MIN_HEARTBEATS_PER_LEASE} heartbeats "
                f"({HEARTBEAT}), so one late heartbeat can't lose the claim"
            )
        if self.lock_timeout > self.statement_timeout:
            raise WorkerConfigError(f"{LOCK_TIMEOUT} must not exceed {STATEMENT_TIMEOUT}")
        if self.statement_timeout >= self.max_step:
            raise WorkerConfigError(
                f"{STATEMENT_TIMEOUT} must be below the step bound "
                f"({self.max_step.total_seconds():g}s), or a database call could outlive it"
            )

    @property
    def max_step(self) -> timedelta:
        """The longest any one step may take before the heartbeat stops renewing."""
        return (
            self.policy.client_timeout
            + self.policy.backoff_cap
            + self.lock_timeout
            + self.liveness_margin
        )

    @classmethod
    def from_env(cls, environ: Mapping[str, str], policy: RetryPolicy) -> "WorkerTimings":
        return cls(
            lease=_seconds(environ, LEASE),
            heartbeat=_seconds(environ, HEARTBEAT),
            liveness_margin=_seconds(environ, LIVENESS_MARGIN),
            claim_cooldown=_seconds(environ, CLAIM_COOLDOWN),
            statement_timeout=_seconds(environ, STATEMENT_TIMEOUT),
            lock_timeout=_seconds(environ, LOCK_TIMEOUT),
            policy=policy,
        )


def _seconds(environ: Mapping[str, str], name: str) -> timedelta:
    raw = environ.get(name, "")
    if not raw:
        raise WorkerConfigError(f"{name} is not set")
    try:
        return timedelta(seconds=float(raw))
    except ValueError as exc:
        raise WorkerConfigError(f"{name} must be a number of seconds, got {raw!r}") from exc
