"""ADR-009 policy: every rule firing and not firing, plus the review scenarios."""

from dataclasses import replace
from datetime import timedelta

import pytest
from policy_fixtures import (
    CONFIG,
    CONTROLS,
    NOW,
    ORDER,
    ORDER_ID,
    presend_inputs,
    proposal,
    proposal_inputs,
    record,
)

from agent.core.capacity import ApprovalSource
from agent.core.lifecycle import RefundOutcome, RunStatus
from agent.core.policy import (
    ConfigError,
    FulfillmentStatus,
    Outcome,
    PolicyControls,
    PresendOutcome,
    ReviewDecline,
    RuleId,
    evaluate_presend,
    evaluate_proposal,
)


def fired_ids(result: object) -> set[str]:
    return set(result.rule_ids)  # type: ignore[attr-defined]


def test_clean_refund_auto_approves() -> None:
    result = evaluate_proposal(proposal_inputs(), CONFIG)
    assert result.outcome is Outcome.AUTO
    assert result.fired == ()
    assert result.trip_breaker is False


# --- each rule fires on its own -----------------------------------------------------

SINGLE_RULE_CASES = [
    pytest.param(
        {"proposal": proposal(amount=0)},
        RuleId.R01_AMOUNT_POSITIVE,
        Outcome.OUT_OF_POLICY,
        id="R01",
    ),
    pytest.param(
        {"looked_up_order_ids": frozenset()},
        RuleId.R02_ORDER_PROVENANCE,
        Outcome.OUT_OF_POLICY,
        id="R02-not-looked-up",
    ),
    pytest.param(
        {"session_user_id": "user_mallory"},
        RuleId.R03_ORDER_OWNERSHIP,
        Outcome.OUT_OF_POLICY,
        id="R03",
    ),
    pytest.param(
        {"proposal": proposal(amount=6_000)}, RuleId.R07_AUTO_APPROVE_MAX, Outcome.REVIEW, id="R07"
    ),
    pytest.param(
        {"order": replace(ORDER, delivered_at=NOW - timedelta(days=31))},
        RuleId.R08_REFUND_WINDOW,
        Outcome.OUT_OF_POLICY,
        id="R08",
    ),
    pytest.param(
        {"order": replace(ORDER, chargeback_open=True)},
        RuleId.R10_NO_CHARGEBACK,
        Outcome.OUT_OF_POLICY,
        id="R10",
    ),
    pytest.param(
        {"controls": PolicyControls(breaker_tripped=True, sends_enabled=True)},
        RuleId.R13_CIRCUIT_BREAKER,
        Outcome.REVIEW,
        id="R13-tripped",
    ),
    pytest.param(
        {"customer_proposals_last_24h": 5},
        RuleId.R17_DAILY_PROPOSAL_CAP,
        Outcome.OUT_OF_POLICY,
        id="R17",
    ),
    pytest.param(
        {"has_unresolved_escalation": True},
        RuleId.R18_STICKY_ESCALATION,
        Outcome.REVIEW,
        id="R18-escalation",
    ),
]


@pytest.mark.parametrize(("overrides", "rule", "expected"), SINGLE_RULE_CASES)
def test_rule_fires_alone(overrides: dict[str, object], rule: RuleId, expected: Outcome) -> None:
    result = evaluate_proposal(proposal_inputs(**overrides), CONFIG)
    assert fired_ids(result) == {rule}
    assert result.outcome is expected


def test_r04_and_r15_fire_for_another_currency() -> None:
    eur_order = replace(ORDER, currency="EUR")
    result = evaluate_proposal(proposal_inputs(order=eur_order), CONFIG)
    assert RuleId.R04_CURRENCY_MATCH in fired_ids(result)
    eur = replace(proposal(), currency="EUR")
    result = evaluate_proposal(proposal_inputs(proposal=eur, order=eur_order), CONFIG)
    assert fired_ids(result) == {RuleId.R15_SUPPORTED_CURRENCY}
    assert result.outcome is Outcome.OUT_OF_POLICY


@pytest.mark.parametrize(
    "status", [FulfillmentStatus.PENDING, FulfillmentStatus.SHIPPED, FulfillmentStatus.CANCELLED]
)
def test_r09_undelivered_orders_are_out_of_policy(status: FulfillmentStatus) -> None:
    order = replace(ORDER, status=status, delivered_at=None)
    result = evaluate_proposal(proposal_inputs(order=order), CONFIG)
    assert RuleId.R09_ORDER_STATUS in fired_ids(result)
    assert result.outcome is Outcome.OUT_OF_POLICY


def test_r08_window_boundary_is_inclusive() -> None:
    on_boundary = replace(ORDER, delivered_at=NOW - CONFIG.refund_window)
    assert evaluate_proposal(proposal_inputs(order=on_boundary), CONFIG).outcome is Outcome.AUTO


# --- capacity, pending exposure and abuse paths --------------------------------------


def test_r05_counts_capacity_and_pending_exposure_at_proposal() -> None:
    awaiting = record(
        7_000, action_id="a", status=RunStatus.AWAIT_APPROVAL, source=None, approved_at=None
    )
    result = evaluate_proposal(proposal_inputs(history=(awaiting,)), CONFIG)
    assert RuleId.R05_ORDER_CHARGED_CAP in fired_ids(result)


