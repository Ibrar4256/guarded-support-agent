"""ADR-009's proposal path, after the loop handed over one valid ``propose_refund`` (ADR-022 S1).

One transaction, customer-locked then fenced on the turn (lock order ``customers`` →
``conversations`` → ``runs`` → ``policy_controls``): read the inputs, evaluate, write the decision
row, move the run, append the customer's template reply and release the lease, all together.
The model is never called again.
- ``AUTO`` → ``APPROVED`` with the idempotency key, canonical payload and review hash
  (``processing``).
- ``REVIEW`` → ``AWAIT_APPROVAL`` with ``review_deadline`` and a ``pending_review`` event
  (``awaiting_review``).
- ``OUT_OF_POLICY`` → ``ESCALATED`` (``needs_person``).
- A lock wait (``55P03``) is retried up to ``TURN_PROPOSAL_LOCK_ATTEMPTS`` times, then
  ``try_again`` with cause ``lock``; the run stays in ``AGENT_STEP``. Not a policy failure.
- ``FAIL_CLOSED``, a missing input or any other database error: that transaction rolls back and a
  separate one, fenced on the turn, escalates with a ``FAIL_CLOSED`` decision row. If that fails
  too: an alert, and the run stays in ``AGENT_STEP`` with nothing written.
A turn that lost its lease writes nothing (``LostTurn``).
"""

import logging
from dataclasses import asdict, dataclass
from enum import StrEnum

from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine

from agent.alerts import Alerter
from agent.core import escalations
from agent.core.policy import (
    POLICY_VERSION,
    MissingInputError,
    Outcome,
    PolicyConfig,
    RefundProposal,
    RuleId,
    evaluate_proposal,
)
from agent.core.replies import ReplyTemplate
from agent.core.review_hash import review_hash
from agent.db import proposal as db
from agent.db import turns
from agent.db.claims import worker_transaction
from agent.db.clock import Clock
from agent.db.presend import jsonable, lock_customer
from agent.tools.contract import LostTurn
from agent.turn.finish import TryAgainCause, reply_and_wait
from agent.turn.settings import TurnSettings
from agent.turn.start import TurnIds
from agent.turn.tools import ProposeRefundArgs
from agent.worker.presend import is_lock_timeout

logger = logging.getLogger(__name__)

BREAKER_TRIPPED_ALERT = "policy_breaker_tripped"
FAIL_CLOSED_ALERT = "proposal_fail_closed_escalated"
NOT_RECORDED_ALERT = "proposal_failure_not_recorded"


class ProposalStep(StrEnum):
    APPROVED = "approved"
    AWAITING_REVIEW = "awaiting_review"
    ESCALATED = "escalated"
    TRY_AGAIN = "try_again"  # lock timeouts; the run stays in AGENT_STEP


TEMPLATES = {
    Outcome.AUTO: (ProposalStep.APPROVED, ReplyTemplate.PROCESSING),
    Outcome.REVIEW: (ProposalStep.AWAITING_REVIEW, ReplyTemplate.AWAITING_REVIEW),
    Outcome.OUT_OF_POLICY: (ProposalStep.ESCALATED, ReplyTemplate.NEEDS_PERSON),
}


@dataclass(frozen=True)
class ProposalContext:
    engine: AsyncEngine
    settings: TurnSettings
    policy: PolicyConfig
    alerter: Alerter
    ids: TurnIds
    user: str
    clock: Clock


class _FailClosed(Exception):
    def __init__(self, reason: str, rule_ids: tuple[str, ...] = ()) -> None:
        super().__init__(reason)
        self.reason = reason
        self.rule_ids = rule_ids


async def run_proposal(ctx: ProposalContext, args: ProposeRefundArgs) -> ProposalStep:
    proposal = RefundProposal(
        action_id=db.new_action_id(),
        order_id=args.order_id,
        amount_minor=args.amount_minor,
        currency=args.currency,
    )
    for attempt in range(1, ctx.settings.proposal_lock_attempts + 1):
        try:
            return await _evaluate_and_move(ctx, proposal)
        except _FailClosed as failed:
            return await _fail_closed(ctx, proposal, failed)
        except DBAPIError as exc:
            if not is_lock_timeout(exc):
                sqlstate = getattr(exc.orig, "sqlstate", None)
                return await _fail_closed(ctx, proposal, _FailClosed(f"database error {sqlstate}"))
            logger.info("run %s: proposal lock wait %d timed out", ctx.ids.run_id, attempt)
    await reply_and_wait(
        ctx.engine,
        ctx.settings,
        ctx.ids,
        ReplyTemplate.TRY_AGAIN,
        TryAgainCause.LOCK,
        clock=ctx.clock,
    )
    return ProposalStep.TRY_AGAIN


