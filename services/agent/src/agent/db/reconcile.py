"""The reconciler's claim and reads (ADR-008 Part 2, ADR-016 Part 2, Part 4, S1).

- **Claim** (S1): ADR-014's pattern, one ``UPDATE`` over a ``SKIP LOCKED`` subselect with every
  condition repeated in the outer ``WHERE``, and its own named conditions: status
  ``NEEDS_RECONCILIATION``, lease free, cooldown passed, quiet period passed. Oldest
  ``last_attempt_at`` first. No R14 hold: the kill switch acts on the decision (Part 4).
- **Quiet period** (Part 2), on the database clock:
  ``last_attempt_at + greatest(in_flight_bound, max_client_timeout + budget_now) + margin``.
  The recorded bound covers a budget that has since gone down, the fresh budget one raised
  around the send, the recorded timeout an agent redeployed with a shorter one. Nothing sent
  (both bounds ``NULL``, kept so by a ``CHECK``): only the margin.
"""

import uuid
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from sqlalchemy import ColumnElement, FromClause, Interval, func, literal_column, select, update
from sqlalchemy.ext.asyncio import AsyncConnection

from agent.core.lifecycle import RECONCILER_CLAIMABLE, RunStatus
from agent.db import claims
from agent.db.claims import Claim, cooldown_passed, current_controls, fenced, lease_free
from agent.db.schema import runs

MILLISECOND = literal_column("interval '1 millisecond'", Interval)
# Bounds the stale-unknown scan per pass; the oldest come first.
STALE_SCAN_LIMIT = 100


@dataclass(frozen=True)
class ReconcileState:
    """The claimed run as the reconciler sees it, row-locked."""

    conversation_id: uuid.UUID
    canonical_payload: str | None
    # ADR-009's kill switch, read in the transaction that decides. NULL (no controls row)
    # holds, like every other reader of the switch (fail closed).
    sends_enabled: bool | None


@dataclass(frozen=True)
class StaleRun:
    run_id: uuid.UUID
    unresolved: timedelta


def quiet_millis(r: FromClause, budget_now: timedelta) -> ColumnElement[Any]:
    """How long after its last attempt a run may still commit at refund-api. ``greatest``
    skips NULLs, so a run with nothing sent gets NULL here and then 0."""
    return func.coalesce(
        func.greatest(r.c.in_flight_bound_ms, r.c.max_client_timeout_ms + _millis(budget_now)),
        0,
    )


def reconcile_conditions(
    r: FromClause, budget_now: timedelta, margin: timedelta
) -> dict[str, ColumnElement[bool]]:
    """What makes a run claimable by the reconciler. Named, so each can be tested alone."""
    quiet = (quiet_millis(r, budget_now) + _millis(margin)) * MILLISECOND
    return {
        "status": r.c.status.in_(sorted(RECONCILER_CLAIMABLE)),
        "lease_free": lease_free(r),
        "cooldown_passed": cooldown_passed(r),
        "quiet_passed": r.c.last_attempt_at + quiet < func.now(),
    }


async def claim_for_reconcile(
    conn: AsyncConnection,
    worker_id: str,
    lease: timedelta,
    *,
    budget_now: timedelta,
    margin: timedelta,
) -> Claim | None:
    candidate = runs.alias("candidate")
    pick = (
        select(candidate.c.id)
        .where(*reconcile_conditions(candidate, budget_now, margin).values())
        .order_by(candidate.c.last_attempt_at, candidate.c.id)
        .limit(1)
        .with_for_update(of=candidate, skip_locked=claims.SKIP_LOCKED)
        .correlate(None)
    )
    statement = (
        update(runs)
        .where(
            runs.c.id == pick.scalar_subquery(),
            *reconcile_conditions(runs, budget_now, margin).values(),
        )
        .values(
            worker_id=worker_id,
            lease_expires_at=func.now() + lease,
            claim_version=runs.c.claim_version + 1,
            claimable_after=None,
        )
        .returning(runs.c.id, runs.c.status, runs.c.claim_version)
    )
    row = (await conn.execute(statement)).one_or_none()
    if row is None:
        return None
    return Claim(
        run_id=row.id, status=row.status, claim_version=row.claim_version, worker_id=worker_id
    )


async def lock_state(conn: AsyncConnection, claim: Claim) -> ReconcileState | None:
    """The claim must be ours and the lease live. None means the claim is lost."""
    row = (
        await conn.execute(
            select(
                runs.c.conversation_id,
                runs.c.canonical_payload,
                current_controls().sends_enabled.label("sends_enabled"),
            )
            .where(
                fenced(claim),
                runs.c.status == RunStatus.NEEDS_RECONCILIATION,
                runs.c.lease_expires_at > func.now(),
            )
            .with_for_update(of=runs)
        )
    ).one_or_none()
    if row is None:
        return None
    return ReconcileState(row.conversation_id, row.canonical_payload, row.sends_enabled)


async def stale_unknown(conn: AsyncConnection, after: timedelta) -> list[StaleRun]:
    """``NEEDS_RECONCILIATION`` runs unresolved for longer than ``after`` (ADR-004), measured
    from when they first needed attention (``escalated_at``; the first send if it's missing,
    which an uncertain run always has)."""
    since = func.coalesce(runs.c.escalated_at, runs.c.first_send_started_at)
    rows = await conn.execute(
        select(runs.c.id, (func.now() - since).label("unresolved"))
        .where(runs.c.status == RunStatus.NEEDS_RECONCILIATION, since < func.now() - after)
        .order_by(since, runs.c.id)
        .limit(STALE_SCAN_LIMIT)
    )
    return [StaleRun(row.id, row.unresolved) for row in rows]


def _millis(duration: timedelta) -> int:
    """Rounded up, so a bound is never shorter than the duration it stands for."""
    return -(-duration // timedelta(milliseconds=1))
