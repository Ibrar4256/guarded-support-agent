"""Database side of an EXECUTING run's attempts and outcomes (ADR-004, ADR-008, ADR-012 Part 3,
ADR-014 Part 3, Part 4).

Every write is fenced by ``claim_version``. Every exit from EXECUTING clears the claim and bumps
the version (ADR-008, ADR-014 S9), so a stale worker can't write after the run has moved on.
"""

import uuid
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from sqlalchemy import func, insert, select, update
from sqlalchemy.ext.asyncio import AsyncConnection

from agent.core.lifecycle import RefundOutcome, RunStatus
from agent.core.replies import ReplyTemplate
from agent.db.claims import Claim, fenced
from agent.db.schema import events, runs


@dataclass(frozen=True)
class AttemptState:
    """The run as the attempt transaction sees it, row-locked, on the database clock."""

    conversation_id: uuid.UUID
    elapsed: timedelta  # now() - first_send_started_at
    send_attempts: int
    snapshot: timedelta  # send_window_ms
    idempotency_key: str | None
    canonical_payload: str | None


@dataclass(frozen=True)
class Age:
    conversation_id: uuid.UUID
    elapsed: timedelta
    snapshot: timedelta


async def lock_attempt_state(conn: AsyncConnection, claim: Claim) -> AttemptState | None:
    """ADR-014 Part 3: the claim must be ours **and the lease live**; a worker that couldn't
    renew can't send (S8). None means the claim is lost."""
    row = (
        await conn.execute(
            select(
                runs.c.conversation_id,
                (func.now() - runs.c.first_send_started_at).label("elapsed"),
                runs.c.send_attempts,
                runs.c.send_window_ms,
                runs.c.idempotency_key,
                runs.c.canonical_payload,
            )
            .where(
                fenced(claim),
                runs.c.status == RunStatus.EXECUTING,
                runs.c.lease_expires_at > func.now(),
            )
            .with_for_update()
        )
    ).one_or_none()
    if row is None:
        return None
    return AttemptState(
        conversation_id=row.conversation_id,
        elapsed=row.elapsed,
        send_attempts=row.send_attempts,
        snapshot=timedelta(milliseconds=row.send_window_ms),
        idempotency_key=row.idempotency_key,
        canonical_payload=row.canonical_payload,
    )


async def record_attempt(
    conn: AsyncConnection,
    claim: Claim,
    client_timeout: timedelta,
    processing_budget: timedelta,
) -> int:
    """The attempt record (ADR-012 Part 3): one more send, ``last_attempt_at = now()`` (the
    quiet-period anchor), and a lease that outlives the attempt (ADR-014 Part 3). Progress,
    so the release streak resets (S12). It also keeps ADR-016's bounds, each the maximum over
    the run's sends: the longest this send could take to commit (its timeout plus the budget
    read just before it) and the longest timeout. Returns the new attempt count."""
    row = (
        await conn.execute(
            update(runs)
            .where(fenced(claim), runs.c.status == RunStatus.EXECUTING)
            .values(
                send_attempts=runs.c.send_attempts + 1,
                last_attempt_at=func.now(),
                lease_expires_at=func.greatest(
                    runs.c.lease_expires_at, func.now() + client_timeout
                ),
                release_streak=0,
                in_flight_bound_ms=func.greatest(
                    runs.c.in_flight_bound_ms, _millis(client_timeout + processing_budget)
                ),
                max_client_timeout_ms=func.greatest(
                    runs.c.max_client_timeout_ms, _millis(client_timeout)
                ),
            )
            .returning(runs.c.send_attempts)
        )
    ).one()
    return int(row.send_attempts)


def _millis(duration: timedelta) -> int:
    """Rounded up, so a recorded bound is never shorter than the real one."""
    return -(-duration // timedelta(milliseconds=1))


async def age(conn: AsyncConnection, claim: Claim) -> Age | None:
    """ADR-014 Part 4: how old the run is against its own snapshot, without a fresh read."""
    row = (
        await conn.execute(
            select(
                runs.c.conversation_id,
                (func.now() - runs.c.first_send_started_at).label("elapsed"),
                runs.c.send_window_ms,
            ).where(fenced(claim), runs.c.status == RunStatus.EXECUTING)
        )
    ).one_or_none()
    if row is None:
        return None
    return Age(row.conversation_id, row.elapsed, timedelta(milliseconds=row.send_window_ms))


def _leave_executing(**values: Any) -> dict[str, Any]:
    return {
        "worker_id": None,
        "lease_expires_at": None,
        "claim_version": runs.c.claim_version + 1,
        "claimable_after": None,
        **values,
    }


async def _move(conn: AsyncConnection, claim: Claim, **values: Any) -> bool:
    result = await conn.execute(
        update(runs)
        .where(fenced(claim), runs.c.status == RunStatus.EXECUTING)
        .values(**_leave_executing(**values))
    )
    return result.rowcount == 1


async def _reply(
    conn: AsyncConnection,
    claim: Claim,
    conversation_id: uuid.UUID,
    kind: str,
    template: ReplyTemplate,
    **params: object,
) -> None:
    await conn.execute(
        insert(events).values(
            conversation_id=conversation_id,
            run_id=claim.run_id,
            kind=kind,
            payload={"template": template.value, **params},
        )
    )


async def confirm(
    conn: AsyncConnection,
    claim: Claim,
    conversation_id: uuid.UUID,
    *,
    order_id: str,
    amount_minor: int,
    currency: str,
) -> bool:
    """``EXECUTING → COMPLETED``, ``confirmed`` (ADR-008), with the "refund issued" reply."""
    if not await _move(
        conn,
        claim,
        status=RunStatus.COMPLETED,
        refund_outcome=RefundOutcome.CONFIRMED,
        release_streak=0,
    ):
        return False
    await _reply(
        conn,
        claim,
        conversation_id,
        "reply",
        ReplyTemplate.REFUND_ISSUED,
        order_id=order_id,
        amount_minor=amount_minor,
        currency=currency,
    )
    return True


async def reject(conn: AsyncConnection, claim: Claim, conversation_id: uuid.UUID) -> bool:
    """``EXECUTING → ESCALATED``, ``rejected``: a recognized business error, so no money moved
    and the capacity is freed (ADR-004, ADR-008, ADR-012)."""
    if not await _move(
        conn,
        claim,
        status=RunStatus.ESCALATED,
        refund_outcome=RefundOutcome.REJECTED,
        escalated_at=func.now(),
        release_streak=0,
    ):
        return False
    await _reply(conn, claim, conversation_id, "escalated", ReplyTemplate.NEEDS_PERSON)
    return True


async def to_reconciliation(conn: AsyncConnection, claim: Claim) -> bool:
    """``EXECUTING → NEEDS_RECONCILIATION`` (ADR-008): ``escalated_at``, outcome stays
    ``unknown``, the claim cleared so the reconciler doesn't wait for a lease (S9). No new
    customer event: "your refund is being processed" still holds."""
    return await _move(conn, claim, status=RunStatus.NEEDS_RECONCILIATION, escalated_at=func.now())
