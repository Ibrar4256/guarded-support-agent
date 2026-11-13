"""Worker timings and limits, and their startup guard (ADR-004 step 7, ADR-005, ADR-009,
ADR-014 S5, S8, S11, S12).

- The lease is at least three heartbeats long, so one late heartbeat never loses a claim.
- A step (a config read, a transaction, a send or a backoff sleep) may take at most
  ``client_timeout + backoff_cap + lock_timeout + margin``; past that the heartbeat stops
  renewing, so a stuck coroutine can't keep its claim forever.
- No database call may outlive that bound, so the statement timeout sits below it.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import timedelta

from agent import envvars
from agent.core.refund_outcome import RetryPolicy

LEASE = "WORKER_LEASE_SECONDS"
HEARTBEAT = "WORKER_HEARTBEAT_SECONDS"
LIVENESS_MARGIN = "WORKER_LIVENESS_MARGIN_SECONDS"
CLAIM_COOLDOWN = "WORKER_CLAIM_COOLDOWN_SECONDS"
STATEMENT_TIMEOUT = "WORKER_STATEMENT_TIMEOUT_SECONDS"
LOCK_TIMEOUT = "WORKER_LOCK_TIMEOUT_SECONDS"
CONFIG_REFRESH = "WORKER_CONFIG_REFRESH_SECONDS"
CONFIG_STALE = "WORKER_CONFIG_STALE_SECONDS"
PRESEND_MAX_ATTEMPTS = "WORKER_PRESEND_MAX_ATTEMPTS"
LOCK_RETRY_MAX = "WORKER_LOCK_RETRY_MAX"
RELEASE_STREAK_LIMIT = "WORKER_RELEASE_STREAK_LIMIT"
MAX_FAILED_READS = "WORKER_MAX_FAILED_READS"
POLL = "WORKER_POLL_SECONDS"
SHUTDOWN_GRACE = "WORKER_SHUTDOWN_GRACE_SECONDS"
QUIET_MARGIN = "RECONCILER_QUIET_MARGIN_SECONDS"
STALE_ALERT = "RECONCILER_STALE_ALERT_SECONDS"
STALE_PAGE = "RECONCILER_STALE_PAGE_SECONDS"
PROVIDER_THRESHOLD = "PROVIDER_BREAKER_FAILURE_THRESHOLD"
PROVIDER_OPEN = "PROVIDER_BREAKER_OPEN_SECONDS"
PROVIDER_MAX_OPEN = "PROVIDER_BREAKER_MAX_OPEN_SECONDS"
PROVIDER_PAGE_AFTER = "PROVIDER_BREAKER_PAGE_AFTER_SECONDS"

MIN_HEARTBEATS_PER_LEASE = 3
# The cached provider config goes stale after this many missed refreshes, so one late
# refresh doesn't close the claim gate.
MIN_REFRESHES_PER_STALENESS = 2


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
    config_refresh: timedelta
    config_staleness: timedelta
    # ADR-009: failed pre-send checks before the run escalates (nothing sent).
    presend_max_attempts: int
    # ADR-005: consecutive lock timeouts before the claim is released with a cooldown.
    lock_retry_max: int
    # ADR-014 S12: consecutive cooldown releases before a run is escalated or paged.
    release_streak_limit: int
    # ADR-014 Part 4: failed per-send config reads in a row before an EXECUTING run's claim
    # is released.
    max_failed_reads: int
    # How often an idle worker looks for work (ADR-014 Part 2).
    poll: timedelta
    # S7: how long a stopping worker lets an attempt on the wire finish before exiting.
    shutdown_grace: timedelta
    # ADR-016 Part 2: added to every quiet period, for refund-api's gap between its deadline
    # check and its commit, and transit time.
    quiet_margin: timedelta
    # ADR-004, ADR-016 Part 3: a NEEDS_RECONCILIATION run unresolved this long alerts.
    stale_unknown_after: timedelta
    # ADR-017 S3: ... and this long pages: the only route to a person for a run whose lookups
    # fail alone, since those never count toward its streak.
    stale_page_after: timedelta
    # ADR-018: provider breakers (one per path) open after this many refund-api-type failures in
    # a row, stay open for the open interval (doubling up to the maximum between failed probes),
    # and page once an outage lasts longer than ``provider_page_after``.
    provider_failure_threshold: int
    provider_open: timedelta
    provider_max_open: timedelta
    provider_page_after: timedelta
    policy: RetryPolicy

    def __post_init__(self) -> None:
        for name in (
            "lease",
            "heartbeat",
            "liveness_margin",
            "claim_cooldown",
            "statement_timeout",
            "lock_timeout",
            "config_refresh",
            "config_staleness",
            "poll",
            "shutdown_grace",
            "quiet_margin",
            "stale_unknown_after",
            "stale_page_after",
            "provider_open",
            "provider_max_open",
            "provider_page_after",
        ):
            if getattr(self, name) <= timedelta(0):
                raise WorkerConfigError(f"{name} must be positive")
        for name in (
            "presend_max_attempts",
            "lock_retry_max",
            "release_streak_limit",
            "max_failed_reads",
            "provider_failure_threshold",
        ):
            if getattr(self, name) < 1:
                raise WorkerConfigError(f"{name} must be at least 1")
        if self.lease < self.heartbeat * MIN_HEARTBEATS_PER_LEASE:
            raise WorkerConfigError(
                f"{LEASE} must be at least {MIN_HEARTBEATS_PER_LEASE} heartbeats "
                f"({HEARTBEAT}), so one late heartbeat can't lose the claim"
            )
        if self.config_staleness < self.config_refresh * MIN_REFRESHES_PER_STALENESS:
            raise WorkerConfigError(
                f"{CONFIG_STALE} must be at least {MIN_REFRESHES_PER_STALENESS} refresh "
                f"intervals ({CONFIG_REFRESH}), so one late refresh doesn't close the gate"
            )
        if self.shutdown_grace <= self.policy.client_timeout:
            raise WorkerConfigError(
                f"{SHUTDOWN_GRACE} must exceed the client timeout, so an attempt on the wire "
                "can finish and be recorded before the worker exits"
            )
        if self.stale_page_after <= self.stale_unknown_after:
            raise WorkerConfigError(
                f"{STALE_PAGE} must exceed {STALE_ALERT}: the page is the later, louder signal"
            )
        if self.provider_failure_threshold >= self.policy.attempts:
            raise WorkerConfigError(
                f"{PROVIDER_THRESHOLD} must be below REFUND_CLIENT_MAX_ATTEMPTS, so the run whose "
                "failures open the write breaker still has an attempt left and is released, not "
                "reconciled (ADR-018 S1)"
            )
        if self.provider_max_open < self.provider_open:
            raise WorkerConfigError(f"{PROVIDER_MAX_OPEN} must be at least {PROVIDER_OPEN}")
        if self.provider_page_after >= self.stale_page_after:
            raise WorkerConfigError(
                f"{PROVIDER_PAGE_AFTER} must be below {STALE_PAGE}: during a read outage the "
                "breaker's page stands in for the per-run stale pages (ADR-018 S9)"
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
            config_refresh=_seconds(environ, CONFIG_REFRESH),
            config_staleness=_seconds(environ, CONFIG_STALE),
            presend_max_attempts=_count(environ, PRESEND_MAX_ATTEMPTS),
            lock_retry_max=_count(environ, LOCK_RETRY_MAX),
            release_streak_limit=_count(environ, RELEASE_STREAK_LIMIT),
            max_failed_reads=_count(environ, MAX_FAILED_READS),
            poll=_seconds(environ, POLL),
            shutdown_grace=_seconds(environ, SHUTDOWN_GRACE),
            quiet_margin=_seconds(environ, QUIET_MARGIN),
            stale_unknown_after=_seconds(environ, STALE_ALERT),
            stale_page_after=_seconds(environ, STALE_PAGE),
            provider_failure_threshold=_count(environ, PROVIDER_THRESHOLD),
            provider_open=_seconds(environ, PROVIDER_OPEN),
            provider_max_open=_seconds(environ, PROVIDER_MAX_OPEN),
            provider_page_after=_seconds(environ, PROVIDER_PAGE_AFTER),
            policy=policy,
        )


def _seconds(environ: Mapping[str, str], name: str) -> timedelta:
    return envvars.seconds(environ, name, WorkerConfigError)


def _count(environ: Mapping[str, str], name: str) -> int:
    return envvars.count(environ, name, WorkerConfigError)
