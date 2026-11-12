"""Database side of the pre-send check (ADR-004 step 6, ADR-005, ADR-009, ADR-014).

Everything here runs inside the worker's transactions; nothing commits on its own. Lock order
(ADR-005/008/009): ``customers`` first, then ``runs``. Only database reads happen while the
customer row is locked; the lock is never held across HTTP.
"""

import json
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import func, insert, or_, select, update
from sqlalchemy.ext.asyncio import AsyncConnection

from agent.core.capacity import ApprovalSource, RefundRecord, used_by_customer, used_on_order
from agent.core.lifecycle import RefundOutcome, RunStatus
from agent.core.policy import (
    MissingInputError,
    OrderFacts,
    PolicyConfig,
    PolicyControls,
    PresendInputs,
    RefundProposal,
)
from agent.core.replies import ReplyTemplate
from agent.db.claims import Claim, fenced
from agent.db.schema import (
    customers,
    decisions,
    events,
    orders,
    policy_controls,
    run_order_lookups,
    runs,
)

# Kept as a module constant so ADR-005's negative control can run the check without the lock.
LOCK_CUSTOMER = True
CONTROLS_ROW_ID = 1
DECIDED_BY_POLICY = "policy"


@dataclass(frozen=True)
class Subject:
    """What a decision row and an escalation need, read without validating the proposal (the
    fail-closed path must still log a row for a malformed run)."""

    run_id: uuid.UUID
    conversation_id: uuid.UUID
    action_id: str | None
    review_hash: str | None


@dataclass(frozen=True)
class PresendRun:
    run_id: uuid.UUID
    conversation_id: uuid.UUID
    user_id: str
    proposal: RefundProposal
    approval_source: ApprovalSource | None
    review_hash: str | None

    @property
    def subject(self) -> Subject:
        return Subject(self.run_id, self.conversation_id, self.proposal.action_id, self.review_hash)


async def db_now(conn: AsyncConnection) -> datetime:
    """The transaction's clock (ADR-004: lease and age times use the database clock)."""
    return (await conn.execute(select(func.now()))).scalar_one()


async def load_approved_run(conn: AsyncConnection, claim: Claim) -> PresendRun | None:
    """The claimed run, if it's still ours and still APPROVED."""
    row = (
        await conn.execute(select(runs).where(fenced(claim), runs.c.status == RunStatus.APPROVED))
    ).one_or_none()
    if row is None:
        return None
    for column in ("action_id", "order_id", "amount_minor", "currency"):
        if getattr(row, column) is None:
            raise MissingInputError(f"approved run has no {column}")
    return PresendRun(
        run_id=row.id,
        conversation_id=row.conversation_id,
        user_id=row.user_id,
        proposal=RefundProposal(
            action_id=row.action_id,
            order_id=row.order_id,
            amount_minor=row.amount_minor,
            currency=row.currency,
        ),
        approval_source=row.approval_source,
        review_hash=row.review_hash,
    )


async def load_subject(conn: AsyncConnection, claim: Claim) -> Subject | None:
    row = (
        await conn.execute(
            select(runs.c.id, runs.c.conversation_id, runs.c.action_id, runs.c.review_hash).where(
                fenced(claim)
            )
        )
    ).one_or_none()
    if row is None:
        return None
    return Subject(row.id, row.conversation_id, row.action_id, row.review_hash)


async def lock_customer(conn: AsyncConnection, user_id: str) -> None:
    """ADR-005: serialize limit checks per customer. Waits are bounded by ``lock_timeout``."""
    if LOCK_CUSTOMER:
        await conn.execute(
            select(customers.c.id).where(customers.c.id == user_id).with_for_update()
        )


async def load_inputs(conn: AsyncConnection, run: PresendRun, now: datetime) -> PresendInputs:
    """Database reads only (ADR-009 Concurrency): the order, the run's lookups, the refund
    history the capacity rules count, and the controls."""
    proposal = run.proposal
    order_row = (
        await conn.execute(select(orders).where(orders.c.id == proposal.order_id))
    ).one_or_none()
    if order_row is None:
        raise MissingInputError(f"order {proposal.order_id} not found")
    looked_up = (
        await conn.execute(
            select(run_order_lookups.c.order_id).where(run_order_lookups.c.run_id == run.run_id)
        )
    ).scalars()
    history_rows = await conn.execute(
        select(runs).where(
            or_(runs.c.user_id == run.user_id, runs.c.order_id == proposal.order_id),
            runs.c.action_id.is_not(None),
            runs.c.amount_minor.is_not(None),
        )
    )
    controls_row = (
        await conn.execute(select(policy_controls).where(policy_controls.c.id == CONTROLS_ROW_ID))
    ).one_or_none()
    if controls_row is None:
        raise MissingInputError("policy_controls row is missing")
    return PresendInputs(
        proposal=proposal,
        session_user_id=run.user_id,
        order=OrderFacts(
            order_id=order_row.id,
            owner_user_id=order_row.owner_user_id,
            charged_minor=order_row.charged_minor,
            currency=order_row.currency,
            status=order_row.status,
            delivered_at=order_row.delivered_at,
            chargeback_open=order_row.chargeback_open,
        ),
        looked_up_order_ids=frozenset(looked_up),
        history=[
            RefundRecord(
                action_id=r.action_id,
                order_id=r.order_id,
                amount_minor=r.amount_minor,
                currency=r.currency,
                status=r.status,
                outcome=r.refund_outcome,
                first_send_started_at=r.first_send_started_at,
                approval_source=r.approval_source,
                approved_at=r.approved_at,
            )
            for r in history_rows
        ],
        approved_by_policy=run.approval_source is ApprovalSource.POLICY,
        controls=PolicyControls(
            breaker_tripped=controls_row.breaker_tripped_at is not None,
            sends_enabled=controls_row.sends_enabled,
        ),
        now=now,
    )


