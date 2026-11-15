"""Writing ``llm_calls`` rows (ADR-023 Part 4).

Each record is its own short, timeout-bounded transaction, independent of any turn transaction
and not conditional on the turn lease. The runtime role can insert, never update.
"""

from dataclasses import asdict
from datetime import timedelta

from sqlalchemy import insert
from sqlalchemy.ext.asyncio import AsyncEngine

from agent.db.claims import worker_transaction
from agent.db.schema import llm_calls
from agent.llm.recording import CallRecord


class DbCallRecorder:
    def __init__(
        self, engine: AsyncEngine, *, statement_timeout: timedelta, lock_timeout: timedelta
    ) -> None:
        self._engine = engine
        self._statement_timeout = statement_timeout
        self._lock_timeout = lock_timeout

    async def __call__(self, record: CallRecord) -> None:
        async with worker_transaction(
            self._engine,
            statement_timeout=self._statement_timeout,
            lock_timeout=self._lock_timeout,
        ) as conn:
            await conn.execute(insert(llm_calls).values(**asdict(record)))
