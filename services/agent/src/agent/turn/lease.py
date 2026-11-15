"""Holding the turn lease while the turn works (ADR-008: "the turn heartbeats like a worker").

- **0 rows renewed:** another turn took the conversation over; the work is cancelled and
  ``LostTurn`` raised. Every write is fenced anyway (S5), so nothing stale lands.
- **Past the deadline plus one statement timeout:** renewal stops and the work is cancelled
  (``TurnOverrun``), so a stuck turn can't keep the conversation. The extra statement timeout is
  the time the loop's own deadline handling needs to write its ``try_again`` under the lease.
- **A database error while renewing** isn't a lost turn: the heartbeat tries again next interval.
"""

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from typing import NoReturn

from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine

from agent.db import turns
from agent.db.claims import worker_transaction
from agent.tools.contract import LostTurn
from agent.turn.settings import TurnSettings
from agent.turn.start import TurnIds

logger = logging.getLogger(__name__)

# "Couldn't renew this time", not "lost the turn"; a module constant for the negative control.
RETRYABLE_HEARTBEAT_ERRORS: tuple[type[BaseException], ...] = (DBAPIError, TimeoutError)


class TurnOverrun(Exception):
    """The turn ran past its deadline; renewal stopped so the lease can expire."""


async def heartbeat(
    engine: AsyncEngine,
    settings: TurnSettings,
    ids: TurnIds,
    deadline: float,
    monotonic: Callable[[], float],
) -> NoReturn:
    give_up_at = deadline + settings.statement_timeout.total_seconds()
    while True:
        await asyncio.sleep(settings.heartbeat.total_seconds())
        if monotonic() > give_up_at:
            raise TurnOverrun(f"turn {ids.turn_id} ran past its deadline")
        try:
            async with worker_transaction(
                engine,
                statement_timeout=settings.statement_timeout,
                lock_timeout=settings.lock_timeout,
            ) as conn:
                renewed = await turns.renew_lease(
                    conn, ids.conversation_id, ids.turn_id, settings.lease
                )
        except RETRYABLE_HEARTBEAT_ERRORS:
            logger.warning("turn %s: heartbeat failed; retrying", ids.turn_id, exc_info=True)
            continue
        if not renewed:
            raise LostTurn(f"turn {ids.turn_id} lost conversation {ids.conversation_id}")


async def hold_lease[T](
    engine: AsyncEngine,
    settings: TurnSettings,
    ids: TurnIds,
    deadline: float,
    work: Callable[[], Awaitable[T]],
    monotonic: Callable[[], float] = time.monotonic,
) -> T:
    """Run ``work`` while the heartbeat keeps the lease. Raises ``LostTurn`` or ``TurnOverrun``
    (with ``work`` cancelled) if the heartbeat gives the turn up."""
    beat = asyncio.create_task(heartbeat(engine, settings, ids, deadline, monotonic))
    job: asyncio.Task[T] = asyncio.ensure_future(work())
    try:
        done, _ = await asyncio.wait({beat, job}, return_when=asyncio.FIRST_COMPLETED)
        if job in done:
            return job.result()
        job.cancel()
        # Bounded: work that ignores cancellation mustn't hang the request.
        await asyncio.wait({job}, timeout=settings.statement_timeout.total_seconds())
        raise beat.exception() or RuntimeError("heartbeat stopped without an error")
    finally:
        for task in (beat, job):
            task.cancel()
        await asyncio.gather(beat, return_exceptions=True)
