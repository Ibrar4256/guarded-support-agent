"""Claiming runs: lease plus fencing (ADR-004 step 7, ADR-008, ADR-009 R14, ADR-014).

- **Claim** (ADR-014 Part 2): one ``UPDATE`` whose subselect picks a claimable run with
  ``FOR UPDATE SKIP LOCKED``. The outer ``WHERE`` repeats every condition, so the
  compare-and-set is the guarantee and ``SKIP LOCKED`` only stops workers queueing on one row.
  ``EXECUTING`` runs first (they're aging against their window), then the oldest approval.
  A half-open write breaker's probe claim orders by what a failed probe would cost instead
  (ADR-018 S3): a run already doomed by its window, then an approval, then fewest attempts.
- **Fencing**: every write a worker makes is conditional on the ``claim_version`` it claimed.
- **Release** (S9, S12): always bumps ``claim_version``, so the releasing worker's version is
  dead the moment it lets go. A cooldown release also sets ``claimable_after``; whether it
  counts toward ``release_streak`` is the caller's choice (ADR-016 S5).
- Lease times use the database clock (``now()``), never the worker's.
"""

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from sqlalchemy import (
    ColumnElement,
    FromClause,
    Interval,
    and_,
    case,
    func,
    literal_column,
    or_,
    select,
    text,
    update,
)
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from agent.core.capacity import ApprovalSource
from agent.core.lifecycle import RECONCILER_CLAIMABLE, WORKER_CLAIMABLE, RunStatus
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
class Held:
    """A claimed run as a release sees it, row-locked: its streak and current status (an
    APPROVED claim may have moved to EXECUTING since)."""

    release_streak: int
    status: RunStatus
    conversation_id: uuid.UUID


@dataclass(frozen=True)
class Release:
    released: bool
    release_streak: int | None = None


@dataclass(frozen=True)
class ProbeOrder:
    """ADR-018 S3: a probe claim's inputs. A run is doomed when its window (on its snapshot)
    leaves less than ``next_open`` before its last possible send."""

    client_timeout: timedelta
    next_open: timedelta


MILLISECOND = literal_column("interval '1 millisecond'", Interval)


def probe_order(r: FromClause, probe: ProbeOrder) -> list[ColumnElement[Any]]:
    last_send_by = (
        r.c.first_send_started_at + r.c.send_window_ms * MILLISECOND - probe.client_timeout
    )
    doomed = and_(r.c.status == RunStatus.EXECUTING, last_send_by < func.now() + probe.next_open)
    rank = case((doomed, 0), (r.c.status == RunStatus.APPROVED, 1), else_=2)
    return [rank, r.c.send_attempts, r.c.approved_at, r.c.id]


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


def lease_free(r: FromClause) -> ColumnElement[bool]:
    return or_(r.c.worker_id.is_(None), r.c.lease_expires_at < func.now())


def cooldown_passed(r: FromClause) -> ColumnElement[bool]:
    return or_(r.c.claimable_after.is_(None), r.c.claimable_after <= func.now())


def claim_conditions(r: FromClause, controls: Controls) -> dict[str, ColumnElement[bool]]:
    """What makes a run claimable by a worker. Named, so each one can be tested on its own."""
    return {
        "status": r.c.status.in_(sorted(WORKER_CLAIMABLE)),
        "lease_free": lease_free(r),
        "cooldown_passed": cooldown_passed(r),
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


async def claim_run(
    conn: AsyncConnection, worker_id: str, lease: timedelta, *, probe: ProbeOrder | None = None
) -> Claim | None:
    candidate = runs.alias("candidate")
    order: list[ColumnElement[Any]] = (
        [
            (candidate.c.status == RunStatus.EXECUTING).desc(),
            candidate.c.approved_at,
            candidate.c.id,
        ]
        if probe is None
        else probe_order(candidate, probe)
    )
    pick = (
        select(candidate.c.id)
        .where(*claim_conditions(candidate, current_controls()).values())
        .order_by(*order)
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


def holdable(claim: Claim) -> frozenset[RunStatus]:
    """The statuses a claim may be held in: the reconciler's own, or the worker's (an APPROVED
    claim moves to EXECUTING while held)."""
    return RECONCILER_CLAIMABLE if claim.status in RECONCILER_CLAIMABLE else WORKER_CLAIMABLE


async def renew_lease(conn: AsyncConnection, claim: Claim, lease: timedelta) -> bool:
    """The heartbeat's write. False means the claim is lost: stop, send and write nothing.
    ``greatest``: never shortens a lease the attempt transaction extended to cover an attempt
    on the wire (ADR-014 Part 3)."""
    result = await conn.execute(
        update(runs)
        .where(fenced(claim), runs.c.status.in_(sorted(holdable(claim))))
        .values(lease_expires_at=func.greatest(runs.c.lease_expires_at, func.now() + lease))
    )
    return result.rowcount == 1


def release_values(
    cooldown: timedelta | None, *, reset_streak: bool = False, count_streak: bool = True
) -> dict[str, Any]:
    values: dict[str, Any] = {
        "worker_id": None,
        "lease_expires_at": None,
        "claim_version": runs.c.claim_version + 1,
        "claimable_after": None,
    }
    if cooldown is not None:
        values["claimable_after"] = func.now() + cooldown
        if count_streak:
            values["release_streak"] = runs.c.release_streak + 1
    elif reset_streak:
        values["release_streak"] = 0
    return values


async def release_claim(
    conn: AsyncConnection,
    claim: Claim,
    *,
    cooldown: timedelta | None = None,
    reset_streak: bool = False,
    count_streak: bool = True,
) -> Release:
    """Let go of a run (S9). The caller cancels its heartbeat first. With ``cooldown``, the
    run isn't claimable again until it passes, and the release counts toward the streak (S12)
    unless ``count_streak`` is off: the cooldown and the streak are separate choices
    (ADR-016 S5). ``reset_streak`` is for releases that follow progress (a pre-send HOLD)."""
    row = (
        await conn.execute(
            update(runs)
            .where(fenced(claim))
            .values(
                **release_values(cooldown, reset_streak=reset_streak, count_streak=count_streak)
            )
            .returning(runs.c.release_streak)
        )
    ).one_or_none()
    if row is None:
        return Release(released=False)
    return Release(released=True, release_streak=row.release_streak)


async def lock_for_release(conn: AsyncConnection, claim: Claim) -> Held | None:
    """None means the claim is already gone."""
    row = (
        await conn.execute(
            select(runs.c.release_streak, runs.c.status, runs.c.conversation_id)
            .where(fenced(claim))
            .with_for_update()
        )
    ).one_or_none()
    if row is None:
        return None
    return Held(row.release_streak, row.status, row.conversation_id)


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
