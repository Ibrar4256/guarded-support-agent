"""An APPROVED run, from its config read to the move to EXECUTING (ADR-004 step 6, ADR-005,
ADR-009, ADR-013, ADR-014 "One claimed run" step 2, S4, S5, S12).

1. Read ``GET /config`` and feed the gate. Failed or 401: release (the gate has closed,
   alerted or paged). A window below the worst case: release, nothing sent (S4).
2. One customer-locked transaction: the policy check, **exactly once per run**, with its
   decision row in the same transaction.
   - ``PROCEED``: ``EXECUTING``, ``first_send_started_at = last_attempt_at = now()``,
     ``refund_outcome = unknown``, the window snapshot from step 1's read. The first attempt
     reuses that read (ADR-014 Part 3).
   - ``OUT_OF_POLICY``: ``ESCALATED`` with a customer event; nothing sent.
   - ``HOLD``: release; the run stays ``APPROVED`` (ADR-009 R14).
   - ``FAIL_CLOSED`` or a database error other than a lock timeout: that transaction rolls
     back; a separate fenced one counts it, and at the maximum escalates (ADR-009, S5).
   - A lock timeout (``55P03``): roll back, keep the claim, back off; after N in a row,
     alert and release with a cooldown (ADR-005, S12).
   Any 0-row fenced write means the claim is lost: everything in that transaction rolls back.
"""

import logging
from dataclasses import asdict, dataclass
from enum import StrEnum

from sqlalchemy.exc import DBAPIError

from agent.core.policy import (
    POLICY_VERSION,
    MissingInputError,
    PresendOutcome,
    evaluate_presend,
)
from agent.core.refund_outcome import ConfigReadKind, ProviderConfig
from agent.db import claims
from agent.db import presend as db
from agent.db.claims import Claim
from agent.worker.context import WorkerContext
from agent.worker.gate import read_config
from agent.worker.lease import StepClock

logger = logging.getLogger(__name__)

LOCK_NOT_AVAILABLE = "55P03"


class ApprovedStep(StrEnum):
    EXECUTING = "executing"  # moved; the first attempt reuses ``config``
    ESCALATED = "escalated"  # nothing sent
    RELEASED = "released"  # claim given back; the run is still APPROVED
    COOLDOWN = "cooldown"  # released, not claimable until the cooldown passes (S12)
    LOST = "lost"  # another worker owns it; nothing was written


@dataclass(frozen=True)
class ApprovedResult:
    step: ApprovedStep
    config: ProviderConfig | None = None


class _FailClosed(Exception):
    def __init__(self, reason: str, rule_ids: tuple[str, ...] = ()) -> None:
        super().__init__(reason)
        self.reason = reason
        self.rule_ids = rule_ids


class _Lost(Exception):
    """A fenced write affected 0 rows: roll the whole transaction back."""


def is_lock_timeout(exc: DBAPIError) -> bool:
    """ADR-005: only ``55P03`` is a lock wait; every other database error is an error."""
    return getattr(exc.orig, "sqlstate", None) == LOCK_NOT_AVAILABLE


async def handle_approved(ctx: WorkerContext, claim: Claim, clock: StepClock) -> ApprovedResult:
    clock.begin_step()
    read = await read_config(ctx.client, ctx.refund_settings)
    ctx.gate.record(read)
    if read.kind is not ConfigReadKind.OK or read.config is None:
        return await _release(ctx, claim)
    if read.config.max_retry_window < ctx.refund_settings.policy.worst_case_window():
        return await _release(ctx, claim)

    lock_timeouts = tries = 0
    while True:
        clock.begin_step()
        tries += 1
        try:
            return await _check_and_move(ctx, claim, read.config)
        except _Lost:
            return ApprovedResult(ApprovedStep.LOST)
        except _FailClosed as failed:
            lock_timeouts = 0
            stopped = await _record_fail_closed(ctx, claim, failed)
        except DBAPIError as exc:
            if not is_lock_timeout(exc):
                lock_timeouts = 0
                sqlstate = getattr(exc.orig, "sqlstate", None)
                stopped = await _record_fail_closed(
                    ctx, claim, _FailClosed(f"database error {sqlstate}")
                )
            else:
                lock_timeouts += 1
                stopped = None
                if lock_timeouts >= ctx.timings.lock_retry_max:
                    ctx.alerter.alert(
                        "presend_lock_timeouts", run_id=claim.run_id, in_a_row=lock_timeouts
                    )
                    return await _cooldown_release(ctx, claim)
        if stopped is not None:
            return stopped
        clock.begin_step()
        wait = ctx.timings.policy.wait_before_retry(tries, ctx.jitter())
        await ctx.sleep(wait.total_seconds())


