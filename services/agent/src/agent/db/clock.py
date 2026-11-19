"""Time and ids on the turn path (ADR-026 Part 2).

Every turn-path write that stamps time takes one reading per transaction and writes it as
``COALESCE(:at, now())``. Production's clock reads ``None``, so the statement's own ``now()``
is written: exactly what the column default or the SQL used to write, one transaction's time.
The eval runner passes a logical clock instead (a fixed anchor plus one tick per reading), so
prompts and policy verdicts don't depend on the real day or on how long a call took.
Lease timestamps stay on the database clock; nothing reads them back into a prompt or a rule.
"""

import uuid
from collections.abc import Callable
from datetime import datetime
from typing import Protocol

from sqlalchemy import DateTime, func, literal
from sqlalchemy.sql.elements import ColumnElement

IdSource = Callable[[], uuid.UUID]


class Clock(Protocol):
    def stamp(self) -> datetime | None:
        """The time to write, or None for the statement's own SQL ``now()``."""
        ...


class DatabaseClock:
    """Production: every stamp is the database's ``now()`` of the writing transaction."""

    def stamp(self) -> datetime | None:
        return None


DATABASE_CLOCK = DatabaseClock()


def stamped(at: datetime | None) -> ColumnElement[datetime]:
    return func.coalesce(literal(at, DateTime(timezone=True)), func.now())
