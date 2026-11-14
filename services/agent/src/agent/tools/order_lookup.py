"""The order-lookup tool (ADR-020, PROJECT_BRIEF requirement 2, ADR-009 R02).

A missing order and another customer's order get the same bytes from the same code path and
the same queries. Only afterwards, with the answer already fixed, a best-effort existence
probe decides whether to write a ``cross_user_order_lookup`` line to the server-side security
log; it can't change the answer, even when it fails.
"""

import json
import logging
import re
from datetime import UTC

from pydantic import Field
from sqlalchemy.exc import DBAPIError

from agent.core.currency import UnknownCurrencyError, format_minor
from agent.core.policy import FulfillmentStatus
from agent.db.order_lookup import (
    OwnedOrder,
    find_owned_order,
    order_exists,
    record_lookup,
    turn_holds_run,
)
from agent.refund_client.models import ID_PATTERN
from agent.tools.contract import LostTurn, ToolArgs, ToolContext, canonical

logger = logging.getLogger(__name__)
security_log = logging.getLogger("agent.security")

ORDER_ID = re.compile(ID_PATTERN)

# The one not-found text (S3), for a missing order, another customer's and a malformed id (S1).
NOT_FOUND = canonical(
    {"error": "not_found", "message": "No order with that ID was found on this account."}
)
# Any database error, or a currency missing from the exponent table (S6): never "not found".
UNAVAILABLE = canonical(
    {"error": "unavailable", "message": "order lookup is temporarily unavailable"}
)


class OrderLookupArgs(ToolArgs):
    order_id: str = Field(description="The order ID the customer gave, for example ord_demo_1001.")


async def lookup_order(ctx: ToolContext, args: OrderLookupArgs) -> str:
    """Raises ``LostTurn`` if the turn lost its lease before recording a found order."""
    if ORDER_ID.fullmatch(args.order_id) is None:
        return NOT_FOUND
    try:
        async with ctx.conn.begin():
            order = await find_owned_order(ctx.conn, args.order_id, ctx.session_user)
            if order is not None:
                result = found_result(order)
                if not await turn_holds_run(ctx.conn, ctx.run_id, ctx.turn_id):
                    raise LostTurn(f"turn {ctx.turn_id} no longer holds run {ctx.run_id}")
                await record_lookup(ctx.conn, ctx.run_id, order.order_id)
                return result
    except DBAPIError:
        logger.warning("order lookup failed for run %s", ctx.run_id, exc_info=True)
        return UNAVAILABLE
    except UnknownCurrencyError:
        logger.error("order lookup for run %s: currency has no exponent", ctx.run_id, exc_info=True)
        return UNAVAILABLE
    answer = NOT_FOUND
    await log_if_cross_user(ctx, args.order_id)
    return answer


def found_result(order: OwnedOrder) -> str:
    """The order's own facts, nothing derived (Part 3): no refund state, no policy numbers."""
    result: dict[str, object] = {
        "order_id": order.order_id,
        "status": order.status.value,
        "charged_minor": order.charged_minor,
        "currency": order.currency,
        "charged_display": format_minor(order.charged_minor, order.currency),
    }
    if order.status is FulfillmentStatus.DELIVERED and order.delivered_at is not None:
        result["delivered_on"] = order.delivered_at.astimezone(UTC).date().isoformat()
    return canonical(result)


async def log_if_cross_user(ctx: ToolContext, order_id: str) -> None:
    """Runs for every well-formed not-found, in its own short transaction. Best-effort: a
    failure is logged and the caller's answer stays the same."""
    try:
        async with ctx.conn.begin():
            foreign = await order_exists(ctx.conn, order_id)
        if foreign:
            security_log.warning(
                json.dumps(
                    {
                        "kind": "cross_user_order_lookup",
                        "session_user": ctx.session_user,
                        "run_id": str(ctx.run_id),
                        "order_id": order_id,
                    },
                    sort_keys=True,
                )
            )
    except Exception:
        security_log.warning(
            "cross-user probe failed for run %s; answer unchanged", ctx.run_id, exc_info=True
        )
