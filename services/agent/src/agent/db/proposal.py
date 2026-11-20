"""Database side of the proposal path (ADR-009 proposal phase, Concurrency, Decision log;
ADR-004 step 1; ADR-008 transitions out of ``AGENT_STEP``).

Nothing here commits on its own; the turn owns its transactions. Lock order (ADR-009):
``customers`` → ``conversations`` → ``runs`` → ``policy_controls``. Every read that feeds a
per-customer rule (R05, R06, R11, R12, R16, R17, R18) runs **inside** the customer-locked
transaction: read before the lock, two concurrent proposals would each miss the other. What runs
before the lock is only what needs no database: parsing, validation and the ``action_id``.
"""

import secrets
import uuid
from dataclasses import asdict
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import exists, func, insert, select, update
from sqlalchemy.ext.asyncio import AsyncConnection

from agent.core import escalations
from agent.core.capacity import (
    ApprovalSource,
    is_live_policy_approved,
    policy_approved_in_window,
    used_by_customer,
    used_on_order,
)
from agent.core.lifecycle import RunStatus, require_transition
from agent.core.policy import (
    PolicyConfig,
    ProposalInputs,
    RefundProposal,
    ReviewDecline,
    RuleId,
)
from agent.db.clock import stamped
from agent.db.presend import (
    CONTROLS_ROW_ID,
    DECIDED_BY_POLICY,
    db_now,
    jsonable,
    load_controls,
    load_history,
    load_lookups,
    load_order,
    record_of,
)
from agent.db.schema import decisions, events, policy_controls, runs
from agent.refund_client.payload import canonical_payload

PROPOSAL_STAGE = "proposal"
BREAKER_TRIP_STAGE = "breaker_trip"
DECLINE_STAGE = "reviewer_decline"
PENDING_REVIEW = "pending_review"
# R17 counts "proposals in the last 24 h": one ``proposal`` decision row per proposal.
PROPOSAL_DAY = timedelta(hours=24)
IDEMPOTENCY_KEY_BYTES = 32


def new_action_id() -> str:
    """ADR-009: assigned when a proposal is validated. Fits refund-api's id pattern."""
    return f"act_{uuid.uuid4().hex}"


def approval_values(proposal: RefundProposal) -> dict[str, Any]:
    """What approval writes to the run (ADR-004 step 1): the proposal, a fresh idempotency key
    and the canonical payload, in the approving transaction, before any call. The e2e helper
    ``make_approved_run`` uses this too, so a test-made run has the same shape as a real one."""
    return {
        "action_id": proposal.action_id,
        "order_id": proposal.order_id,
        "amount_minor": proposal.amount_minor,
        "currency": proposal.currency,
        "idempotency_key": secrets.token_urlsafe(IDEMPOTENCY_KEY_BYTES),
        "canonical_payload": canonical_payload(
            order_id=proposal.order_id,
            amount_minor=proposal.amount_minor,
            currency=proposal.currency,
            action_id=proposal.action_id,
        ),
    }


async def load_proposal_inputs(
    conn: AsyncConnection,
    *,
    run_id: uuid.UUID,
    user: str,
    proposal: RefundProposal,
    breaker_window: timedelta,
    at: datetime | None,
) -> ProposalInputs:
    """Database reads only, under the customer lock. Raises ``MissingInputError`` (fail closed).
    The policy's "now" is ``at`` when the clock gives one (ADR-026), else the database's."""
    now = at if at is not None else await db_now(conn)
    return ProposalInputs(
        proposal=proposal,
        session_user_id=user,
        order=await load_order(conn, proposal.order_id),
        looked_up_order_ids=await load_lookups(conn, run_id),
        history=await load_history(conn, user, proposal.order_id),
        declines=await _declines_on_order(conn, proposal.order_id),
        has_unresolved_escalation=await _has_unresolved_escalation(conn, user),
        customer_pending_reviews=await _count_reviews(conn, user),
        customer_proposals_last_24h=await _count_recent_proposals(conn, user, now),
        global_policy_approved_in_breaker_window_minor=await _global_policy_approved(
            conn, now, breaker_window
        ),
        global_pending_reviews=await _count_reviews(conn, None),
        controls=await load_controls(conn),
        now=now,
    )


