"""The claim gate (ADR-012 Part 4, ADR-013, ADR-014 Part 4, ADR-016 Part 3)."""

import asyncio
from datetime import timedelta

import pytest
from agent_testing.fakes import RecordingAlerter, ScriptedRefundClient, config_result

from agent.alerts import Severity
from agent.core.refund_outcome import ConfigRead, ConfigReadKind, HttpResult, RetryPolicy
from agent.refund_client.config import RefundClientSettings
from agent.worker.gate import (
    ConfigGate,
    GateState,
    read_config,
    refresh_forever,
    startup_check,
)
from agent.worker.timings import WorkerConfigError

POLICY = RetryPolicy(
    attempts=5,
    backoff_base=timedelta(seconds=0.5),
    backoff_cap=timedelta(seconds=8),
    client_timeout=timedelta(seconds=8),
)
WORST_MS = int(POLICY.worst_case_window().total_seconds() * 1000)  # 72 s
SETTINGS = RefundClientSettings(
    base_url="http://refund-api",
    token="t" * 32,
    policy=POLICY,
    max_provider_window=timedelta(seconds=900),
    min_provider_budget=timedelta(seconds=1),
)
STALENESS = timedelta(seconds=90)
FITS = config_result(window_ms=600_000, retention_ms=3_600_000)
TOO_SMALL = config_result(window_ms=WORST_MS - 1, retention_ms=3_600_000)
UNAUTHORIZED = HttpResult(status=401)
UNREACHABLE = HttpResult(status=None)
INCONSISTENT = config_result(window_ms=600_000, retention_ms=600_000)  # retention <= window


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def make_gate() -> tuple[ConfigGate, RecordingAlerter, Clock]:
    alerter, clock = RecordingAlerter(), Clock()
    gate = ConfigGate(
        worst_case=POLICY.worst_case_window(), staleness=STALENESS, alerter=alerter, monotonic=clock
    )
    return gate, alerter, clock


async def feed(gate: ConfigGate, *results: HttpResult) -> None:
    client = ScriptedRefundClient(configs=results)
    for _ in results:
        gate.record(await read_config(client, SETTINGS))


def test_the_gate_starts_closed() -> None:
    gate, _, _ = make_gate()
    assert gate.closed_reason() == "missing"


async def test_a_valid_fitting_read_opens_it_quietly() -> None:
    gate, alerter, _ = make_gate()
    await feed(gate, FITS)
    assert gate.is_open() and alerter.raised == []


async def test_a_window_below_the_worst_case_closes_it_and_alerts() -> None:
    gate, alerter, _ = make_gate()
    await feed(gate, FITS, TOO_SMALL)
    assert gate.state is GateState.WINDOW_TOO_SMALL
    assert alerter.kinds(Severity.ALERT) == ["provider_window_below_worst_case"]
    await feed(gate, FITS)
    assert gate.is_open()


async def test_negative_control_a_window_exactly_at_the_worst_case_opens_it() -> None:
    gate, _, _ = make_gate()
    await feed(gate, config_result(window_ms=WORST_MS, retention_ms=3_600_000))
    assert gate.is_open()


async def test_401_closes_it_and_pages() -> None:
    gate, alerter, _ = make_gate()
    await feed(gate, FITS, UNAUTHORIZED)
    assert gate.state is GateState.AUTH_FAILED
    assert alerter.kinds(Severity.PAGE) == ["refund_api_auth_failed"]


async def test_negative_control_a_valid_token_opens_it_without_a_page() -> None:
    gate, alerter, _ = make_gate()
    await feed(gate, UNAUTHORIZED, FITS)
    assert gate.is_open()
    assert alerter.kinds(Severity.PAGE) == ["refund_api_auth_failed"], "only the 401 paged"


@pytest.mark.parametrize("failed", [UNREACHABLE, INCONSISTENT, HttpResult(status=503)])
async def test_a_failed_read_closes_it_even_while_the_cache_is_fresh(failed: HttpResult) -> None:
    """ADR-014 Part 4: a per-send read that fails stops claims until a read succeeds, so a
    released run isn't reclaimed into the same failure."""
    gate, alerter, _ = make_gate()
    await feed(gate, FITS, failed)
    assert gate.closed_reason() == "read_failed"
    assert alerter.kinds(Severity.ALERT) == ["provider_config_read_failed"]
    await feed(gate, FITS)
    assert gate.is_open()


