"""Claiming runs: lease plus fencing (ADR-004 step 7, ADR-008, ADR-009 R14, ADR-014).

- **Claim** (ADR-014 Part 2): one ``UPDATE`` whose subselect picks a claimable run with
  ``FOR UPDATE SKIP LOCKED``. The outer ``WHERE`` repeats every condition, so the
  compare-and-set is the guarantee and ``SKIP LOCKED`` only stops workers queueing on one row.
  ``EXECUTING`` runs first (they're aging against their window), then the oldest approval.
- **Fencing**: every write a worker makes is conditional on the ``claim_version`` it claimed.
- **Release** (S9, S12): always bumps ``claim_version``, so the releasing worker's version is
  dead the moment it lets go. A cooldown release also sets ``claimable_after`` and counts
  toward ``release_streak``.
- Lease times use the database clock (``now()``), never the worker's.
"""

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from sqlalchemy import ColumnElement, FromClause, and_, func, or_, select, text, update
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from agent.core.capacity import ApprovalSource
from agent.core.lifecycle import WORKER_CLAIMABLE, RunStatus
from agent.db.schema import policy_controls, runs

# Kept as a module constant so the negative control can show what SKIP LOCKED buys.
SKIP_LOCKED = True
CONTROLS_ROW_ID = 1


@dataclass(frozen=True)
class Claim:
    run_id: uuid.UUID
    status: RunStatus
    claim_version: int
    worker_id: str


@dataclass(frozen=True)
class Release:
    released: bool
    release_streak: int | None = None


@dataclass(frozen=True)
class Controls:
    """The R14 controls, read as scalar subqueries of the one ``policy_controls`` row. A
    missing row reads as NULL, which holds every APPROVED run (fail closed)."""

    sends_enabled: ColumnElement[Any]
    breaker_tripped_at: ColumnElement[Any]


def current_controls() -> Controls:
    def value(column: ColumnElement[Any]) -> ColumnElement[Any]:
        return select(column).where(policy_controls.c.id == CONTROLS_ROW_ID).scalar_subquery()

    return Controls(
        sends_enabled=value(policy_controls.c.sends_enabled),
        breaker_tripped_at=value(policy_controls.c.breaker_tripped_at),
    )


def claim_conditions(r: FromClause, controls: Controls) -> dict[str, ColumnElement[bool]]:
    """What makes a run claimable by a worker. Named, so each one can be tested on its own."""
    return {
        "status": r.c.status.in_(sorted(WORKER_CLAIMABLE)),
        "lease_free": or_(r.c.worker_id.is_(None), r.c.lease_expires_at < func.now()),
        "cooldown_passed": or_(r.c.claimable_after.is_(None), r.c.claimable_after <= func.now()),
        # R14 (ADR-009): the kill switch holds every APPROVED run, the breaker holds all but
        # reviewer-approved ones. EXECUTING runs may already be sent and are never held.
        "not_held": or_(
            r.c.status == RunStatus.EXECUTING,
            and_(
                controls.sends_enabled,
                or_(
                    controls.breaker_tripped_at.is_(None),
                    r.c.approval_source == ApprovalSource.REVIEWER,
                ),
            ),
        ),
    }


def fenced(claim: Claim) -> ColumnElement[bool]:
    """The condition every write by the claiming worker carries (ADR-004 step 7)."""
    return and_(runs.c.id == claim.run_id, runs.c.claim_version == claim.claim_version)


async def claim_run(conn: AsyncConnection, worker_id: str, lease: timedelta) -> Claim | None:
    candidate = runs.alias("candidate")
    pick = (
        select(candidate.c.id)
        .where(*claim_conditions(candidate, current_controls()).values())
        .order_by(
            (candidate.c.status == RunStatus.EXECUTING).desc(),
            candidate.c.approved_at,
            candidate.c.id,
        )
        .limit(1)
        .with_for_update(of=candidate, skip_locked=SKIP_LOCKED)
        .correlate(None)
    )
    statement = (
        update(runs)
        .where(
            runs.c.id == pick.scalar_subquery(),
            *claim_conditions(runs, current_controls()).values(),
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


async def renew_lease(conn: AsyncConnection, claim: Claim, lease: timedelta) -> bool:
    """The heartbeat's write. False means the claim is lost: stop, send and write nothing."""
    result = await conn.execute(
        update(runs)
        .where(fenced(claim), runs.c.status.in_(sorted(WORKER_CLAIMABLE)))
        .values(lease_expires_at=func.now() + lease)
    )
    return result.rowcount == 1


def release_values(cooldown: timedelta | None) -> dict[str, Any]:
    values: dict[str, Any] = {
        "worker_id": None,
        "lease_expires_at": None,
        "claim_version": runs.c.claim_version + 1,
        "claimable_after": None,
    }
    if cooldown is not None:
        values["claimable_after"] = func.now() + cooldown
        values["release_streak"] = runs.c.release_streak + 1
    return values


async def release_claim(
    conn: AsyncConnection, claim: Claim, *, cooldown: timedelta | None = None
) -> Release:
    """Let go of a run (S9). The caller cancels its heartbeat first. With ``cooldown``, the
    run isn't claimable again until it passes and the release counts toward the streak (S12)."""
    row = (
        await conn.execute(
            update(runs)
            .where(fenced(claim))
            .values(**release_values(cooldown))
            .returning(runs.c.release_streak)
        )
    ).one_or_none()
    if row is None:
        return Release(released=False)
    return Release(released=True, release_streak=row.release_streak)


@asynccontextmanager
async def worker_transaction(
    engine: AsyncEngine, *, statement_timeout: timedelta, lock_timeout: timedelta
) -> AsyncIterator[AsyncConnection]:
    """A transaction whose statements and lock waits are bounded (ADR-014 S8)."""
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "SELECT set_config('statement_timeout', :statement, true), "
                "set_config('lock_timeout', :lock, true)"
            ),
            {"statement": _millis(statement_timeout), "lock": _millis(lock_timeout)},
        )
        yield conn


def _millis(duration: timedelta) -> str:
    return f"{max(1, round(duration.total_seconds() * 1000))}ms"
