"""Shared builders for policy tests. Defaults describe one clean, auto-approvable refund."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta

from agent.core.capacity import ApprovalSource, RefundRecord
from agent.core.lifecycle import RefundOutcome, RunStatus
from agent.core.policy import (
    FulfillmentStatus,
    OrderFacts,
    PolicyConfig,
    PolicyControls,
    PresendInputs,
    ProposalInputs,
    RefundProposal,
)

NOW = datetime(2026, 10, 8, 12, 0, tzinfo=UTC)
USER = "user_alice"
ORDER_ID = "ord_1"

CONFIG = PolicyConfig(
    currency="USD",
    auto_approve_max_minor=5_000,
    auto_approve_budget_minor=10_000,
    per_customer_limit_minor=20_000,
    per_customer_window=timedelta(days=30),
    refund_window=timedelta(days=30),
    breaker_threshold_minor=100_000,
    pending_review_cap=3,
    daily_proposal_cap=5,
    global_review_queue_cap=50,
    decline_cooldown=timedelta(days=7),
)

ORDER = OrderFacts(
    order_id=ORDER_ID,
    owner_user_id=USER,
    charged_minor=8_000,
    currency="USD",
    status=FulfillmentStatus.DELIVERED,
    delivered_at=NOW - timedelta(days=3),
    chargeback_open=False,
)

CONTROLS = PolicyControls(breaker_tripped=False, sends_enabled=True)


def proposal(amount: int = 2_000, action_id: str = "act_new") -> RefundProposal:
    return RefundProposal(
        action_id=action_id, order_id=ORDER_ID, amount_minor=amount, currency="USD"
    )


def record(
    amount: int,
    *,
    action_id: str,
    status: RunStatus,
    outcome: RefundOutcome = RefundOutcome.NONE,
    order_id: str = ORDER_ID,
    sent_at: datetime | None = None,
    source: ApprovalSource | None = ApprovalSource.POLICY,
    approved_at: datetime | None = NOW,
) -> RefundRecord:
    if outcome is not RefundOutcome.NONE and sent_at is None:
        sent_at = NOW
    return RefundRecord(
        action_id=action_id,
        order_id=order_id,
        amount_minor=amount,
        currency="USD",
        status=status,
        outcome=outcome,
        first_send_started_at=sent_at,
        approval_source=source,
        approved_at=approved_at,
    )


def proposal_inputs(**overrides: object) -> ProposalInputs:
    base = ProposalInputs(
        proposal=proposal(),
        session_user_id=USER,
        order=ORDER,
        looked_up_order_ids=frozenset({ORDER_ID}),
        history=(),
        declines=(),
        has_unresolved_escalation=False,
        customer_pending_reviews=0,
        customer_proposals_last_24h=0,
        global_policy_approved_in_breaker_window_minor=0,
        global_pending_reviews=0,
        controls=CONTROLS,
        now=NOW,
    )
    return replace(base, **overrides)  # type: ignore[arg-type]


def presend_inputs(**overrides: object) -> PresendInputs:
    base = PresendInputs(
        proposal=proposal(),
        session_user_id=USER,
        order=ORDER,
        looked_up_order_ids=frozenset({ORDER_ID}),
        history=(),
        approved_by_policy=True,
        controls=CONTROLS,
        now=NOW,
    )
    return replace(base, **overrides)  # type: ignore[arg-type]