class IgnoresFailedReads(ConfigGate):
    """The planted bug: only the interval's staleness closes the gate."""

    def record(self, read: ConfigRead) -> None:
        if read.kind is not ConfigReadKind.FAILED:
            super().record(read)


async def test_negative_control_a_gate_that_ignores_failed_reads_stays_open() -> None:
    gate = IgnoresFailedReads(
        worst_case=POLICY.worst_case_window(), staleness=STALENESS, alerter=RecordingAlerter()
    )
    await feed(gate, FITS, UNREACHABLE)
    assert gate.is_open(), "the worker would reclaim the run it just released"


async def test_it_goes_stale_after_the_staleness_limit() -> None:
    gate, _, clock = make_gate()
    await feed(gate, FITS)
    clock.now += STALENESS.total_seconds()
    assert gate.is_open(), "exactly at the limit is still fresh"
    clock.now += 0.001
    assert gate.closed_reason() == "stale"
    await feed(gate, FITS)
    assert gate.is_open(), "a fresh read reopens it"


async def test_startup_refuses_a_window_below_the_worst_case() -> None:
    gate, _, _ = make_gate()
    with pytest.raises(WorkerConfigError, match="worst case"):
        await startup_check(ScriptedRefundClient(configs=[TOO_SMALL]), SETTINGS, gate)


@pytest.mark.parametrize(
    ("result", "reason"), [(UNREACHABLE, "read_failed"), (UNAUTHORIZED, "auth_failed")]
)
async def test_startup_with_refund_api_down_or_a_bad_token_isnt_fatal(
    result: HttpResult, reason: str
) -> None:
    gate, _, _ = make_gate()
    await startup_check(ScriptedRefundClient(configs=[result]), SETTINGS, gate)
    assert gate.closed_reason() == reason


async def test_negative_control_startup_with_a_fitting_window_opens_the_gate() -> None:
    gate, _, _ = make_gate()
    await startup_check(ScriptedRefundClient(configs=[FITS]), SETTINGS, gate)
    assert gate.is_open()


class RaisingOnce(ScriptedRefundClient):
    """Raises on the first read, then holds the next read until the test has looked."""

    def __init__(self, then_config: HttpResult) -> None:
        super().__init__(then_config=then_config)
        self.proceed = asyncio.Event()

    async def read_config(self) -> HttpResult:
        if self.config_reads == 0:
            self.config_reads += 1
            raise RuntimeError("bug in the client")
        await self.proceed.wait()
        return await super().read_config()


async def test_refresh_keeps_going_and_an_unexpected_error_closes_the_gate() -> None:
    gate, alerter, _ = make_gate()
    await feed(gate, FITS)
    client = RaisingOnce(then_config=FITS)
    task = asyncio.create_task(refresh_forever(client, SETTINGS, gate, timedelta(seconds=0.01)))
    try:
        async with asyncio.timeout(2):
            while gate.state is not GateState.READ_FAILED:
                await asyncio.sleep(0.001)
            assert not gate.is_open(), "the error closed the gate"
            client.proceed.set()
            while client.config_reads < 3:
                await asyncio.sleep(0.005)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    assert gate.is_open(), "later refreshes reopened it"
    assert "provider_config_read_failed" in alerter.kinds(Severity.ALERT)


async def test_a_failed_reconcile_lookup_closes_the_gate_until_a_read_succeeds() -> None:
    gate, alerter, _ = make_gate()
    await feed(gate, FITS)
    assert gate.is_open()
    gate.lookup_failed()
    assert (gate.state, gate.closed_reason()) == (GateState.LOOKUP_FAILED, "lookup_failed")
    assert alerter.raised == [], "the reconciler raises its own alert"
    await feed(gate, FITS)
    assert gate.is_open()
