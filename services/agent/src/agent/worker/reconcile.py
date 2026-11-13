"""The reconcile loop (ADR-008 Part 2, ADR-010, ADR-016, ADR-017). It never sends.

Each pass, one run at a time:
1. **Stale-unknown alert** (ADR-004): every ``NEEDS_RECONCILIATION`` run unresolved past
   ``RECONCILER_STALE_ALERT_SECONDS`` alerts; the alerter limits it per run.
2. **A fresh ``GET /config``** (ADR-016 Part 2), not the gate's cache, which can be a refresh
   interval old. It feeds the gate; the loop claims only while the gate is open, with the budget
   just read in the quiet period.
3. **Look the refund up** by ``action_id`` under the worker's lease, heartbeat and fencing, and
   classify it against the stored payload (ADR-012).
4. **Found** → ``confirmed``, plus an alert for two or more (ADR-010). **Not found** →
   ``rejected``/``ESCALATED``, unless the kill switch is on: then no decision, a cooldown and no
   streak (Part 4). The switch is read in the transaction that decides.
5. **A failure** always releases with the cooldown (S5):
   - refund-api's (it didn't answer): the gate closes until a config read succeeds, one alert
     for all runs per interval (ADR-017 S1), 401/403 pages. It counts toward the run's streak
     only if refund-api answered another run's lookup since this run last failed (ADR-017).
   - the run's (a mismatched refund, no payload to look up, an unexpected error): an alert and
     the streak; at the limit a page with the ``run_id``, status untouched.
"""

import asyncio
import logging
import uuid
from contextlib import AbstractAsyncContextManager
from datetime import timedelta
from enum import StrEnum

from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncConnection

from agent.core.lifecycle import RunStatus
from agent.core.refund_outcome import (
    ConfigRead,
    ConfigReadKind,
    LookupFault,
    LookupKind,
    LookupOutcome,
    classify_lookup,
)
from agent.db import claims, execution
from agent.db import reconcile as db
from agent.db.claims import Claim
from agent.refund_client.models import RefundPayload
from agent.worker.context import WorkerContext
from agent.worker.gate import read_config
from agent.worker.lease import LostClaim, StepClock, StepOverrun, hold_claim
from agent.worker.releases import Released, release_with_cooldown

logger = logging.getLogger(__name__)

AUTH_STATUSES = (401, 403)


class Reconciled(StrEnum):
    CONFIRMED = "confirmed"
    REJECTED = "rejected"
    HELD = "held"  # the kill switch held a "not found": no decision
    PROVIDER_FAILED = "provider_failed"  # refund-api didn't answer
    RUN_FAILED = "run_failed"
    LOST = "lost"  # the claim is gone; nothing written


class LookupEvidence:
    """Whether a repeated refund-api-type failure is the run's own (ADR-017).

    Counts the lookups refund-api answered (any well-formed 200) and, per run, that count at
    the run's last refund-api-type failure. A repeat counts toward the streak only if refund-api
    has answered someone else since. Kept in process memory: a restart costs one extra cycle."""

    def __init__(self) -> None:
        self._answered = 0
        self._failed_at: dict[uuid.UUID, int] = {}

    def answered(self, run_id: uuid.UUID) -> None:
        self._answered += 1
        self._failed_at.pop(run_id, None)

    def provider_failure_counts(self, run_id: uuid.UUID) -> bool:
        before = self._failed_at.get(run_id)
        self._failed_at[run_id] = self._answered
        return before is not None and self._answered > before


async def reconcile_forever(ctx: WorkerContext, worker_id: str) -> None:
    """Until ``ctx.stopping`` is set. A pass in progress finishes; ``run_worker`` bounds it."""
    evidence = LookupEvidence()
    stop = asyncio.create_task(ctx.stopping.wait())
    try:
        while not ctx.stopping.is_set():
            try:
                step = await reconcile_once(ctx, worker_id, evidence)
            except Exception as exc:
                logger.exception("reconcile pass failed")
                ctx.alerter.alert("reconcile_pass_failed", error=type(exc).__name__)
                step = None
            if step is None:
                await asyncio.wait({stop}, timeout=ctx.timings.poll.total_seconds())
    finally:
        stop.cancel()
        await asyncio.gather(stop, return_exceptions=True)


async def reconcile_once(
    ctx: WorkerContext, worker_id: str, evidence: LookupEvidence
) -> Reconciled | None:
    """One pass. None means nothing was claimed."""
    await _alert_stale(ctx)
    read = await _fresh_read(ctx)
    ctx.gate.record(read)
    if read.config is None or not ctx.gate.is_open():
        return None
    claim = await _claim(ctx, worker_id, read.config.processing_budget)
    if claim is None:
        return None
    return await process_reconcile(ctx, claim, evidence)


async def process_reconcile(
    ctx: WorkerContext, claim: Claim, evidence: LookupEvidence
) -> Reconciled:
    clock = StepClock(ctx.timings.max_step)
    try:
        return await hold_claim(
            ctx.engine, claim, ctx.timings, clock, lambda: _reconcile(ctx, claim, evidence, clock)
        )
    except LostClaim:
        return Reconciled.LOST
    except Exception as exc:  # StepOverrun included: the run's failure, never an outcome
        kind = "reconcile_step_overrun" if isinstance(exc, StepOverrun) else "reconcile_error"
        logger.error("run %s: %s", claim.run_id, kind, exc_info=exc)
        try:
            return await _run_failed(ctx, claim, kind, type(exc).__name__)
        except Exception:
            # The lease expires normally; the run stays NEEDS_RECONCILIATION.
            logger.exception("run %s: release after failure failed", claim.run_id)
            ctx.alerter.alert("worker_release_failed", run_id=claim.run_id)
            return Reconciled.LOST


