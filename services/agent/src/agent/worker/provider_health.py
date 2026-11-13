"""Provider breakers: refund-api's health on one path, judged by that path's real requests
(ADR-018). Not ADR-009's breaker, which is a policy control on refund volume.

One instance per path: the write breaker is fed by sends and gates the send loop, the read
breaker is fed by lookups and gates the reconcile loop. A read succeeding says nothing about
writes, and the reverse. Config reads feed neither: ``GET /config`` never touches refund-api's
database, so it can't see the outages these exist for.

- **Closed → open** after ``threshold`` failures in a row (``core.refund_outcome.Health``).
- **Open:** the loop doesn't claim, and a run between attempts doesn't send unless it is the
  probe.
- **Half-open** once the open interval has passed: one claim is let through and its next request
  on this path is the probe. Success closes; failure re-opens with the interval doubled, capped; a
  probe claim that ends without a request (or with a neutral answer) frees the slot (S4).
- **Starts half-open** (S5): after a restart during an outage the first request is a probe.
- **Signals** (S8): each opening alerts; an outage longer than ``page_after`` pages, both under
  ``provider_<path>_open``, rate-limited by the alerter.

Single-threaded by design: each breaker is used by one loop on one event loop, and no method
awaits.
"""

import logging
import time
from collections.abc import Callable
from datetime import timedelta
from enum import StrEnum

from agent.alerts import Alerter
from agent.core.refund_outcome import Health
from agent.worker.timings import WorkerTimings

logger = logging.getLogger(__name__)


class BreakerState(StrEnum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class ProviderBreaker:
    def __init__(
        self,
        path: str,
        *,
        threshold: int,
        open_for: timedelta,
        max_open_for: timedelta,
        page_after: timedelta,
        alerter: Alerter,
        monotonic: Callable[[], float] = time.monotonic,
        initial: BreakerState = BreakerState.HALF_OPEN,
    ) -> None:
        if threshold < 1:
            raise ValueError("threshold must be at least 1")
        if open_for <= timedelta(0) or max_open_for < open_for:
            raise ValueError("open_for must be positive and at most max_open_for")
        self.path = path
        self.kind = f"provider_{path}_open"
        self._threshold = threshold
        self._base = open_for.total_seconds()
        self._max = max_open_for.total_seconds()
        self._page_after = page_after.total_seconds()
        self._alerter = alerter
        self._monotonic = monotonic
        self._state = initial
        self._failures = 0
        self._interval = self._base
        self._reopen_at = 0.0
        self._probing = False
        self._outage_since: float | None = None
        # Successful requests on this path; the read breaker's count is ADR-017's evidence (S7).
        self.successes = 0

    @property
    def state(self) -> BreakerState:
        if self._state is BreakerState.OPEN and self._monotonic() >= self._reopen_at:
            self._state = BreakerState.HALF_OPEN
            self._probing = False
        return self._state

    def allows_claim(self) -> bool:
        state = self.state
        return state is BreakerState.CLOSED or (
            state is BreakerState.HALF_OPEN and not self._probing
        )

    def probing(self) -> bool:
        """Whether the next claim would be the probe."""
        return self.state is BreakerState.HALF_OPEN and not self._probing

    def take_probe(self) -> None:
        if not self.probing():
            raise RuntimeError(f"{self.path} breaker has no probe slot to take")
        self._probing = True

    def release_probe(self) -> None:
        """S4: the probe claim ended without a request; the next claim may probe."""
        self._probing = False

    def allows_attempt(self) -> bool:
        """Between attempts (S2): closed, or this run holds the probe slot."""
        state = self.state
        return state is BreakerState.CLOSED or (state is BreakerState.HALF_OPEN and self._probing)

    def in_outage(self) -> bool:
        """Opened and not yet closed again (S9). False at a fresh start, though half-open."""
        return self._outage_since is not None

    def next_open_for(self) -> timedelta:
        """How long it would stay open if the current probe failed (S3's doomed runs)."""
        return timedelta(seconds=self._next_interval())

    def record(self, health: Health) -> None:
        if health is Health.NEUTRAL:
            self._probing = False  # S4: a neutral answer settles nothing
            return
        if health is Health.SUCCESS:
            self.successes += 1
            if self._state is not BreakerState.CLOSED:
                logger.info("provider %s breaker closed", self.path)
            self._state = BreakerState.CLOSED
            self._failures = 0
            self._interval = self._base
            self._probing = False
            self._outage_since = None
            return
        state = self.state
        if state is BreakerState.HALF_OPEN:
            self._open(self._next_interval())
        elif state is BreakerState.CLOSED:
            self._failures += 1
            if self._failures >= self._threshold:
                self._open(self._base)
        # OPEN: a request already on the wire when it opened; nothing more to learn.

    def check_page(self) -> None:
        """Called by the loop each pass: an outage past ``page_after`` pages (S8)."""
        if self._outage_since is None:
            return
        lasted = self._monotonic() - self._outage_since
        if lasted > self._page_after:
            self._alerter.page(self.kind, open_seconds=round(lasted), state=self.state.value)

    def _next_interval(self) -> float:
        if self._outage_since is None:
            return self._base
        return min(self._interval * 2, self._max)

    def _open(self, interval: float) -> None:
        now = self._monotonic()
        self._state = BreakerState.OPEN
        self._interval = interval
        self._reopen_at = now + interval
        self._probing = False
        self._failures = 0
        if self._outage_since is None:
            self._outage_since = now
        self._alerter.alert(self.kind, open_seconds=interval)


def provider_breakers(
    timings: WorkerTimings, alerter: Alerter, monotonic: Callable[[], float] = time.monotonic
) -> tuple[ProviderBreaker, ProviderBreaker]:
    """The write and read breakers, as production builds them: half-open (S5)."""

    def breaker(path: str) -> ProviderBreaker:
        return ProviderBreaker(
            path,
            threshold=timings.provider_failure_threshold,
            open_for=timings.provider_open,
            max_open_for=timings.provider_max_open,
            page_after=timings.provider_page_after,
            alerter=alerter,
            monotonic=monotonic,
        )

    return breaker("write"), breaker("read")
