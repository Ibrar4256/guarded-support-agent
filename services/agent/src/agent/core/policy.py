"""Refund policy check (ADR-001 POLICY_CHECK, ADR-004 step 6, ADR-005).

Pure function: every input, including the current time and the limits, is a parameter.
The same limit checks run at proposal time and once more before the first send; only
the proposal-time check assigns a risk tier (an approved action is never sent back to
review).
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum

from agent.core.capacity import RefundRecord, used_by_customer, used_on_order


class PolicyDecision(StrEnum):
    IN_POLICY = "in_policy"
    HIGH_RISK = "high_risk"
    OUT_OF_POLICY = "out_of_policy"


@dataclass(frozen=True, slots=True)
class RefundPolicy:
    """Policy configuration. Values come from settings, never hardcoded."""

    auto_approve_max_minor: int
    per_customer_limit_minor: int
    per_customer_window: timedelta


@dataclass(frozen=True, slots=True)
class RefundProposal:
    action_id: str
    order_id: str
    amount_minor: int
    currency: str


@dataclass(frozen=True, slots=True)
class OrderFacts:
    """Order data loaded by code from the database; never taken from model output."""

    order_id: str
    owner_user_id: str
    charged_minor: int
    currency: str


@dataclass(frozen=True, slots=True)
class PolicyResult:
    decision: PolicyDecision
    reasons: tuple[str, ...]


def check_limits(
    proposal: RefundProposal,
    *,
    session_user_id: str,
    order: OrderFacts,
    history: Sequence[RefundRecord],
    policy: RefundPolicy,
    now: datetime,
) -> tuple[str, ...]:
    """Return the reasons the proposal is out of policy (empty means within limits)."""
    reasons: list[str] = []
    if proposal.amount_minor <= 0:
        reasons.append("amount must be positive")
    if proposal.order_id != order.order_id:
        reasons.append("proposal does not match the loaded order")
    if order.owner_user_id != session_user_id:
        reasons.append("order does not belong to the session user")
    if proposal.currency != order.currency:
        reasons.append("currency does not match the order")
    if reasons:
        return tuple(reasons)

    order_used = used_on_order(
        history,
        order_id=order.order_id,
        currency=order.currency,
        exclude_action_id=proposal.action_id,
    )
    if order_used + proposal.amount_minor > order.charged_minor:
        reasons.append("refunds would exceed the amount charged for the order")

    customer_used = used_by_customer(
        history,
        currency=proposal.currency,
        exclude_action_id=proposal.action_id,
        now=now,
        window=policy.per_customer_window,
    )
    if customer_used + proposal.amount_minor > policy.per_customer_limit_minor:
        reasons.append("refunds would exceed the customer's rolling limit")
    return tuple(reasons)


def check_proposal(
    proposal: RefundProposal,
    *,
    session_user_id: str,
    order: OrderFacts,
    history: Sequence[RefundRecord],
    policy: RefundPolicy,
    now: datetime,
) -> PolicyResult:
    """Proposal-time POLICY_CHECK: limits first, then the risk tier."""
    reasons = check_limits(
        proposal,
        session_user_id=session_user_id,
        order=order,
        history=history,
        policy=policy,
        now=now,
    )
    if reasons:
        return PolicyResult(PolicyDecision.OUT_OF_POLICY, reasons)
    if proposal.amount_minor > policy.auto_approve_max_minor:
        return PolicyResult(PolicyDecision.HIGH_RISK, ("amount above auto-approve maximum",))
    return PolicyResult(PolicyDecision.IN_POLICY, ())