async def _reconcile(
    ctx: WorkerContext, claim: Claim, evidence: LookupEvidence, clock: StepClock
) -> Reconciled:
    clock.begin_step()
    async with _transaction(ctx) as conn:
        state = await db.lock_state(conn, claim)
    if state is None:
        return Reconciled.LOST
    payload = _payload(state.canonical_payload)
    if payload is None:
        return await _run_failed(ctx, claim, "reconcile_unreadable_run", "no canonical payload")

    clock.begin_step()
    result = await ctx.client.lookup(payload.order_id, payload.action_id)
    outcome = classify_lookup(result, payload.as_sent())
    if outcome.fault is LookupFault.PROVIDER:
        return await _provider_failed(ctx, claim, evidence, result.status, outcome.reason)
    evidence.answered(claim.run_id)
    if outcome.fault is LookupFault.RUN:
        return await _run_failed(ctx, claim, "reconcile_lookup_mismatch", outcome.reason)
    clock.begin_step()
    return await _decide(ctx, claim, payload, outcome)


async def _decide(
    ctx: WorkerContext, claim: Claim, payload: RefundPayload, outcome: LookupOutcome
) -> Reconciled:
    async with _transaction(ctx) as conn:
        state = await db.lock_state(conn, claim)
        if state is None:
            return Reconciled.LOST
        if outcome.kind is LookupKind.FOUND:
            moved = await execution.confirm(
                conn,
                claim,
                state.conversation_id,
                order_id=payload.order_id,
                amount_minor=payload.amount_minor,
                currency=payload.currency,
                from_status=RunStatus.NEEDS_RECONCILIATION,
            )
            step = Reconciled.CONFIRMED
        elif state.sends_enabled is not True:
            # Part 4: an empty answer during an incident may be refund-api's bug; only an
            # admin turning the switch off lets "not found" free capacity.
            moved = (
                await claims.release_claim(
                    conn, claim, cooldown=ctx.timings.claim_cooldown, count_streak=False
                )
            ).released
            step = Reconciled.HELD
        else:
            moved = await execution.reject_unconfirmed(conn, claim, state.conversation_id)
            step = Reconciled.REJECTED
    if not moved:
        return Reconciled.LOST
    if outcome.alert:  # two or more refunds for one action (ADR-010)
        ctx.alerter.alert(
            "duplicate_refunds", run_id=claim.run_id, refund_ids=list(outcome.refund_ids)
        )
    if step is Reconciled.HELD:
        ctx.alerter.alert("reconcile_held_by_kill_switch", held_run=str(claim.run_id))
    return step


async def _provider_failed(
    ctx: WorkerContext,
    claim: Claim,
    evidence: LookupEvidence,
    status: int | None,
    reason: str,
) -> Reconciled:
    ctx.gate.lookup_failed()
    if status in AUTH_STATUSES:
        ctx.alerter.page("refund_api_auth_failed", reason=reason)
    else:
        # ADR-017 S1: one key for every run, so an outage is one line per interval.
        ctx.alerter.alert("reconcile_lookup_failed", reason=reason, failed_run=str(claim.run_id))
    released = await release_with_cooldown(
        ctx,
        claim,
        reason="reconcile_lookup_failed",
        count_streak=evidence.provider_failure_counts(claim.run_id),
    )
    return Reconciled.LOST if released is Released.LOST else Reconciled.PROVIDER_FAILED


async def _run_failed(ctx: WorkerContext, claim: Claim, kind: str, reason: str) -> Reconciled:
    ctx.alerter.alert(kind, run_id=claim.run_id, reason=reason)
    released = await release_with_cooldown(ctx, claim, reason=kind)
    return Reconciled.LOST if released is Released.LOST else Reconciled.RUN_FAILED


async def _alert_stale(ctx: WorkerContext) -> None:
    """Never blocks reconciling: a failed scan alerts and the pass goes on."""
    try:
        async with _transaction(ctx) as conn:
            stale = await db.stale_unknown(conn, ctx.timings.stale_unknown_after)
    except Exception as exc:
        logger.exception("stale-unknown scan failed")
        ctx.alerter.alert("reconcile_stale_scan_failed", error=type(exc).__name__)
        return
    for run in stale:
        ctx.alerter.alert(
            "reconcile_stale_unknown",
            run_id=run.run_id,
            unresolved_seconds=round(run.unresolved.total_seconds()),
        )


async def _fresh_read(ctx: WorkerContext) -> ConfigRead:
    try:
        return await read_config(ctx.client, ctx.refund_settings)
    except Exception as exc:
        logger.exception("reconciler config read raised")
        return ConfigRead(ConfigReadKind.FAILED, f"config read raised {type(exc).__name__}")


async def _claim(ctx: WorkerContext, worker_id: str, budget_now: timedelta) -> Claim | None:
    async with _transaction(ctx) as conn:
        return await db.claim_for_reconcile(
            conn,
            worker_id,
            ctx.timings.lease,
            budget_now=budget_now,
            margin=ctx.timings.quiet_margin,
        )


def _payload(canonical: str | None) -> RefundPayload | None:
    try:
        return RefundPayload.model_validate_json(canonical or "")
    except ValidationError:
        return None


def _transaction(ctx: WorkerContext) -> AbstractAsyncContextManager[AsyncConnection]:
    return claims.worker_transaction(
        ctx.engine,
        statement_timeout=ctx.timings.statement_timeout,
        lock_timeout=ctx.timings.lock_timeout,
    )
