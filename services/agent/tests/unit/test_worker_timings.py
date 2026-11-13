"""Worker timings guard and the step clock (ADR-004 step 7, ADR-014 S8, S11)."""

from dataclasses import replace
from datetime import timedelta
from typing import Any

import pytest

from agent.core.refund_outcome import RetryPolicy
from agent.worker.lease import StepClock, new_worker_id
from agent.worker.timings import (
    CONFIG_STALE,
    HEARTBEAT,
    LEASE,
    LOCK_TIMEOUT,
    SHUTDOWN_GRACE,
    STATEMENT_TIMEOUT,
    WorkerConfigError,
    WorkerTimings,
)

POLICY = RetryPolicy(
    attempts=5,
    backoff_base=timedelta(seconds=0.5),
    backoff_cap=timedelta(seconds=8),
    client_timeout=timedelta(seconds=8),
)
VALID = WorkerTimings(
    lease=timedelta(seconds=15),
    heartbeat=timedelta(seconds=5),
    liveness_margin=timedelta(seconds=0.5),
    claim_cooldown=timedelta(seconds=30),
    statement_timeout=timedelta(seconds=5),
    lock_timeout=timedelta(seconds=1),
    config_refresh=timedelta(seconds=30),
    config_staleness=timedelta(seconds=90),
    presend_max_attempts=3,
    lock_retry_max=3,
    release_streak_limit=5,
    max_failed_reads=5,
    poll=timedelta(seconds=2),
    shutdown_grace=timedelta(seconds=10),
    quiet_margin=timedelta(seconds=2),
    stale_unknown_after=timedelta(seconds=3600),
    stale_page_after=timedelta(seconds=7200),
    policy=POLICY,
)
ENV = {
    "WORKER_LEASE_SECONDS": "15",
    "WORKER_HEARTBEAT_SECONDS": "5",
    "WORKER_LIVENESS_MARGIN_SECONDS": "0.5",
    "WORKER_CLAIM_COOLDOWN_SECONDS": "30",
    "WORKER_STATEMENT_TIMEOUT_SECONDS": "5",
    "WORKER_LOCK_TIMEOUT_SECONDS": "1",
    "WORKER_CONFIG_REFRESH_SECONDS": "30",
    "WORKER_CONFIG_STALE_SECONDS": "90",
    "WORKER_PRESEND_MAX_ATTEMPTS": "3",
    "WORKER_LOCK_RETRY_MAX": "3",
    "WORKER_RELEASE_STREAK_LIMIT": "5",
    "WORKER_MAX_FAILED_READS": "5",
    "WORKER_POLL_SECONDS": "2",
    "WORKER_SHUTDOWN_GRACE_SECONDS": "10",
    "RECONCILER_QUIET_MARGIN_SECONDS": "2",
    "RECONCILER_STALE_ALERT_SECONDS": "3600",
    "RECONCILER_STALE_PAGE_SECONDS": "7200",
}


def test_max_step_is_timeout_plus_cap_plus_lock_wait_plus_margin() -> None:
    assert VALID.max_step == timedelta(seconds=8 + 8 + 1 + 0.5)


def test_from_env_reads_every_value() -> None:
    assert WorkerTimings.from_env(ENV, POLICY) == VALID


@pytest.mark.parametrize("name", sorted(ENV))
def test_from_env_refuses_a_missing_value(name: str) -> None:
    with pytest.raises(WorkerConfigError, match=name):
        WorkerTimings.from_env({k: v for k, v in ENV.items() if k != name}, POLICY)


def test_from_env_refuses_a_non_number() -> None:
    with pytest.raises(WorkerConfigError, match=LEASE):
        WorkerTimings.from_env({**ENV, LEASE: "15s"}, POLICY)


def test_lease_shorter_than_three_heartbeats_is_refused() -> None:
    with pytest.raises(WorkerConfigError, match=HEARTBEAT):
        replace(VALID, lease=timedelta(seconds=14.9))


def test_negative_control_lease_of_exactly_three_heartbeats_is_accepted() -> None:
    assert replace(VALID, lease=timedelta(seconds=15)).lease == timedelta(seconds=15)