async def _evaluate_and_move(ctx: ProposalContext, proposal: RefundProposal) -> ProposalStep:
    ids = ctx.ids
    async with worker_transaction(
        ctx.engine,
        statement_timeout=ctx.settings.statement_timeout,
        lock_timeout=ctx.settings.lock_timeout,
    ) as conn:
        await lock_customer(conn, ctx.user)
        if not await turns.hold_turn(conn, ids.conversation_id, ids.turn_id):
            raise LostTurn(f"turn {ids.turn_id} lost conversation {ids.conversation_id}")
        at = ctx.clock.stamp()
        try:
            inputs = await db.load_proposal_inputs(
                conn,
                run_id=ids.run_id,
                user=ctx.user,
                proposal=proposal,
                breaker_window=ctx.policy.breaker_window,
                at=at,
            )
        except MissingInputError as exc:
            raise _FailClosed(f"missing input: {exc}") from exc
        result = evaluate_proposal(inputs, ctx.policy)
        if result.outcome is Outcome.FAIL_CLOSED:
            raise _FailClosed("a rule could not decide", result.rule_ids)
        bound = review_hash(proposal, result.rule_ids, result.policy_version)
        config_snapshot, input_snapshot = db.snapshots(inputs, ctx.policy)
        await db.insert_decision(
            conn,
            run_id=ids.run_id,
            action_id=proposal.action_id,
            stage=db.PROPOSAL_STAGE,
            outcome=result.outcome.name,
            rule_ids=result.rule_ids,
            policy_version=result.policy_version,
            config_snapshot=config_snapshot,
            input_snapshot=input_snapshot,
            review_hash=bound,
            at=at,
        )
        if result.outcome is Outcome.AUTO:
            await db.approve(conn, ids.run_id, proposal, bound, at=at)
        elif result.outcome is Outcome.REVIEW:
            await db.await_review(
                conn,
                ids.conversation_id,
                ids.run_id,
                proposal,
                bound,
                ctx.settings.review_deadline,
                at=at,
            )
        else:
            await db.escalate_proposal(
                conn,
                ids.run_id,
                proposal.action_id,
                reason=escalations.policy(result.rule_ids),
                at=at,
            )
        tripped = result.trip_breaker and await db.trip_breaker(conn, at=at)
        if tripped:
            await db.insert_decision(
                conn,
                run_id=ids.run_id,
                action_id=proposal.action_id,
                stage=db.BREAKER_TRIP_STAGE,
                outcome="TRIPPED",
                rule_ids=(RuleId.R13_CIRCUIT_BREAKER.value,),
                policy_version=result.policy_version,
                config_snapshot=config_snapshot,
                input_snapshot=db.breaker_trip_snapshot(inputs, ctx.policy),
                review_hash=None,
                at=at,
            )
        step, template = TEMPLATES[result.outcome]
        await turns.append_event(
            conn, ids.conversation_id, ids.run_id, turns.REPLY, {"template": template.value}, at=at
        )
        await turns.release_lease(conn, ids.conversation_id, ids.turn_id)
    logger.info(
        "run %s: proposal %s %s (%s)",
        ids.run_id,
        proposal.action_id,
        result.outcome.name,
        ", ".join(result.rule_ids) or "no rule fired",
    )
    if tripped:
        ctx.alerter.alert(BREAKER_TRIPPED_ALERT, run_id=ids.run_id)
    return step


async def _fail_closed(
    ctx: ProposalContext, proposal: RefundProposal, failed: _FailClosed
) -> ProposalStep:
    """ADR-009 Fail closed at proposal time: the failing transaction has rolled back; this one,
    fenced on the turn, escalates the run and writes the ``FAIL_CLOSED`` row."""
    ids = ctx.ids
    try:
        async with worker_transaction(
            ctx.engine,
            statement_timeout=ctx.settings.statement_timeout,
            lock_timeout=ctx.settings.lock_timeout,
        ) as conn:
            if not await turns.hold_turn(conn, ids.conversation_id, ids.turn_id):
                raise LostTurn(f"turn {ids.turn_id} lost conversation {ids.conversation_id}")
            at = ctx.clock.stamp()
            await db.escalate_proposal(
                conn,
                ids.run_id,
                proposal.action_id,
                reason=escalations.fail_closed(failed.rule_ids),
                at=at,
            )
            await db.insert_decision(
                conn,
                run_id=ids.run_id,
                action_id=proposal.action_id,
                stage=db.PROPOSAL_STAGE,
                outcome=Outcome.FAIL_CLOSED.name,
                rule_ids=failed.rule_ids,
                policy_version=POLICY_VERSION,
                config_snapshot=jsonable(asdict(ctx.policy)),
                input_snapshot=jsonable({"reason": failed.reason, "proposal": asdict(proposal)}),
                review_hash=None,
                at=at,
            )
            await turns.append_event(
                conn,
                ids.conversation_id,
                ids.run_id,
                turns.REPLY,
                {"template": ReplyTemplate.NEEDS_PERSON.value},
                at=at,
            )
            await turns.release_lease(conn, ids.conversation_id, ids.turn_id)
    except LostTurn:
        raise
    except Exception:
        # ADR-009: the run stays in AGENT_STEP with nothing sent; the lease expires normally.
        ctx.alerter.alert(NOT_RECORDED_ALERT, run_id=ids.run_id, reason=failed.reason)
        raise
    ctx.alerter.alert(FAIL_CLOSED_ALERT, run_id=ids.run_id, reason=failed.reason)
    return ProposalStep.ESCALATED
