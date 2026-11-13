"""The worker's run loop (ADR-004 step 7, ADR-012 Part 4, ADR-014 "One claimed run", S3, S6,
S7).

- **Startup:** one config read. A window below the worst case is fatal; anything else just
  leaves the gate closed until a refresh opens it.
- **Claim only while the gate is open** (ADR-012 Part 4), one run at a time (S3), polling when
  idle. The config refresh runs as its own task.
- **One claimed run:** the heartbeat holds it while the run is dispatched by status. An
  APPROVED run goes through pre-send and, if it moves, straight into the send loop with the
  same config read. An EXECUTING run goes to the send loop only: **the policy check never runs
  on a run that may already have been sent** (ADR-004 step 6).
- **A lost claim** writes nothing. **A stuck step or an unexpected error** (S6) alerts and
  releases with a cooldown, never classified as an outcome; the streak decides escalation
  (APPROVED) or a page (EXECUTING).
- **Stopping** (S7): handlers release at their next step boundary; an attempt on the wire gets
  ``shutdown_grace`` to finish and be recorded, then the run is cancelled and its lease left
  to expire.
- **The reconcile loop** (ADR-016 Part 1) runs beside the send loop as its own task, over the
  same engine, client, gate and alerter; a reconcile pass in progress gets the same grace.
"""

import asyncio
import logging
from enum import StrEnum

from agent.core.lifecycle import RunStatus
from agent.db import claims
from agent.db.claims import Claim
from agent.worker.context import WorkerContext
from agent.worker.execute import handle_executing
from agent.worker.gate import refresh_forever, startup_check
from agent.worker.lease import LostClaim, StepClock, StepOverrun, hold_claim
from agent.worker.presend import ApprovedStep, handle_approved
from agent.worker.reconcile import reconcile_forever
from agent.worker.releases import release_with_cooldown

logger = logging.getLogger(__name__)


class Ended(StrEnum):
    COMPLETED = "completed"
    ESCALATED = "escalated"
    RECONCILING = "reconciling"
    RELEASED = "released"
    COOLDOWN = "cooldown"
    LOST = "lost"


async def process_claim(ctx: WorkerContext, claim: Claim) -> Ended:
    """Run one claimed run to wherever this worker can take it."""
    clock = StepClock(ctx.timings.max_step)

    async def work() -> Ended:
        first_read = None
        if claim.status is RunStatus.APPROVED:
            approved = await handle_approved(ctx, claim, clock)
            if approved.step is not ApprovedStep.EXECUTING:
                return Ended(approved.step.value)
            first_read = approved.config
        executed = await handle_executing(ctx, claim, clock, first_read)
        return Ended(executed.step.value)

    try:
        return await hold_claim(ctx.engine, claim, ctx.timings, clock, work)
    except LostClaim:
        return Ended.LOST
    except Exception as exc:  # StepOverrun included
        return await _after_failure(ctx, claim, exc)


async def _after_failure(ctx: WorkerContext, claim: Claim, exc: BaseException) -> Ended:
    """S6: alert, release with a cooldown; never classify. The heartbeat is already cancelled
    (``hold_claim`` cancels it on the way out), as S9 requires before a release."""
    kind = "worker_step_overrun" if isinstance(exc, StepOverrun) else "worker_unexpected_error"
    logger.error("run %s: %s", claim.run_id, kind, exc_info=exc)
    ctx.alerter.alert(kind, run_id=claim.run_id, error=type(exc).__name__)
    try:
        released = await release_with_cooldown(ctx, claim, reason=kind)
    except Exception:
        # The run stays where it was and its lease expires normally (ADR-009 Fail closed).
        logger.exception("run %s: release after failure failed", claim.run_id)
        ctx.alerter.alert("worker_release_failed", run_id=claim.run_id)
        return Ended.LOST
    return Ended(released.value)


async def claim_next(ctx: WorkerContext, worker_id: str) -> Claim | None:
    async with claims.worker_transaction(
        ctx.engine,
        statement_timeout=ctx.timings.statement_timeout,
        lock_timeout=ctx.timings.lock_timeout,
    ) as conn:
        return await claims.claim_run(conn, worker_id, ctx.timings.lease)


async def run_worker(ctx: WorkerContext, worker_id: str) -> None:
    """Until ``ctx.stopping`` is set. Raises ``WorkerConfigError`` at startup if refund-api's
    window could never fit a retry schedule."""
    await startup_check(ctx.client, ctx.refund_settings, ctx.gate)
    refresher = asyncio.create_task(
        refresh_forever(ctx.client, ctx.refund_settings, ctx.gate, ctx.timings.config_refresh)
    )
    reconciler = asyncio.create_task(reconcile_forever(ctx, worker_id))
    logger.info(
        "worker %s running: send loop and reconcile loop; claim gate %s",
        worker_id,
        ctx.gate.closed_reason() or "open",
    )
    stop = asyncio.create_task(ctx.stopping.wait())
    try:
        while not ctx.stopping.is_set():
            claim = await claim_next(ctx, worker_id) if ctx.gate.is_open() else None
            if claim is None:
                await asyncio.wait({stop}, timeout=ctx.timings.poll.total_seconds())
                continue
            run = asyncio.create_task(process_claim(ctx, claim))
            await asyncio.wait({run, stop}, return_when=asyncio.FIRST_COMPLETED)
            if not run.done():
                # Stopping: the handlers release at their next step boundary; give an attempt
                # already on the wire the grace period to finish and be recorded.
                await asyncio.wait({run}, timeout=ctx.timings.shutdown_grace.total_seconds())
                if not run.done():
                    logger.warning("run %s: still busy after the grace period", claim.run_id)
                    run.cancel()
            await asyncio.gather(run, return_exceptions=True)
    finally:
        if ctx.stopping.is_set() and not reconciler.done():
            # A lookup on the wire finishes and its outcome is recorded, within the grace.
            await asyncio.wait({reconciler}, timeout=ctx.timings.shutdown_grace.total_seconds())
        for task in (refresher, stop, reconciler):
            task.cancel()
        await asyncio.gather(refresher, stop, reconciler, return_exceptions=True)
