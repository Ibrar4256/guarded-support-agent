"""Database side of the turn's memory (ADR-002, ADR-022 Part 1, S6).

- The rolling summary: an append-only row per summary. Readers take the row that **covers the
  latest event**, never the newest insert, and a writer inserts only if no row already covers a
  later event, so a late or concurrent summary of older events can't replace a newer one.
- The past-ticket digest's rows: the session user's other runs, newest first, read by code
  (never by a model decision) and scoped by the user id the session supplies.

Nothing here commits on its own.
"""

import uuid
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import exists, insert, literal, select
from sqlalchemy.ext.asyncio import AsyncConnection

from agent.core.lifecycle import RefundOutcome, RunStatus
from agent.db.clock import stamped
from agent.db.schema import conversation_summaries, runs


@dataclass(frozen=True)
class StoredSummary:
    text: str
    covers_through_event_id: int


@dataclass(frozen=True)
class PastRun:
    run_id: uuid.UUID
    created_at: datetime
    status: RunStatus
    refund_outcome: RefundOutcome
    order_id: str | None
    amount_minor: int | None
    currency: str | None


async def latest_summary(conn: AsyncConnection, conversation_id: uuid.UUID) -> StoredSummary | None:
    row = (
        await conn.execute(
            select(
                conversation_summaries.c.summary, conversation_summaries.c.covers_through_event_id
            )
            .where(conversation_summaries.c.conversation_id == conversation_id)
            .order_by(
                conversation_summaries.c.covers_through_event_id.desc(),
                conversation_summaries.c.id.desc(),
            )
            .limit(1)
        )
    ).one_or_none()
    return None if row is None else StoredSummary(row.summary, row.covers_through_event_id)


async def insert_summary_if_newer(
    conn: AsyncConnection,
    conversation_id: uuid.UUID,
    *,
    text: str,
    covers_through_event_id: int,
    prompt_version: str,
    provider: str,
    model: str,
    at: datetime | None,
) -> bool:
    """False when a row already covers this event or a later one: the new text is dropped."""
    newer = exists().where(
        conversation_summaries.c.conversation_id == conversation_id,
        conversation_summaries.c.covers_through_event_id >= covers_through_event_id,
    )
    values = select(
        literal(conversation_id),
        literal(covers_through_event_id),
        literal(text),
        literal(prompt_version),
        literal(provider),
        literal(model),
        stamped(at),
    ).where(~newer)
    result = await conn.execute(
        insert(conversation_summaries)
        .from_select(
            [
                "conversation_id",
                "covers_through_event_id",
                "summary",
                "prompt_version",
                "provider",
                "model",
                "created_at",
            ],
            values,
        )
        .returning(conversation_summaries.c.id)
    )
    return result.one_or_none() is not None


async def past_runs(
    conn: AsyncConnection, user: str, *, exclude_run: uuid.UUID, limit: int
) -> list[PastRun]:
    """The session user's last ``limit`` runs other than the current one (ADR-002's "tickets")."""
    rows = await conn.execute(
        select(
            runs.c.id,
            runs.c.created_at,
            runs.c.status,
            runs.c.refund_outcome,
            runs.c.order_id,
            runs.c.amount_minor,
            runs.c.currency,
        )
        .where(runs.c.user_id == user, runs.c.id != exclude_run)
        .order_by(runs.c.created_at.desc(), runs.c.id.desc())
        .limit(limit)
    )
    return [
        PastRun(
            run_id=r.id,
            created_at=r.created_at,
            status=RunStatus(r.status),
            refund_outcome=RefundOutcome(r.refund_outcome),
            order_id=r.order_id,
            amount_minor=r.amount_minor,
            currency=r.currency,
        )
        for r in rows
    ]