def test_r06_customer_limit_counts_unknown_of_any_age() -> None:
    old_unknown = record(
        19_000,
        action_id="a",
        status=RunStatus.NEEDS_RECONCILIATION,
        outcome=RefundOutcome.UNKNOWN,
        order_id="ord_other",
        sent_at=NOW - timedelta(days=90),
    )
    result = evaluate_proposal(proposal_inputs(history=(old_unknown,)), CONFIG)
    assert RuleId.R06_CUSTOMER_LIMIT in fired_ids(result)


def test_split_refunds_hit_the_auto_approve_budget_at_the_boundary() -> None:
    """$40 sent + $40 approved-but-unsent: $20 more fits the $100 budget, $25 doesn't."""
    history = (
        record(
            4_000,
            action_id="a0",
            status=RunStatus.COMPLETED,
            outcome=RefundOutcome.CONFIRMED,
            order_id="ord_0",
        ),
        record(4_000, action_id="a1", status=RunStatus.APPROVED, order_id="ord_1b"),
    )
    at_budget = evaluate_proposal(
        proposal_inputs(history=history, proposal=proposal(amount=2_000)), CONFIG
    )
    assert at_budget.outcome is Outcome.AUTO
    over_budget = evaluate_proposal(
        proposal_inputs(history=history, proposal=proposal(amount=2_500)), CONFIG
    )
    assert fired_ids(over_budget) == {RuleId.R11_AUTO_APPROVE_BUDGET}
    assert over_budget.outcome is Outcome.REVIEW


def test_budget_ignores_reviewer_approved_and_escalated_before_send() -> None:
    history = (
        record(
            8_000,
            action_id="a",
            status=RunStatus.APPROVED,
            order_id="ord_x",
            source=ApprovalSource.REVIEWER,
        ),
        record(8_000, action_id="b", status=RunStatus.ESCALATED, order_id="ord_y"),
    )
    result = evaluate_proposal(proposal_inputs(history=history), CONFIG)
    assert RuleId.R11_AUTO_APPROVE_BUDGET not in fired_ids(result)


def test_small_refund_while_large_one_awaits_review_goes_to_review() -> None:
    """ADR-009 review 3: a second conversation must not auto-approve on the same order."""
    awaiting = record(
        1_000, action_id="big", status=RunStatus.AWAIT_APPROVAL, source=None, approved_at=None
    )
    result = evaluate_proposal(proposal_inputs(history=(awaiting,)), CONFIG)
    assert RuleId.R12_OTHER_ACTION_ON_ORDER in fired_ids(result)
    assert result.outcome is Outcome.REVIEW


def test_rejected_and_escalated_actions_stop_counting_for_r12() -> None:
    history = (
        record(1_000, action_id="a", status=RunStatus.ESCALATED, outcome=RefundOutcome.REJECTED),
        record(1_000, action_id="b", status=RunStatus.ESCALATED),
    )
    result = evaluate_proposal(proposal_inputs(history=history), CONFIG)
    assert result.outcome is Outcome.AUTO


def test_r18_decline_within_cooldown_on_same_order() -> None:
    recent = ReviewDecline(order_id=ORDER_ID, declined_at=NOW - timedelta(days=2))
    old = ReviewDecline(order_id=ORDER_ID, declined_at=NOW - timedelta(days=8))
    other = ReviewDecline(order_id="ord_other", declined_at=NOW)
    assert evaluate_proposal(proposal_inputs(declines=(recent,)), CONFIG).outcome is Outcome.REVIEW
    assert evaluate_proposal(proposal_inputs(declines=(old, other)), CONFIG).outcome is Outcome.AUTO


# --- breaker -------------------------------------------------------------------------


def test_r13_threshold_sends_to_review_and_trips() -> None:
    result = evaluate_proposal(
        proposal_inputs(global_policy_approved_in_breaker_window_minor=99_000), CONFIG
    )
    assert result.outcome is Outcome.REVIEW
    assert result.trip_breaker is True


def test_out_of_policy_proposal_never_trips_the_breaker() -> None:
    result = evaluate_proposal(
        proposal_inputs(
            proposal=proposal(amount=0), global_policy_approved_in_breaker_window_minor=99_999
        ),
        CONFIG,
    )
    assert result.outcome is Outcome.OUT_OF_POLICY
    assert result.trip_breaker is False


# --- phase 2: queue admission --------------------------------------------------------


def test_full_queues_never_block_an_auto_approvable_refund() -> None:
    result = evaluate_proposal(
        proposal_inputs(customer_pending_reviews=99, global_pending_reviews=999), CONFIG
    )
    assert result.outcome is Outcome.AUTO


@pytest.mark.parametrize(
    ("overrides", "rule"),
    [
        ({"customer_pending_reviews": 3}, RuleId.R16_PENDING_REVIEW_CAP),
        ({"global_pending_reviews": 50}, RuleId.R19_GLOBAL_REVIEW_QUEUE_CAP),
    ],
)
def test_full_queue_escalates_a_refund_headed_for_review(
    overrides: dict[str, object], rule: RuleId
) -> None:
    result = evaluate_proposal(
        proposal_inputs(proposal=proposal(amount=6_000), **overrides), CONFIG
    )
    assert rule in fired_ids(result)
    assert result.outcome is Outcome.OUT_OF_POLICY


