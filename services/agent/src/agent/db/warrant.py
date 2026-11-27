"""What the warrant check reads (ADR-029 W01), in a short transaction with no customer lock: the
conversation's customer messages and the orders looked up in any of its runs. Not the
assistant's replies, tool results or the summary: the evidence is what the customer wrote."""

import uuid
from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncConnection

from agent.db.order_lookup import OwnedOrder
from agent.db.schema import events, orders, run_order_lookups, runs
from agent.db.turns import CUSTOMER_MESSAGE


@dataclass(frozen=True)
class WarrantFacts:
    customer_messages: tuple[str, ...]  # oldest first
    orders: tuple[OwnedOrder, ...]  # the refund's order first, then the most recently looked up


async def load_warrant_facts(
    conn: AsyncConnection,
    *,
    conversation_id: uuid.UUID,
    user: str,
    refund_order_id: str,
    max_orders: int,
) -> WarrantFacts:
    texts = await conn.execute(
        select(events.c.payload["text"].astext)
        .where(events.c.conversation_id == conversation_id, events.c.kind == CUSTOMER_MESSAGE)
        .order_by(events.c.id)
    )
    last_looked_up = func.max(run_order_lookups.c.looked_up_at)
    found = await conn.execute(
        select(
            orders.c.id,
            orders.c.status,
            orders.c.charged_minor,
            orders.c.currency,
            orders.c.delivered_at,
        )
        .join(run_order_lookups, run_order_lookups.c.order_id == orders.c.id)
        .join(runs, runs.c.id == run_order_lookups.c.run_id)
        .where(runs.c.conversation_id == conversation_id, orders.c.owner_user_id == user)
        .group_by(orders.c.id)
        .order_by((orders.c.id == refund_order_id).desc(), last_looked_up.desc(), orders.c.id)
        .limit(max_orders)
    )
    return WarrantFacts(
        customer_messages=tuple(str(t) for t in texts.scalars() if t is not None),
        orders=tuple(OwnedOrder(*row) for row in found),
    )
