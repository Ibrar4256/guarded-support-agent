"""Refund totals used by the policy (ADR-004, ADR-005, ADR-009).

Two different metrics:
- **Capacity** (ADR-004): ``unknown`` + ``confirmed`` refunds. Authoritative at pre-send.
  Every ``unknown`` refund counts regardless of age; the per-customer rolling window
  applies to ``confirmed`` refunds only.
- **Pending exposure** (ADR-009): other actions that are ``AWAIT_APPROVAL``, or
  ``APPROVED`` with outcome ``none``. Added on top of capacity at proposal time only, so a
  second proposal sees the first before anything is sent.

The action being evaluated is always excluded.
"""

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum

from agent.core.lifecycle import RefundOutcome, RunStatus

COUNTED_OUTCOMES = frozenset({RefundOutcome.UNKNOWN, RefundOutcome.CONFIRMED})


class ApprovalSource(StrEnum):
    POLICY = "policy"
    REVIEWER = "reviewer"


@dataclass(frozen=True, slots=True)
class RefundRecord:
    """One refund action from the database, as the policy sees it."""

    action_id: str
    order_id: str
    amount_minor: int
    currency: str
    status: RunStatus
    outcome: RefundOutcome
    first_send_started_at: datetime | None
    approval_source: ApprovalSource | None
    approved_at: datetime | None


def is_pending_exposure(record: RefundRecord) -> bool:
    return record.status is RunStatus.AWAIT_APPROVAL or (
        record.status is RunStatus.APPROVED and record.outcome is RefundOutcome.NONE
    )


def is_live_policy_approved(record: RefundRecord) -> bool:
    """ADR-009 'live policy-approved action' (R11, R13)."""
    if record.approval_source is not ApprovalSource.POLICY or record.approved_at is None:
        return False
    approved_unsent = record.status is RunStatus.APPROVED and record.outcome is RefundOutcome.NONE
    return approved_unsent or record.outcome in COUNTED_OUTCOMES


def _others(records: Iterable[RefundRecord], exclude_action_id: str) -> list[RefundRecord]:
    return [r for r in records if r.action_id != exclude_action_id]


def used_on_order(
    records: Iterable[RefundRecord],
    *,
    order_id: str,
    exclude_action_id: str,
    include_pending: bool,
) -> int:
    """Refunded (or possibly refunded) on one order over its lifetime."""
    return sum(
        r.amount_minor
        for r in _others(records, exclude_action_id)
        if r.order_id == order_id
        and (r.outcome in COUNTED_OUTCOMES or (include_pending and is_pending_exposure(r)))
    )


def used_by_customer(
    records: Iterable[RefundRecord],
    *,
    exclude_action_id: str,
    now: datetime,
    window: timedelta,
    include_pending: bool,
) -> int:
    """Amount counted against the customer's rolling-window limit."""
    window_start = now - window
    total = 0
    for r in _others(records, exclude_action_id):
        if (
            r.outcome is RefundOutcome.UNKNOWN
            or (
                r.outcome is RefundOutcome.CONFIRMED
                and r.first_send_started_at is not None
                and r.first_send_started_at >= window_start
            )
            or (include_pending and is_pending_exposure(r))
        ):
            total += r.amount_minor
    return total


def policy_approved_in_window(
    records: Iterable[RefundRecord], *, exclude_action_id: str, now: datetime, window: timedelta
) -> int:
    """Live policy-approved amount with ``approved_at`` inside the window (R11)."""
    window_start = now - window
    return sum(
        r.amount_minor
        for r in _others(records, exclude_action_id)
        if is_live_policy_approved(r)
        and r.approved_at is not None
        and r.approved_at >= window_start
    )


def has_other_action_on_order(
    records: Iterable[RefundRecord], *, order_id: str, exclude_action_id: str
) -> bool:
    """Another action on this order that is pending or may have moved money (R12)."""
    return any(
        r.order_id == order_id and (is_pending_exposure(r) or r.outcome in COUNTED_OUTCOMES)
        for r in _others(records, exclude_action_id)
    )