async def _declines_on_order(conn: AsyncConnection, order_id: str) -> list[ReviewDecline]:
    """R18: reviewer declines of any action on this order (the decision log is the record)."""
    rows = await conn.execute(
        select(decisions.c.decided_at)
        .join(runs, runs.c.id == decisions.c.run_id)
        .where(decisions.c.stage == DECLINE_STAGE, runs.c.order_id == order_id)
    )
    return [ReviewDecline(order_id, declined_at) for declined_at in rows.scalars()]


async def _has_unresolved_escalation(conn: AsyncConnection, user: str) -> bool:
    """R18: a run that is ``ESCALATED`` or C01-flagged and not yet resolved. ``escalated_at``
    alone doesn't count: a reconciliation alert on a confirmed refund isn't an escalation."""
    query = select(
        exists().where(
            runs.c.user_id == user,
            (runs.c.status == RunStatus.ESCALATED) | runs.c.c01_flagged_at.is_not(None),
            runs.c.escalation_resolved_at.is_(None),
        )
    )
    return bool((await conn.execute(query)).scalar_one())


async def _count_reviews(conn: AsyncConnection, user: str | None) -> int:
    """R16 (one customer) and R19 (``user=None``: system-wide): runs waiting for a reviewer."""
    query = select(func.count()).where(runs.c.status == RunStatus.AWAIT_APPROVAL)
    if user is not None:
        query = query.where(runs.c.user_id == user)
    return int((await conn.execute(query)).scalar_one())


async def _count_recent_proposals(conn: AsyncConnection, user: str, now: datetime) -> int:
    query = (
        select(func.count())
        .select_from(decisions.join(runs, runs.c.id == decisions.c.run_id))
        .where(
            runs.c.user_id == user,
            decisions.c.stage == PROPOSAL_STAGE,
            decisions.c.decided_at > now - PROPOSAL_DAY,
        )
    )
    return int((await conn.execute(query)).scalar_one())


async def _global_policy_approved(
    conn: AsyncConnection, now: datetime, breaker_window: timedelta
) -> int:
    """R13: live policy-approved amount system-wide with ``approved_at`` in the breaker window.
    The rows are filtered by the core's own definition, so SQL and policy can't disagree."""
    rows = await conn.execute(
        select(runs).where(
            runs.c.approval_source == ApprovalSource.POLICY,
            runs.c.approved_at >= now - breaker_window,
            runs.c.action_id.is_not(None),
            runs.c.amount_minor.is_not(None),
        )
    )
    return sum(r.amount_minor for r in rows if is_live_policy_approved(record_of(r)))


def snapshots(
    inputs: ProposalInputs, config: PolicyConfig
) -> tuple[dict[str, Any], dict[str, Any]]:
    """The decision row's config and input snapshots (ADR-009): the order facts and the totals
    the rules saw, so the decision can be replayed."""
    proposal, now = inputs.proposal, inputs.now
    excluded = proposal.action_id
    input_snapshot = {
        "proposal": asdict(proposal),
        "order": asdict(inputs.order),
        "looked_up_order_ids": sorted(inputs.looked_up_order_ids),
        "used_on_order_minor": used_on_order(
            inputs.history,
            order_id=proposal.order_id,
            exclude_action_id=excluded,
            include_pending=True,
        ),
        "used_by_customer_minor": used_by_customer(
            inputs.history,
            exclude_action_id=excluded,
            now=now,
            window=config.per_customer_window,
            include_pending=True,
        ),
        "policy_approved_in_window_minor": policy_approved_in_window(
            inputs.history, exclude_action_id=excluded, now=now, window=config.per_customer_window
        ),
        "declines_on_order": len(inputs.declines),
        "has_unresolved_escalation": inputs.has_unresolved_escalation,
        "customer_pending_reviews": inputs.customer_pending_reviews,
        "customer_proposals_last_24h": inputs.customer_proposals_last_24h,
        "global_policy_approved_in_breaker_window_minor": (
            inputs.global_policy_approved_in_breaker_window_minor
        ),
        "global_pending_reviews": inputs.global_pending_reviews,
        "controls": asdict(inputs.controls),
        "now": now,
    }
    return jsonable(asdict(config)), jsonable(input_snapshot)


