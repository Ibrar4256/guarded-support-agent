from datetime import UTC, datetime, timedelta

from agent.core.capacity import RefundRecord, used_by_customer, used_on_order
from agent.core.lifecycle import RefundOutcome
from agent.core.policy import (
    OrderFacts,
    PolicyDecision,
    RefundPolicy,
    RefundProposal,
    check_limits,
    check_proposal,
)

NOW = datetime(2026, 10, 8, 12, 0, tzinfo=UTC)
WINDOW = timedelta(days=30)
POLICY = RefundPolicy(
    auto_approve_max_minor=5_000, per_customer_limit_minor=10_000, per_customer_window=WINDOW
)
USER = "user_alice"
ORDER = OrderFacts(order_id="ord_1", owner_user_id=USER, charged_minor=8_000, currency="USD")


def proposal(amount: int, action_id: str = "act_new", order_id: str = "ord_1") -> RefundProposal:
    return RefundProposal(
        action_id=action_id, order_id=order_id, amount_minor=amount, currency="USD"
    )


def record(
    amount: int,
    outcome: RefundOutcome,
    *,
    action_id: str,
    order_id: str = "ord_1",
    sent_at: datetime | None = NOW,
) -> RefundRecord:
    return RefundRecord(action_id, order_id, amount, "USD", outcome, sent_at)


def check(p: RefundProposal, history: list[RefundRecord] | None = None) -> PolicyDecision:
    return check_proposal(
        p, session_user_id=USER, order=ORDER, history=history or [], policy=POLICY, now=NOW
    ).decision


# --- capacity rules ---------------------------------------------------------------


def test_only_unknown_and_confirmed_count() -> None:
    history = [
        record(1_000, RefundOutcome.UNKNOWN, action_id="a"),
        record(2_000, RefundOutcome.CONFIRMED, action_id="b"),
        record(4_000, RefundOutcome.REJECTED, action_id="c"),
        record(8_000, RefundOutcome.NONE, action_id="d", sent_at=None),
    ]
    assert used_on_order(history, order_id="ord_1", currency="USD", exclude_action_id="x") == 3_000


def test_action_being_evaluated_is_excluded() -> None:
    history = [record(4_000, RefundOutcome.UNKNOWN, action_id="act_new")]
    assert (
        used_on_order(history, order_id="ord_1", currency="USD", exclude_action_id="act_new") == 0
    )


def test_unknown_counts_even_outside_the_window() -> None:
    old = NOW - WINDOW - timedelta(days=1)
    history = [
        record(3_000, RefundOutcome.UNKNOWN, action_id="a", sent_at=old),
        record(2_000, RefundOutcome.CONFIRMED, action_id="b", sent_at=old),
    ]
    used = used_by_customer(history, currency="USD", exclude_action_id="x", now=NOW, window=WINDOW)
    assert used == 3_000


# --- decisions ----------------------------------------------------------------------


def test_small_refund_is_in_policy() -> None:
    assert check(proposal(2_000)) is PolicyDecision.IN_POLICY


def test_refund_above_auto_approve_max_needs_review() -> None:
    assert check(proposal(6_000)) is PolicyDecision.HIGH_RISK


def test_refund_exceeding_amount_charged_is_out_of_policy() -> None:
    history = [record(5_000, RefundOutcome.CONFIRMED, action_id="a")]
    assert check(proposal(4_000), history) is PolicyDecision.OUT_OF_POLICY


def test_unknown_refund_blocks_a_second_one_on_the_order() -> None:
    history = [record(5_000, RefundOutcome.UNKNOWN, action_id="a")]
    assert check(proposal(4_000), history) is PolicyDecision.OUT_OF_POLICY


def test_rejected_refund_frees_capacity() -> None:
    history = [record(5_000, RefundOutcome.REJECTED, action_id="a")]
    assert check(proposal(4_000), history) is PolicyDecision.IN_POLICY


def test_customer_rolling_limit_is_enforced_across_orders() -> None:
    history = [record(9_000, RefundOutcome.CONFIRMED, action_id="a", order_id="ord_other")]
    assert check(proposal(2_000), history) is PolicyDecision.OUT_OF_POLICY


def test_order_of_another_user_is_out_of_policy() -> None:
    result = check_proposal(
        proposal(1_000),
        session_user_id="user_mallory",
        order=ORDER,
        history=[],
        policy=POLICY,
        now=NOW,
    )
    assert result.decision is PolicyDecision.OUT_OF_POLICY
    assert "order does not belong to the session user" in result.reasons


def test_non_positive_amount_is_out_of_policy() -> None:
    assert check(proposal(0)) is PolicyDecision.OUT_OF_POLICY


def test_resumed_refund_is_not_blocked_by_its_own_reservation() -> None:
    """ADR-004 step 6: a $40 refund on a $50 limit must not count itself as $80."""
    tight_order = OrderFacts("ord_1", USER, charged_minor=5_000, currency="USD")
    history = [record(4_000, RefundOutcome.UNKNOWN, action_id="act_new")]
    reasons = check_limits(
        proposal(4_000),
        session_user_id=USER,
        order=tight_order,
        history=history,
        policy=POLICY,
        now=NOW,
    )
    assert reasons == ()
