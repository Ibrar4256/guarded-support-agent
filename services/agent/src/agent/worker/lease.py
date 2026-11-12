"""Holding a claim: the heartbeat task and the liveness rule (ADR-004 step 7, ADR-014 S8).

The heartbeat runs as its own task, so a slow send or a lock wait never delays renewal.
- **0 rows renewed** means another worker owns the run: the work is cancelled at once, and
  nothing further is sent or written. Anything already sent is covered by key deduplication.
- **A step past its bound** (``WorkerTimings.max_step``): the heartbeat stops renewing and
  cancels the work, so a stuck coroutine can't keep its claim; the lease then expires and
  another worker takes over through fencing.
- **A database error** while renewing isn't a lost claim. The heartbeat tries again next
  interval; a worker that can't renew can't send anyway, because sends require a live lease
  (ADR-014 Part 3).
"""

import asyncio
import logging
import os
import socket
import time
import uuid
from collections.abc import Awaitable, Callable
from datetime import timedelta
from typing import NoReturn

from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine

from agent.db import claims
from agent.db.claims import Claim
from agent.worker.timings import WorkerTimings

logger = logging.getLogger(__name__)

# Errors that mean "couldn't renew this time", not "lost the claim". A module constant so the
# negative control can show what treating them as a loss would do.
RETRYABLE_HEARTBEAT_ERRORS: tuple[type[BaseException], ...] = (DBAPIError, TimeoutError)


class LostClaim(Exception):
    """Another worker owns the run now; stop without writing anything."""


class StepOverrun(Exception):
    """A step ran past its bound; renewal stopped so the lease can expire."""


def new_worker_id() -> str:
    """Unique per process start, so a restarted process never mistakes an old claim for its
    own (fencing by ``claim_version`` protects the row either way)."""
    return f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}"


class StepClock:
    """When the current step started, on the monotonic clock."""

    def __init__(self, max_step: timedelta, monotonic: Callable[[], float] = time.monotonic):
        self._max_step = max_step.total_seconds()
        self._monotonic = monotonic
        self._started = monotonic()

    def begin_step(self) -> None:
        self._started = self._monotonic()

    def overrun(self) -> bool:
        return self._monotonic() - self._started > self._max_step


async def heartbeat(
    engine: AsyncEngine, claim: Claim, timings: WorkerTimings, clock: StepClock
) -> NoReturn:
    """Renew the lease every interval until cancelled; raise when the claim must be given up."""
    while True:
        await asyncio.sleep(timings.heartbeat.total_seconds())
        if clock.overrun():
            raise StepOverrun(f"run {claim.run_id}: step past {timings.max_step}")
        try:
            async with claims.worker_transaction(
                engine,
                statement_timeout=timings.statement_timeout,
                lock_timeout=timings.lock_timeout,
            ) as conn:
                renewed = await claims.renew_lease(conn, claim, timings.lease)
        except RETRYABLE_HEARTBEAT_ERRORS:
            logger.warning("heartbeat for run %s failed; retrying", claim.run_id, exc_info=True)
            continue
        if not renewed:
            raise LostClaim(f"run {claim.run_id}: claim version {claim.claim_version} is gone")


async def hold_claim[T](
    engine: AsyncEngine,
    claim: Claim,
    timings: WorkerTimings,
    clock: StepClock,
    work: Callable[[], Awaitable[T]],
) -> T:
    """Run ``work`` while the heartbeat keeps the claim. Raises ``LostClaim`` or
    ``StepOverrun`` (with ``work`` cancelled) if the heartbeat gives the claim up."""
    beat = asyncio.create_task(heartbeat(engine, claim, timings, clock))
    job: asyncio.Task[T] = asyncio.ensure_future(work())
    try:
        done, _ = await asyncio.wait({beat, job}, return_when=asyncio.FIRST_COMPLETED)
        if job in done:
            return job.result()
        job.cancel()
        # Bounded: a coroutine that ignores cancellation mustn't hang the worker; its lease
        # is no longer renewed either way.
        await asyncio.wait({job}, timeout=timings.max_step.total_seconds())
        raise beat.exception() or RuntimeError("heartbeat stopped without an error")
    finally:
        for task in (beat, job):
            task.cancel()
        await asyncio.gather(beat, return_exceptions=True)