async def insert_decision(
    conn: AsyncConnection,
    *,
    run_id: uuid.UUID,
    action_id: str,
    stage: str,
    outcome: str,
    rule_ids: tuple[str, ...],
    policy_version: str,
    config_snapshot: dict[str, Any],
    input_snapshot: dict[str, Any],
    review_hash: str | None,
    at: datetime | None,
) -> None:
    await conn.execute(
        insert(decisions).values(
            run_id=run_id,
            action_id=action_id,
            stage=stage,
            outcome=outcome,
            rule_ids=list(rule_ids),
            policy_version=policy_version,
            config_snapshot=config_snapshot,
            input_snapshot=input_snapshot,
            review_hash=review_hash,
            decided_by=DECIDED_BY_POLICY,
            decided_at=stamped(at),
        )
    )


async def trip_breaker(conn: AsyncConnection, *, at: datetime | None) -> bool:
    """R13's trip signal (ADR-009): last in the lock order; concurrent trips are harmless.
    True only for the transaction that tripped it."""
    result = await conn.execute(
        update(policy_controls)
        .where(
            policy_controls.c.id == CONTROLS_ROW_ID, policy_controls.c.breaker_tripped_at.is_(None)
        )
        .values(breaker_tripped_at=stamped(at))
        .returning(policy_controls.c.id)
    )
    return result.one_or_none() is not None


async def _move(conn: AsyncConnection, run_id: uuid.UUID, to: RunStatus, **values: Any) -> None:
    require_transition(RunStatus.AGENT_STEP, to)
    result = await conn.execute(
        update(runs)
        .where(runs.c.id == run_id, runs.c.status == RunStatus.AGENT_STEP)
        .values(status=to, **values)
        .returning(runs.c.id)
    )
    if result.one_or_none() is None:
        raise RuntimeError(f"run {run_id} left AGENT_STEP while this turn held its conversation")


async def approve(
    conn: AsyncConnection,
    run_id: uuid.UUID,
    proposal: RefundProposal,
    review_hash: str,
    *,
    at: datetime | None,
) -> None:
    """``AGENT_STEP → APPROVED`` by policy: key and canonical payload with the approval."""
    await _move(
        conn,
        run_id,
        RunStatus.APPROVED,
        **approval_values(proposal),
        approval_source=ApprovalSource.POLICY,
        approved_at=stamped(at),
        review_hash=review_hash,
    )


async def await_review(
    conn: AsyncConnection,
    conversation_id: uuid.UUID,
    run_id: uuid.UUID,
    proposal: RefundProposal,
    review_hash: str,
    deadline: timedelta,
    *,
    at: datetime | None,
) -> None:
    """``AGENT_STEP → AWAIT_APPROVAL``: ``review_deadline`` and a ``pending_review`` event; no
    key yet (it's created when a reviewer approves, ADR-004 step 1)."""
    await _move(
        conn,
        run_id,
        RunStatus.AWAIT_APPROVAL,
        action_id=proposal.action_id,
        order_id=proposal.order_id,
        amount_minor=proposal.amount_minor,
        currency=proposal.currency,
        review_hash=review_hash,
        review_deadline=stamped(at) + deadline,
    )
    await conn.execute(
        insert(events).values(
            conversation_id=conversation_id,
            run_id=run_id,
            kind=PENDING_REVIEW,
            payload={"action_id": proposal.action_id},
            created_at=stamped(at),
        )
    )


async def escalate_proposal(
    conn: AsyncConnection, run_id: uuid.UUID, action_id: str, *, reason: str, at: datetime | None
) -> None:
    """``AGENT_STEP → ESCALATED``, nothing sent. Only the ``action_id`` goes on the run: the
    proposal itself is in the decision row's snapshot, and an amount R01 refused (≤ 0) would
    break the run's own CHECK and with it the escalation."""
    await _move(
        conn,
        run_id,
        RunStatus.ESCALATED,
        action_id=action_id,
        escalated_at=stamped(at),
        escalation_reason=escalations.checked(reason),
    )


def breaker_trip_snapshot(inputs: ProposalInputs, config: PolicyConfig) -> dict[str, Any]:
    return jsonable(
        {
            "rule_id": RuleId.R13_CIRCUIT_BREAKER.value,
            "global_policy_approved_in_breaker_window_minor": (
                inputs.global_policy_approved_in_breaker_window_minor
            ),
            "amount_minor": inputs.proposal.amount_minor,
            "breaker_threshold_minor": config.breaker_threshold_minor,
            "now": inputs.now,
        }
    )