# --- fail closed ---------------------------------------------------------------------


def test_missing_delivered_at_fails_closed_without_hiding_other_rules() -> None:
    order = replace(ORDER, delivered_at=None, chargeback_open=True)
    result = evaluate_proposal(proposal_inputs(order=order), CONFIG)
    assert result.outcome is Outcome.FAIL_CLOSED
    assert "FAIL_CLOSED:R08_REFUND_WINDOW" in fired_ids(result)
    assert RuleId.R10_NO_CHARGEBACK in fired_ids(result)


def test_unrecognized_order_status_fails_closed() -> None:
    order = replace(ORDER, status="refunded")  # type: ignore[arg-type]
    result = evaluate_proposal(proposal_inputs(order=order), CONFIG)
    assert result.outcome is Outcome.FAIL_CLOSED


def test_missing_session_user_fails_closed() -> None:
    result = evaluate_proposal(proposal_inputs(session_user_id=""), CONFIG)
    assert result.outcome is Outcome.FAIL_CLOSED


# --- pre-send ------------------------------------------------------------------------


def test_presend_proceeds_on_clean_refund() -> None:
    assert evaluate_presend(presend_inputs(), CONFIG).outcome is PresendOutcome.PROCEED


def test_presend_uses_capacity_without_pending_exposure() -> None:
    """ADR-004 step 6: an approved action must not be blocked by other unsent approvals."""
    other_approved = record(7_000, action_id="other", status=RunStatus.APPROVED)
    result = evaluate_presend(presend_inputs(history=(other_approved,)), CONFIG)
    assert result.outcome is PresendOutcome.PROCEED


def test_presend_not_blocked_by_its_own_reservation() -> None:
    """A $40 refund on an order charged $50 must not count itself as $80."""
    tight = replace(ORDER, charged_minor=5_000)
    own = record(
        4_000, action_id="act_new", status=RunStatus.EXECUTING, outcome=RefundOutcome.UNKNOWN
    )
    result = evaluate_presend(
        presend_inputs(proposal=proposal(amount=4_000), order=tight, history=(own,)), CONFIG
    )
    assert result.outcome is PresendOutcome.PROCEED


def test_chargeback_opened_while_waiting_blocks_the_send() -> None:
    result = evaluate_presend(presend_inputs(order=replace(ORDER, chargeback_open=True)), CONFIG)
    assert result.outcome is PresendOutcome.OUT_OF_POLICY


def test_refund_window_is_not_rechecked_before_send() -> None:
    late = replace(ORDER, delivered_at=NOW - timedelta(days=45))
    assert evaluate_presend(presend_inputs(order=late), CONFIG).outcome is PresendOutcome.PROCEED


@pytest.mark.parametrize(
    ("controls", "by_policy", "expected"),
    [
        (PolicyControls(breaker_tripped=True, sends_enabled=True), True, PresendOutcome.HOLD),
        (PolicyControls(breaker_tripped=True, sends_enabled=True), False, PresendOutcome.PROCEED),
        (PolicyControls(breaker_tripped=False, sends_enabled=False), True, PresendOutcome.HOLD),
        (PolicyControls(breaker_tripped=False, sends_enabled=False), False, PresendOutcome.HOLD),
    ],
    ids=[
        "breaker-holds-policy",
        "breaker-spares-reviewer",
        "kill-holds-policy",
        "kill-holds-reviewer",
    ],
)
def test_hold_scope_breaker_vs_kill_switch(
    controls: PolicyControls, by_policy: bool, expected: PresendOutcome
) -> None:
    result = evaluate_presend(
        presend_inputs(controls=controls, approved_by_policy=by_policy), CONFIG
    )
    assert result.outcome is expected


def test_out_of_policy_beats_hold_at_presend() -> None:
    result = evaluate_presend(
        presend_inputs(
            controls=PolicyControls(breaker_tripped=False, sends_enabled=False),
            order=replace(ORDER, chargeback_open=True),
        ),
        CONFIG,
    )
    assert result.outcome is PresendOutcome.OUT_OF_POLICY


# --- config validation ----------------------------------------------------------------


def test_valid_config_passes() -> None:
    CONFIG.validate()


@pytest.mark.parametrize(
    "overrides",
    [
        {"currency": "usd"},
        {"auto_approve_max_minor": 0},
        {"auto_approve_budget_minor": 4_000},
        {"per_customer_limit_minor": 9_000},
        {"refund_window": timedelta(0)},
        {"pending_review_cap": 0},
    ],
)
def test_invalid_config_refuses_startup(overrides: dict[str, object]) -> None:
    with pytest.raises(ConfigError):
        replace(CONFIG, **overrides).validate()  # type: ignore[arg-type]


def test_default_controls_do_not_hold() -> None:
    assert CONTROLS.sends_enabled and not CONTROLS.breaker_tripped
