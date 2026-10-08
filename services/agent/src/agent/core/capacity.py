"""Refund capacity: how much of each limit is already used (ADR-004, ADR-005, ADR-008).

Rules:
- Only ``unknown`` and ``confirmed`` outcomes count; run status is never used.
- Every ``unknown`` refund counts regardless of age. The per-customer rolling window
  applies to ``confirmed`` refunds only, so uncertainty can never age out and free
  capacity.
- The action being evaluated is always excluded.
"""

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta

from agent.core.lifecycle import RefundOutcome

COUNTED_OUTCOMES = frozenset({RefundOutcome.UNKNOWN, RefundOutcome.CONFIRMED})


@dataclass(frozen=True, slots=True)
class RefundRecord:
    action_id: str
    order_id: str
    amount_minor: int
    currency: str
    outcome: RefundOutcome
    first_send_started_at: datetime | None


def used_on_order(
    records: Iterable[RefundRecord], *, order_id: str, currency: str, exclude_action_id: str
) -> int:
    """Amount already refunded (or possibly refunded) on one order, over its lifetime."""
    return sum(
        r.amount_minor
        for r in records
        if r.order_id == order_id
        and r.currency == currency
        and r.action_id != exclude_action_id
        and r.outcome in COUNTED_OUTCOMES
    )


def used_by_customer(
    records: Iterable[RefundRecord],
    *,
    currency: str,
    exclude_action_id: str,
    now: datetime,
    window: timedelta,
) -> int:
    """Amount counted against the customer's rolling-window limit."""
    window_start = now - window
    total = 0
    for r in records:
        if r.currency != currency or r.action_id == exclude_action_id:
            continue
        if r.outcome is RefundOutcome.UNKNOWN or (
            r.outcome is RefundOutcome.CONFIRMED
            and r.first_send_started_at is not None
            and r.first_send_started_at >= window_start
        ):
            total += r.amount_minor
    return total