def snapshots(inputs: PresendInputs, config: PolicyConfig) -> tuple[dict[str, Any], dict[str, Any]]:
    """The decision row's config and input snapshots (ADR-009 Decision log): enough to replay
    the decision later."""
    proposal = inputs.proposal
    input_snapshot = {
        "proposal": asdict(proposal),
        "order": asdict(inputs.order),
        "looked_up_order_ids": sorted(inputs.looked_up_order_ids),
        "approved_by_policy": inputs.approved_by_policy,
        "controls": asdict(inputs.controls),
        "used_on_order_minor": used_on_order(
            inputs.history,
            order_id=proposal.order_id,
            exclude_action_id=proposal.action_id,
            include_pending=False,
        ),
        "used_by_customer_minor": used_by_customer(
            inputs.history,
            exclude_action_id=proposal.action_id,
            now=inputs.now,
            window=config.per_customer_window,
            include_pending=False,
        ),
        "now": inputs.now,
    }
    return jsonable(asdict(config)), jsonable(input_snapshot)


def jsonable(value: dict[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = json.loads(json.dumps(value, default=str))
    return result


async def insert_decision(
    conn: AsyncConnection,
    subject: Subject,
    *,
    outcome: str,
    rule_ids: tuple[str, ...],
    policy_version: str,
    config_snapshot: dict[str, Any],
    input_snapshot: dict[str, Any],
) -> None:
    await conn.execute(
        insert(decisions).values(
            run_id=subject.run_id,
            action_id=subject.action_id,
            stage="presend",
            outcome=outcome,
            rule_ids=list(rule_ids),
            policy_version=policy_version,
            config_snapshot=config_snapshot,
            input_snapshot=input_snapshot,
            review_hash=subject.review_hash,
            decided_by=DECIDED_BY_POLICY,
        )
    )


async def move_to_executing(conn: AsyncConnection, claim: Claim, window: timedelta) -> bool:
    """ADR-004 step 6 / ADR-008: the reservation. ``first_send_started_at`` and
    ``last_attempt_at`` get the same value; the window snapshot is the value just read
    (ADR-012 Part 3, ADR-013, ADR-014 S1). False means the claim is lost."""
    result = await conn.execute(
        update(runs)
        .where(fenced(claim), runs.c.status == RunStatus.APPROVED)
        .values(
            status=RunStatus.EXECUTING,
            refund_outcome=RefundOutcome.UNKNOWN,
            first_send_started_at=func.now(),
            last_attempt_at=func.now(),
            send_window_ms=window // timedelta(milliseconds=1),
            release_streak=0,
        )
    )
    return result.rowcount == 1


async def escalate_unsent(conn: AsyncConnection, claim: Claim, conversation_id: uuid.UUID) -> bool:
    """``APPROVED → ESCALATED`` with nothing sent (ADR-005/008/009, ADR-014 S12): the flag, a
    customer-facing event, and the claim cleared with the version bumped (S9). The event names
    only the template, never why (ADR-009 non-disclosure); the why is in the decision log or
    the alert."""
    result = await conn.execute(
        update(runs)
        .where(fenced(claim), runs.c.status == RunStatus.APPROVED)
        .values(
            status=RunStatus.ESCALATED,
            escalated_at=func.now(),
            worker_id=None,
            lease_expires_at=None,
            claim_version=runs.c.claim_version + 1,
            claimable_after=None,
            release_streak=0,
        )
    )
    if result.rowcount != 1:
        return False
    await conn.execute(
        insert(events).values(
            conversation_id=conversation_id,
            run_id=claim.run_id,
            kind="escalated",
            payload={"template": ReplyTemplate.NEEDS_PERSON.value},
        )
    )
    return True


async def count_failed_presend(conn: AsyncConnection, claim: Claim) -> int | None:
    """ADR-009 Fail closed: the separate, fenced transaction's counter. None means lost."""
    row = (
        await conn.execute(
            update(runs)
            .where(fenced(claim), runs.c.status == RunStatus.APPROVED)
            .values(presend_attempts=runs.c.presend_attempts + 1)
            .returning(runs.c.presend_attempts)
        )
    ).one_or_none()
    return None if row is None else int(row.presend_attempts)
