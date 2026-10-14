"""Creating and looking up refunds: the idempotency protocol (ADR-004, ADR-010).

One request is one transaction, in this order:
1. Budget: the time left since receipt; abort if none (ADR-010 deadline).
2. Database deadline = clock_timestamp() + remaining; statement_timeout = remaining.
3. pg_try_advisory_xact_lock(key) — not acquired → 409, before anything else happens.
4. prune_expired_key (migration 0002), then look the key up: replay or 422.
5. Lock the charge row (FOR UPDATE), then the business checks → 400, nothing stored.
6. Insert the refund and the key with the exact response body.
7. clock_timestamp() < deadline, or roll back; then commit.
"""

import math
import time
from dataclasses import dataclass, field
from typing import Any

from psycopg import errors as pg_errors
from sqlalchemy import func, insert, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.exc import TimeoutError as PoolTimeoutError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from refund_api.config import Settings
from refund_api.db.schema import idempotency_keys, refunds
from refund_api.hooks import RefundHooks
from refund_api.models import (
    BusinessError,
    BusinessErrorCode,
    InFlight,
    KeyMismatch,
    Refund,
    RefundList,
    RefundRequest,
    Unavailable,
)
from refund_api.payload import payload_hash

REPLAYED_HEADER = "Idempotent-Replayed"


@dataclass(frozen=True)
class Outcome:
    status: int
    body: str
    headers: dict[str, str] = field(default_factory=dict)


class _Stop(Exception):
    """End the transaction with this outcome, rolling back anything it wrote."""

    def __init__(self, outcome: Outcome) -> None:
        super().__init__(outcome.status)
        self.outcome = outcome


def deadline_exceeded() -> Outcome:
    return Outcome(503, Unavailable(status="deadline_exceeded").model_dump_json())


def business_error(code: BusinessErrorCode) -> Outcome:
    return Outcome(400, BusinessError(code=code).model_dump_json())


def remaining_budget(settings: Settings, received_at: float) -> float:
    """Seconds left, measured on the monotonic clock from request receipt."""
    return settings.processing_budget.total_seconds() - (time.monotonic() - received_at)


async def lock_charge(connection: AsyncConnection, order_id: str) -> Any:
    """The charge row, locked FOR UPDATE until this transaction ends, so concurrent refunds
    on one order can't both pass the sum. Through a SECURITY DEFINER function (migration
    0003), because FOR UPDATE needs UPDATE privilege and charges are immutable to us."""
    query = text("SELECT * FROM public.lock_charge(:order_id)")
    return (await connection.execute(query, {"order_id": order_id})).one_or_none()


async def create_refund(
    engine: AsyncEngine,
    settings: Settings,
    hooks: RefundHooks,
    request: RefundRequest,
    idempotency_key: str,
    received_at: float,
) -> Outcome:
    await hooks.before_transaction(idempotency_key)
    try:
        async with engine.connect() as connection:
            remaining = remaining_budget(settings, received_at)
            if remaining <= 0:
                return deadline_exceeded()
            try:
                async with connection.begin():
                    return await _create_in_transaction(
                        connection, settings, hooks, request, idempotency_key, remaining
                    )
            except _Stop as stop:
                return stop.outcome
    except PoolTimeoutError:
        return deadline_exceeded()
    except DBAPIError as exc:
        if isinstance(exc.orig, pg_errors.QueryCanceled):  # statement_timeout fired
            return deadline_exceeded()
        raise


async def _create_in_transaction(
    connection: AsyncConnection,
    settings: Settings,
    hooks: RefundHooks,
    request: RefundRequest,
    idempotency_key: str,
    remaining: float,
) -> Outcome:
    deadline = (
        await connection.execute(
            text("SELECT clock_timestamp() + make_interval(secs => :seconds)"),
            {"seconds": remaining},
        )
    ).scalar_one()
    await connection.execute(
        text("SELECT set_config('statement_timeout', :ms, true)"),
        {"ms": str(math.ceil(remaining * 1000))},
    )

    locked = (
        await connection.execute(
            text("SELECT pg_try_advisory_xact_lock(:lock_id)"),
            {"lock_id": hooks.lock_key(idempotency_key)},
        )
    ).scalar_one()
    if not locked:
        raise _Stop(Outcome(409, InFlight().model_dump_json()))

    await connection.execute(
        text("SELECT public.prune_expired_key(:key, :retention)"),
        {"key": idempotency_key, "retention": settings.key_retention},
    )
    request_hash = payload_hash(**request.model_dump())
    stored = (
        await connection.execute(
            select(
                idempotency_keys.c.payload_hash,
                idempotency_keys.c.response_status,
                idempotency_keys.c.response_body,
            ).where(idempotency_keys.c.key == idempotency_key)
        )
    ).one_or_none()
    if stored is not None:
        if stored.payload_hash != request_hash:
            raise _Stop(Outcome(422, KeyMismatch().model_dump_json()))
        return Outcome(stored.response_status, stored.response_body, {REPLAYED_HEADER: "true"})

    charge = await lock_charge(connection, request.order_id)
    if charge is None:
        raise _Stop(business_error(BusinessErrorCode.CHARGE_NOT_FOUND))
    if charge.currency != request.currency:
        raise _Stop(business_error(BusinessErrorCode.CURRENCY_MISMATCH))
    already_refunded = (
        await connection.execute(
            select(func.coalesce(func.sum(refunds.c.amount_minor), 0)).where(
                refunds.c.order_id == request.order_id
            )
        )
    ).scalar_one()
    if already_refunded + request.amount_minor > charge.amount_minor:
        raise _Stop(business_error(BusinessErrorCode.REFUND_EXCEEDS_CHARGE))

    row = (
        await connection.execute(
            insert(refunds).values(**request.model_dump()).returning(*refunds.c)
        )
    ).one()
    body = Refund.model_validate(row._mapping).model_dump_json()
    await connection.execute(
        insert(idempotency_keys).values(
            key=idempotency_key,
            payload_hash=request_hash,
            refund_id=row.id,
            response_status=201,
            response_body=body,
        )
    )

    await hooks.before_commit(idempotency_key)
    in_time = (
        await connection.execute(
            text("SELECT clock_timestamp() < :deadline"), {"deadline": deadline}
        )
    ).scalar_one()
    if not in_time:
        raise _Stop(deadline_exceeded())
    return Outcome(201, body)


async def list_refunds(engine: AsyncEngine, order_id: str, action_id: str) -> str:
    """The reconcile lookup (ADR-004 step 5): every refund for this action, oldest first."""
    query = (
        select(refunds)
        .where(refunds.c.order_id == order_id, refunds.c.action_id == action_id)
        .order_by(refunds.c.created_at, refunds.c.id)
    )
    async with engine.connect() as connection:
        rows = (await connection.execute(query)).all()
    found = RefundList(refunds=[Refund.model_validate(row._mapping) for row in rows])
    return found.model_dump_json()