async def _check_and_move(
    ctx: WorkerContext, claim: Claim, provider: ProviderConfig
) -> ApprovedResult:
    """The one customer-locked transaction (ADR-005)."""
    async with claims.worker_transaction(
        ctx.engine,
        statement_timeout=ctx.timings.statement_timeout,
        lock_timeout=ctx.timings.lock_timeout,
    ) as conn:
        try:
            run = await db.load_approved_run(conn, claim)
            if run is None:
                raise _Lost
            await db.lock_customer(conn, run.user_id)
            inputs = await db.load_inputs(conn, run, await db.db_now(conn))
        except MissingInputError as exc:
            raise _FailClosed(f"missing input: {exc}") from exc
        await ctx.hooks.after_capacity_read(run.run_id)
        result = evaluate_presend(inputs, ctx.policy)
        if result.outcome is PresendOutcome.FAIL_CLOSED:
            raise _FailClosed("a rule could not decide", result.rule_ids)
        config_snapshot, input_snapshot = db.snapshots(inputs, ctx.policy)
        await db.insert_decision(
            conn,
            run.subject,
            outcome=result.outcome.name,
            rule_ids=result.rule_ids,
            policy_version=result.policy_version,
            config_snapshot=config_snapshot,
            input_snapshot=input_snapshot,
        )
        if result.outcome is PresendOutcome.PROCEED:
            if not await db.move_to_executing(conn, claim, provider.max_retry_window):
                raise _Lost
            return ApprovedResult(ApprovedStep.EXECUTING, provider)
        if result.outcome is PresendOutcome.OUT_OF_POLICY:
            if not await db.escalate_unsent(conn, claim, run.conversation_id):
                raise _Lost
            return ApprovedResult(ApprovedStep.ESCALATED)
        # HOLD: progress, so the streak resets; the run waits APPROVED for the hold to lift.
        if not (await claims.release_claim(conn, claim, reset_streak=True)).released:
            raise _Lost
        return ApprovedResult(ApprovedStep.RELEASED)


async def _record_fail_closed(
    ctx: WorkerContext, claim: Claim, failed: _FailClosed
) -> ApprovedResult | None:
    """ADR-009 Fail closed: a separate transaction fenced by ``claim_version`` counts the
    failure and logs it; at the maximum the run escalates. None means "back off and retry"."""
    try:
        async with claims.worker_transaction(
            ctx.engine,
            statement_timeout=ctx.timings.statement_timeout,
            lock_timeout=ctx.timings.lock_timeout,
        ) as conn:
            attempts = await db.count_failed_presend(conn, claim)
            subject = await db.load_subject(conn, claim)
            if attempts is None or subject is None:
                return ApprovedResult(ApprovedStep.LOST)
            await db.insert_decision(
                conn,
                subject,
                outcome=PresendOutcome.FAIL_CLOSED.name,
                rule_ids=failed.rule_ids,
                policy_version=POLICY_VERSION,
                config_snapshot=db.jsonable(asdict(ctx.policy)),
                input_snapshot={"reason": failed.reason, "presend_attempts": attempts},
            )
            if attempts < ctx.timings.presend_max_attempts:
                return None
            if not await db.escalate_unsent(conn, claim, subject.conversation_id):
                raise _Lost
    except _Lost:
        return ApprovedResult(ApprovedStep.LOST)
    except Exception:
        # ADR-009: if this transaction fails too, alert; the run stays APPROVED, nothing sent,
        # and the lease expires normally.
        ctx.alerter.alert("presend_failure_not_recorded", run_id=claim.run_id)
        raise
    ctx.alerter.alert("presend_fail_closed_escalated", run_id=claim.run_id, reason=failed.reason)
    return ApprovedResult(ApprovedStep.ESCALATED)


async def _cooldown_release(ctx: WorkerContext, claim: Claim) -> ApprovedResult:
    """S12: release with a cooldown; at the streak limit an APPROVED run escalates instead
    (safe: nothing was sent)."""
    async with claims.worker_transaction(
        ctx.engine,
        statement_timeout=ctx.timings.statement_timeout,
        lock_timeout=ctx.timings.lock_timeout,
    ) as conn:
        streak = await db.release_streak(conn, claim)
        subject = await db.load_subject(conn, claim)
        if streak is None or subject is None:
            return ApprovedResult(ApprovedStep.LOST)
        if streak + 1 >= ctx.timings.release_streak_limit:
            if not await db.escalate_unsent(conn, claim, subject.conversation_id):
                return ApprovedResult(ApprovedStep.LOST)
            ctx.alerter.alert("release_streak_limit", run_id=claim.run_id, status="APPROVED")
            return ApprovedResult(ApprovedStep.ESCALATED)
        released = await claims.release_claim(conn, claim, cooldown=ctx.timings.claim_cooldown)
        return ApprovedResult(ApprovedStep.COOLDOWN if released.released else ApprovedStep.LOST)


async def _release(ctx: WorkerContext, claim: Claim) -> ApprovedResult:
    async with claims.worker_transaction(
        ctx.engine,
        statement_timeout=ctx.timings.statement_timeout,
        lock_timeout=ctx.timings.lock_timeout,
    ) as conn:
        released = await claims.release_claim(conn, claim)
    return ApprovedResult(ApprovedStep.RELEASED if released.released else ApprovedStep.LOST)
