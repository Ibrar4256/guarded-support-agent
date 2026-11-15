"""The connection a tool gets (ADR-022 S3): the turn's timeouts already set.

Tools open their own transactions (ADR-020), so ``SET LOCAL`` from outside can't reach them; the
timeouts are set for the session instead and reset before the connection goes back to the pool,
so they never leak to the next user of that connection. If the reset fails, the connection is
invalidated rather than returned with the settings still on it.
"""

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import timedelta

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from agent.turn.settings import TurnSettings

logger = logging.getLogger(__name__)


def _millis(duration: timedelta) -> str:
    return f"{max(1, round(duration.total_seconds() * 1000))}ms"


@asynccontextmanager
async def tool_connection(
    engine: AsyncEngine, settings: TurnSettings
) -> AsyncIterator[AsyncConnection]:
    async with engine.connect() as conn:
        await conn.execute(
            text(
                "SELECT set_config('statement_timeout', :statement, false), "
                "set_config('lock_timeout', :lock, false)"
            ),
            {
                "statement": _millis(settings.statement_timeout),
                "lock": _millis(settings.lock_timeout),
            },
        )
        await conn.commit()
        try:
            yield conn
        finally:
            try:
                await conn.rollback()
                await conn.execute(text("RESET statement_timeout"))
                await conn.execute(text("RESET lock_timeout"))
                await conn.commit()
            except Exception:
                logger.warning("tool connection reset failed; discarding it", exc_info=True)
                await conn.invalidate()
