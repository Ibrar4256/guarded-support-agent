"""An EXECUTING run's send loop (ADR-004, ADR-008, ADR-012 Part 3, ADR-013, ADR-014 Part 3,
Part 4). The policy check never runs here: once a key may have been sent, the run is
committed to finishing that action (ADR-004 step 6).

Each round, in this order:
1. **Read** ``GET /config`` (the first round reuses pre-send's read) and feed the gate.
2. **Start the attempt budget** on the monotonic clock, before the transaction.
3. **One transaction**, the run row locked, the claim ours and the lease live: the attempt
   limit and the age check (``send_allowed`` with the snapshot and the window just read), then
   the attempt record (count + 1, ``last_attempt_at = now()``, a lease that outlives the
   attempt) or the move to ``NEEDS_RECONCILIATION``.
4. **Send** with what's left of the budget; nothing left, nothing sent (counted anyway).
5. **Classify** (ADR-012) and move, or back off and go round again.
A failed read sends nothing and counts nothing; it ends through the run's own snapshot, a
bounded number of failed reads, or a successful read (Part 4).
"""

import time
import uuid
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from enum import StrEnum

from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncConnection

from agent.core.refund_outcome import (
    ConfigReadKind,
    HttpResult,
    ProviderConfig,
    SendKind,
    classify_send,
    send_allowed,
)
from agent.db import claims
from agent.db import execution as db
from agent.db.claims import Claim
from agent.refund_client.models import RefundPayload
from agent.worker.context import WorkerContext
from agent.worker.gate import read_config
from agent.worker.lease import StepClock


class ExecutingStep(StrEnum):
    COMPLETED = "completed"  # confirmed
    ESCALATED = "escalated"  # rejected by a business error: no money moved
    RECONCILING = "reconciling"  # outcome unknown; the reconciler owns it now
    RELEASED = "released"  # claim given back, still EXECUTING (config unreadable)
    LOST = "lost"  # another worker owns it; nothing written


@dataclass(frozen=True)
class ExecutingResult:
    step: ExecutingStep
    reason: str = ""


@dataclass(frozen=True)
class _Attempt:
    number: int
    payload: RefundPayload
    key: str
    conversation_id: uuid.UUID


def monotonic() -> float:
    """The attempt budget's clock (a seam for tests)."""
    return time.monotonic()


async def handle_executing(
    ctx: WorkerContext,
    claim: Claim,
    clock: StepClock,
    first_read: ProviderConfig | None = None,
) -> ExecutingResult:
    policy = ctx.timings.policy
    provider = first_read
    failed_reads = 0
    while True:
        if ctx.stopping.is_set():
            # S7: between attempts; the last one's outcome is already recorded or retryable.
            return await _release(ctx, claim, "worker stopping")
        clock.begin_step()
        if provider is None:
            read = await read_config(ctx.client, ctx.refund_settings)
            ctx.gate.record(read)
            if read.kind is ConfigReadKind.AUTH_FAILED:
                return await _release(ctx, claim, "config read unauthorized")
            if read.kind is not ConfigReadKind.OK or read.config is None:
                failed_reads += 1
                stopped = await _after_failed_read(ctx, claim, failed_reads)
                if stopped is not None:
                    return stopped
                clock.begin_step()
                await ctx.sleep(
                    policy.wait_before_retry(failed_reads, ctx.jitter()).total_seconds()
                )
                continue
            provider = read.config
        failed_reads = 0

        deadline = monotonic() + policy.client_timeout.total_seconds()
        attempt = await _record_attempt(ctx, claim, provider)
        provider = None  # every later send reads the config again (ADR-013)
        if isinstance(attempt, ExecutingResult):
            return attempt

        clock.begin_step()
        await ctx.hooks.before_refund_call(claim.run_id)
        remaining = deadline - monotonic()
        if remaining > 0:
            result = await ctx.client.send(attempt.payload, attempt.key, timeout=remaining)
        else:
            result = HttpResult(status=None)  # budget spent before sending: counted, unsent
        await ctx.hooks.after_refund_response(claim.run_id)

        outcome = classify_send(result, attempt.payload.as_sent())
        if outcome.kind is SendKind.CONFIRMED:
            return await _finish_confirmed(ctx, claim, attempt)
        if outcome.kind is SendKind.REJECTED:
            return await _finish_rejected(ctx, claim, attempt, outcome.reason)
        if outcome.kind is SendKind.RECONCILE:
            return await _reconcile(ctx, claim, "refund_outcome_unknown", outcome.reason)
        if attempt.number >= policy.attempts:
            return await _reconcile(ctx, claim, "retries_exhausted", outcome.reason)
        clock.begin_step()
        wait = policy.wait_before_retry(attempt.number, ctx.jitter(), outcome.retry_after)
        await ctx.sleep(wait.total_seconds())


