"""Database side of the order-lookup tool (ADR-020, PROJECT_BRIEF requirement 2, ADR-009 R02).

Nothing here commits on its own; the tool owns the transactions. Lock order (ADR-008):
``conversations`` before ``runs``.
"""

import uuid
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import exists, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncConnection

from agent.core.policy import FulfillmentStatus
from agent.db.schema import conversations, orders, run_order_lookups, runs


@dataclass(frozen=True)
class OwnedOrder:
    order_id: str
    status: FulfillmentStatus
    charged_minor: int
    currency: str
    delivered_at: datetime | None


async def find_owned_order(
    conn: AsyncConnection, order_id: str, session_user: str
) -> OwnedOrder | None:
    """The answering query: ownership is in the ``WHERE``, so another customer's row is
    never read into the code that builds the answer."""
    result = await conn.execute(
        select(
            orders.c.id,
            orders.c.status,
            orders.c.charged_minor,
            orders.c.currency,
            orders.c.delivered_at,
        ).where(orders.c.id == order_id, orders.c.owner_user_id == session_user)
    )
    row = result.one_or_none()
    return None if row is None else OwnedOrder(*row)


async def turn_holds_run(conn: AsyncConnection, run_id: uuid.UUID, turn_id: uuid.UUID) -> bool:
    """The turn lease fence (ADR-008, ADR-020 S7): this turn still holds the run's
    conversation. ``FOR SHARE`` keeps a takeover from committing before this transaction
    does, so a write made after a true answer can't land under a lost lease."""
    result = await conn.execute(
        select(conversations.c.id)
        .join(runs, runs.c.conversation_id == conversations.c.id)
        .where(runs.c.id == run_id, conversations.c.active_turn_id == turn_id)
        .with_for_update(read=True, of=conversations)
    )
    return result.one_or_none() is not None


async def record_lookup(conn: AsyncConnection, run_id: uuid.UUID, order_id: str) -> None:
    """R02 provenance: a second lookup of the same order in the same run is a no-op."""
    await conn.execute(
        pg_insert(run_order_lookups)
        .values(run_id=run_id, order_id=order_id)
        .on_conflict_do_nothing()
    )


async def order_exists(conn: AsyncConnection, order_id: str) -> bool:
    """The security-log probe: whether the id exists for anyone. Never feeds the answer."""
    result = await conn.execute(select(exists().where(orders.c.id == order_id)))
    return bool(result.scalar_one())
