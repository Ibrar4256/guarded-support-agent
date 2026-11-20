"""ADR-026 S2a: after a case, every ``timestamptz`` and ``date`` column in its database lies within
``[ANCHOR - seed horizon, ANCHOR + 1 day]``. A write that forgot the eval clock and fell back
to a column default lands on the real date and fails the case, whichever call site it was.

The columns come from the catalog, so a column added later is checked without touching this
file. Exempt: the turn lease (real time on purpose; it never reaches a prompt or a policy) and
``llm_calls.created_at`` (telemetry). A deadline column may sit its configured offset past the
range: ``runs.review_deadline`` is a stamp plus ``REVIEW_DEADLINE``.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta

from sqlalchemy import Connection, text

from agent_testing.evals.cases import SEED_HORIZON
from agent_testing.evals.clock import ANCHOR

EXEMPT = frozenset({"conversations.turn_expires_at", "llm_calls.created_at"})
CASE_SPAN = timedelta(days=1)


@dataclass(frozen=True)
class Violation:
    column: str
    value: datetime
    allowed: tuple[datetime, datetime]

    def __str__(self) -> str:
        low, high = self.allowed
        value = self.value.isoformat()
        return f"{self.column} = {value} outside [{low.isoformat()}, {high.isoformat()}]"


def timestamp_columns(conn: Connection) -> list[tuple[str, str]]:
    rows = conn.execute(
        text(
            "SELECT table_name, column_name FROM information_schema.columns "
            "WHERE table_schema = 'public' "
            "AND data_type IN ('timestamp with time zone', 'timestamp without time zone', 'date') "
            "ORDER BY table_name, column_name"
        )
    ).all()
    return [(table, column) for table, column in rows]


def violations(conn: Connection, offsets: Mapping[str, timedelta]) -> list[Violation]:
    """``offsets``: how far past the range each deadline column may sit."""
    low = ANCHOR - SEED_HORIZON
    found = []
    for table, column in timestamp_columns(conn):
        name = f"{table}.{column}"
        if name in EXEMPT:
            continue
        high = ANCHOR + CASE_SPAN + offsets.get(name, timedelta(0))
        row = conn.execute(
            text(f'SELECT min("{column}"), max("{column}") FROM "{table}"')  # catalog names
        ).one()
        for value in row:
            if value is None:
                continue
            instant = _instant(value)
            if not low <= instant <= high:
                found.append(Violation(name, instant, (low, high)))
    return found


def _instant(value: datetime | date) -> datetime:
    if isinstance(value, datetime):
        return value if value.tzinfo is not None else value.replace(tzinfo=UTC)
    return datetime.combine(value, time(12), tzinfo=UTC)