async def _record_attempt(
    ctx: WorkerContext, claim: Claim, provider: ProviderConfig
) -> _Attempt | ExecutingResult:
    """ADR-014 Part 3: the age check and the attempt record in one transaction."""
    policy = ctx.timings.policy
    async with _transaction(ctx) as conn:
        state = await db.lock_attempt_state(conn, claim)
        if state is None:
            return ExecutingResult(ExecutingStep.LOST, "claim or lease gone")
        if state.send_attempts >= policy.attempts:
            return await _reconcile_in(ctx, conn, claim, "retries_exhausted", "attempt limit")
        if not send_allowed(
            state.elapsed, policy.client_timeout, state.snapshot, provider.max_retry_window
        ):
            return await _reconcile_in(
                ctx, conn, claim, "send_window_passed", "age check failed before sending"
            )
        try:
            payload = RefundPayload.model_validate_json(state.canonical_payload or "")
        except ValidationError:
            payload = None
        if payload is None or not state.idempotency_key:
            return await _reconcile_in(
                ctx, conn, claim, "unsendable_run", "no saved key or canonical payload"
            )
        number = await db.record_attempt(conn, claim, policy.client_timeout)
        return _Attempt(number, payload, state.idempotency_key, state.conversation_id)


async def _after_failed_read(
    ctx: WorkerContext, claim: Claim, failed_reads: int
) -> ExecutingResult | None:
    """ADR-014 Part 4. None means "back off and read again"."""
    policy = ctx.timings.policy
    async with _transaction(ctx) as conn:
        age = await db.age(conn, claim)
        if age is None:
            return ExecutingResult(ExecutingStep.LOST, "claim gone")
        # min(snapshot, any later read) <= snapshot: if the snapshot rules a send out, nothing
        # read later can allow one.
        if not send_allowed(age.elapsed, policy.client_timeout, age.snapshot, age.snapshot):
            return await _reconcile_in(
                ctx, conn, claim, "send_window_passed", "config unreadable past the window"
            )
    if failed_reads >= ctx.timings.max_failed_reads:
        return await _release(ctx, claim, f"{failed_reads} failed config reads")
    return None


async def _finish_confirmed(ctx: WorkerContext, claim: Claim, attempt: _Attempt) -> ExecutingResult:
    async with _transaction(ctx) as conn:
        moved = await db.confirm(
            conn,
            claim,
            attempt.conversation_id,
            order_id=attempt.payload.order_id,
            amount_minor=attempt.payload.amount_minor,
            currency=attempt.payload.currency,
        )
    return ExecutingResult(ExecutingStep.COMPLETED if moved else ExecutingStep.LOST)


async def _finish_rejected(
    ctx: WorkerContext, claim: Claim, attempt: _Attempt, reason: str
) -> ExecutingResult:
    async with _transaction(ctx) as conn:
        moved = await db.reject(conn, claim, attempt.conversation_id)
    return ExecutingResult(ExecutingStep.ESCALATED if moved else ExecutingStep.LOST, reason)


async def _reconcile(ctx: WorkerContext, claim: Claim, kind: str, reason: str) -> ExecutingResult:
    async with _transaction(ctx) as conn:
        return await _reconcile_in(ctx, conn, claim, kind, reason)


async def _reconcile_in(
    ctx: WorkerContext, conn: AsyncConnection, claim: Claim, kind: str, reason: str
) -> ExecutingResult:
    if not await db.to_reconciliation(conn, claim):
        return ExecutingResult(ExecutingStep.LOST, reason)
    ctx.alerter.alert(kind, run_id=claim.run_id, reason=reason)
    return ExecutingResult(ExecutingStep.RECONCILING, reason)


async def _release(ctx: WorkerContext, claim: Claim, reason: str) -> ExecutingResult:
    async with _transaction(ctx) as conn:
        released = await claims.release_claim(conn, claim)
    return ExecutingResult(
        ExecutingStep.RELEASED if released.released else ExecutingStep.LOST, reason
    )


def _transaction(ctx: WorkerContext) -> AbstractAsyncContextManager[AsyncConnection]:
    return claims.worker_transaction(
        ctx.engine,
        statement_timeout=ctx.timings.statement_timeout,
        lock_timeout=ctx.timings.lock_timeout,
    )