def test_lock_timeout_above_statement_timeout_is_refused() -> None:
    with pytest.raises(WorkerConfigError, match=LOCK_TIMEOUT):
        replace(VALID, lock_timeout=timedelta(seconds=6))


def test_statement_timeout_at_the_step_bound_is_refused() -> None:
    with pytest.raises(WorkerConfigError, match=STATEMENT_TIMEOUT):
        replace(VALID, statement_timeout=VALID.max_step)


def test_negative_control_statement_timeout_just_below_the_bound_is_accepted() -> None:
    below = VALID.max_step - timedelta(milliseconds=1)
    assert replace(VALID, statement_timeout=below).statement_timeout == below


@pytest.mark.parametrize(
    "name", ["presend_max_attempts", "lock_retry_max", "release_streak_limit", "max_failed_reads"]
)
def test_limits_below_one_are_refused(name: str) -> None:
    changes: dict[str, Any] = {name: 0}
    with pytest.raises(WorkerConfigError, match=name):
        replace(VALID, **changes)


def test_from_env_refuses_a_fractional_count() -> None:
    with pytest.raises(WorkerConfigError, match="WORKER_LOCK_RETRY_MAX"):
        WorkerTimings.from_env({**ENV, "WORKER_LOCK_RETRY_MAX": "2.5"}, POLICY)


def test_a_shutdown_grace_not_above_the_client_timeout_is_refused() -> None:
    with pytest.raises(WorkerConfigError, match=SHUTDOWN_GRACE):
        replace(VALID, shutdown_grace=POLICY.client_timeout)


def test_negative_control_a_grace_just_above_the_client_timeout_is_accepted() -> None:
    grace = POLICY.client_timeout + timedelta(milliseconds=1)
    assert replace(VALID, shutdown_grace=grace).shutdown_grace == grace


def test_staleness_under_two_refreshes_is_refused() -> None:
    with pytest.raises(WorkerConfigError, match=CONFIG_STALE):
        replace(VALID, config_staleness=timedelta(seconds=59))


def test_negative_control_staleness_of_exactly_two_refreshes_is_accepted() -> None:
    assert replace(VALID, config_staleness=timedelta(seconds=60)).config_staleness == timedelta(
        seconds=60
    )


@pytest.mark.parametrize(
    "name",
    [
        "lease",
        "heartbeat",
        "liveness_margin",
        "claim_cooldown",
        "config_refresh",
        "quiet_margin",
        "stale_unknown_after",
        "stale_page_after",
    ],
)
def test_non_positive_durations_are_refused(name: str) -> None:
    with pytest.raises(WorkerConfigError, match=name):
        changes: dict[str, Any] = {name: timedelta(0)}
        replace(VALID, **changes)


class FakeMonotonic:
    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now


def test_step_clock_overruns_only_past_the_bound() -> None:
    mono = FakeMonotonic()
    clock = StepClock(timedelta(seconds=10), monotonic=mono)
    mono.now += 10
    assert not clock.overrun(), "exactly at the bound is still in time"
    mono.now += 0.001
    assert clock.overrun()


def test_begin_step_restarts_the_clock() -> None:
    mono = FakeMonotonic()
    clock = StepClock(timedelta(seconds=10), monotonic=mono)
    mono.now += 9
    clock.begin_step()
    mono.now += 9
    assert not clock.overrun()


def test_negative_control_without_begin_step_the_same_time_overruns() -> None:
    mono = FakeMonotonic()
    clock = StepClock(timedelta(seconds=10), monotonic=mono)
    mono.now += 18
    assert clock.overrun()


def test_worker_ids_are_unique_per_call() -> None:
    assert new_worker_id() != new_worker_id()


@pytest.mark.parametrize("page_seconds", [3600, 1800])
def test_a_stale_page_not_later_than_the_alert_is_refused(page_seconds: int) -> None:
    """ADR-017 S3: the page is the later, louder signal."""
    with pytest.raises(WorkerConfigError, match="RECONCILER_STALE_PAGE_SECONDS"):
        replace(VALID, stale_page_after=timedelta(seconds=page_seconds))
