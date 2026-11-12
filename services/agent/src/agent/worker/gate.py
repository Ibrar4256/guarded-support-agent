"""The claim gate (ADR-012 Part 4, ADR-013, ADR-014 Part 4, S4).

The worker claims runs only while the gate is open: the last provider-config read was valid,
authorized, its window fits the agent's worst case, and it's no older than the staleness
limit. The gate never bounds a send (each send reads the config itself, ADR-013); it only
decides whether to take new work.

Every read feeds it, the interval refresh and each per-send read alike:
- a valid read whose window fits opens it;
- **a failed or invalid read closes it until a read succeeds** (ADR-014 Part 4: refund-api
  isn't answering, so a released run mustn't be reclaimed into the same failure);
- 401/403 closes it and **pages**: a wrong token would otherwise stop every refund;
- a window below the worst case closes it and alerts; at startup that's fatal.
Unreachable at startup is not fatal: the worker just doesn't send yet.
"""

import asyncio
import logging
import time
from collections.abc import Callable
from datetime import timedelta
from enum import StrEnum
from typing import NoReturn

from agent.alerts import Alerter
from agent.core.refund_outcome import (
    ConfigRead,
    ConfigReadKind,
    ProviderConfig,
    read_provider_config,
)
from agent.refund_client.config import RefundClientSettings
from agent.refund_client.http import RefundClient
from agent.worker.timings import WorkerConfigError

logger = logging.getLogger(__name__)


class GateState(StrEnum):
    MISSING = "missing"  # no valid read yet
    OPEN = "open"
    READ_FAILED = "read_failed"
    AUTH_FAILED = "auth_failed"
    WINDOW_TOO_SMALL = "window_too_small"


class ConfigGate:
    def __init__(
        self,
        *,
        worst_case: timedelta,
        staleness: timedelta,
        alerter: Alerter,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._worst_case = worst_case
        self._staleness = staleness.total_seconds()
        self._alerter = alerter
        self._monotonic = monotonic
        self._state = GateState.MISSING
        self._fresh_at: float | None = None
        self._config: ProviderConfig | None = None

    @property
    def state(self) -> GateState:
        return self._state

    @property
    def config(self) -> ProviderConfig | None:
        """The last valid config. For alerting and the reconciler's guard, never a send bound."""
        return self._config

    def closed_reason(self) -> str | None:
        """None when the worker may claim; otherwise why not."""
        if self._state is not GateState.OPEN:
            return self._state.value
        assert self._fresh_at is not None
        if self._monotonic() - self._fresh_at > self._staleness:
            return "stale"
        return None

    def is_open(self) -> bool:
        return self.closed_reason() is None

    def record(self, read: ConfigRead) -> None:
        if read.kind is ConfigReadKind.OK:
            assert read.config is not None
            self._config = read.config
            if read.config.max_retry_window < self._worst_case:
                self._state = GateState.WINDOW_TOO_SMALL
                self._alerter.alert(
                    "provider_window_below_worst_case",
                    window_seconds=read.config.max_retry_window.total_seconds(),
                    worst_case_seconds=self._worst_case.total_seconds(),
                )
                return
            self._state = GateState.OPEN
            self._fresh_at = self._monotonic()
        elif read.kind is ConfigReadKind.AUTH_FAILED:
            self._state = GateState.AUTH_FAILED
            self._alerter.page("refund_api_auth_failed", reason=read.reason)
        else:
            self._state = GateState.READ_FAILED
            self._alerter.alert("provider_config_read_failed", reason=read.reason)


async def read_config(client: RefundClient, settings: RefundClientSettings) -> ConfigRead:
    """One ``GET /config``, validated by the core (ADR-013)."""
    return read_provider_config(
        await client.read_config(), settings.policy.client_timeout, settings.max_provider_window
    )


async def startup_check(
    client: RefundClient, settings: RefundClientSettings, gate: ConfigGate
) -> None:
    """A reported window smaller than the worst case is fatal for the worker: the
    configuration could never finish a retry schedule. Anything else just leaves the gate
    closed (ADR-012 Part 4)."""
    gate.record(await read_config(client, settings))
    if gate.state is GateState.WINDOW_TOO_SMALL:
        raise WorkerConfigError(
            "refund-api's retry window is smaller than the agent's worst case "
            f"({settings.policy.worst_case_window().total_seconds():g}s)"
        )


async def refresh_forever(
    client: RefundClient, settings: RefundClientSettings, gate: ConfigGate, interval: timedelta
) -> NoReturn:
    """Refresh the gate every interval until cancelled, starting one interval in (startup
    has just read it). An unexpected error closes the gate and alerts; it never ends the loop
    with a gate left open."""
    while True:
        await asyncio.sleep(interval.total_seconds())
        try:
            read = await read_config(client, settings)
        except Exception as exc:
            logger.exception("config refresh raised")
            read = ConfigRead(ConfigReadKind.FAILED, f"config refresh raised {type(exc).__name__}")
        gate.record(read)
