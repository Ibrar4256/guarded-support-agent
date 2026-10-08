"""Property-based tests for the ADR-009 policy (Hypothesis).

Each property has a negative control: a planted bug that Hypothesis must find, proving the
property can fail.
"""

from dataclasses import replace
from datetime import timedelta

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import find as hypothesis_find
from hypothesis import strategies as st
from policy_fixtures import CONFIG, NOW, ORDER, ORDER_ID, USER, proposal_inputs

import agent.core.policy as policy_module
from agent.core.capacity import ApprovalSource, RefundRecord, policy_approved_in_window
from agent.core.lifecycle import RefundOutcome, RunStatus
from agent.core.policy import (
    Fired,
    FulfillmentStatus,
    Outcome,
    ProposalInputs,
    RefundProposal,
    RuleId,
    _most_severe,
    evaluate_proposal,
)

USERS = [USER, "user_mallory"]
CURRENCIES = ["USD", "EUR"]
amounts = st.integers(min_value=-1_000, max_value=30_000)


@st.composite
def refund_records(draw: st.DrawFn) -> RefundRecord:
    status = draw(st.sampled_from(list(RunStatus)))
    outcome = draw(st.sampled_from(list(RefundOutcome)))
    sent = None if outcome is RefundOutcome.NONE else NOW - timedelta(days=draw(st.integers(0, 90)))
    source = draw(st.sampled_from([*ApprovalSource, None]))
    return RefundRecord(
        action_id=draw(st.sampled_from(["h1", "h2", "h3", "h4", "act_new"])),
        order_id=draw(st.sampled_from([ORDER_ID, "ord_other"])),
        amount_minor=draw(st.integers(1, 20_000)),
        currency="USD",
        status=status,
        outcome=outcome,
        first_send_started_at=sent,
        approval_source=source,
        approved_at=None if source is None else NOW - timedelta(days=draw(st.integers(0, 60))),
    )


def near_baseline[T](baseline: T, mutation: st.SearchStrategy[T]) -> st.SearchStrategy[T]:
    """Keep the clean default most of the time, so AUTO decisions are actually reached.

    Drawing every field at random makes AUTO so rare (every rule must pass at once) that a
    property about AUTO decisions would pass without checking anything.
    """
    return st.one_of(st.just(baseline), st.just(baseline), mutation)


@st.composite
def any_inputs(draw: st.DrawFn) -> ProposalInputs:
    currency = draw(near_baseline("USD", st.sampled_from(CURRENCIES)))
    delivered_days = draw(st.integers(0, 60))
    order = replace(
        ORDER,
        owner_user_id=draw(near_baseline(USER, st.sampled_from(USERS))),
        charged_minor=draw(st.integers(1, 30_000)),
        currency=draw(near_baseline("USD", st.sampled_from(CURRENCIES))),
        status=draw(
            near_baseline(FulfillmentStatus.DELIVERED, st.sampled_from(list(FulfillmentStatus)))
        ),
        delivered_at=draw(
            near_baseline(
                ORDER.delivered_at, st.sampled_from([None, NOW - timedelta(days=delivered_days)])
            )
        ),
        chargeback_open=draw(near_baseline(False, st.booleans())),
    )
    proposal = RefundProposal("act_new", ORDER_ID, draw(amounts), currency)
    return proposal_inputs(
        proposal=proposal,
        order=order,
        session_user_id=draw(near_baseline(USER, st.sampled_from(USERS))),
        looked_up_order_ids=draw(
            near_baseline(
                frozenset({ORDER_ID}), st.sampled_from([frozenset(), frozenset({ORDER_ID})])
            )
        ),
        history=tuple(draw(st.lists(refund_records(), max_size=6))),
        customer_pending_reviews=draw(st.integers(0, 5)),
        customer_proposals_last_24h=draw(near_baseline(0, st.integers(0, 6))),
        global_policy_approved_in_breaker_window_minor=draw(
            near_baseline(0, st.integers(0, 120_000))
        ),
        global_pending_reviews=draw(st.integers(0, 60)),
        has_unresolved_escalation=draw(near_baseline(False, st.booleans())),
    )


def test_generator_actually_reaches_auto() -> None:
    """Guards against a vacuous property: the generator must produce AUTO decisions."""
    example = hypothesis_find(
        any_inputs(),
        lambda i: evaluate_proposal(i, CONFIG).outcome is Outcome.AUTO,
        settings=settings(max_examples=500, database=None),
    )
    assert evaluate_proposal(example, CONFIG).outcome is Outcome.AUTO


def auto_violations(inputs: ProposalInputs) -> list[str]:
    """Return the invariants an AUTO decision would break (empty means AUTO is safe)."""
    p, order = inputs.proposal, inputs.order
    problems = []
    if p.amount_minor <= 0:
        problems.append("non-positive amount")
    if order.owner_user_id != inputs.session_user_id:
        problems.append("cross-user")
    if p.currency != CONFIG.currency or p.currency != order.currency:
        problems.append("currency")
    if p.amount_minor > order.charged_minor:
        problems.append("above charged")
    budget_used = policy_approved_in_window(
        inputs.history, exclude_action_id=p.action_id, now=NOW, window=CONFIG.per_customer_window
    )
    if budget_used + p.amount_minor > CONFIG.auto_approve_budget_minor:
        problems.append("over budget")
    return problems


@settings(max_examples=400, suppress_health_check=[HealthCheck.too_slow])
@given(any_inputs())
def test_auto_never_violates_hard_invariants(inputs: ProposalInputs) -> None:
    result = evaluate_proposal(inputs, CONFIG)  # must never raise
    if result.outcome is Outcome.AUTO:
        assert auto_violations(inputs) == []


@settings(max_examples=200)
@given(st.lists(st.integers(1, 5_000), min_size=1, max_size=12))
def test_splitting_never_auto_approves_more_than_the_budget(splits: list[int]) -> None:
    """Approve each split in turn; total auto-approved stays within the budget."""
    history: list[RefundRecord] = []
    approved_total = 0
    for i, amount in enumerate(splits):
        order_id = f"ord_{i}"
        order = replace(ORDER, order_id=order_id, charged_minor=10_000)
        inputs = proposal_inputs(
            proposal=RefundProposal(f"act_{i}", order_id, amount, "USD"),
            order=order,
            looked_up_order_ids=frozenset({order_id}),
            history=tuple(history),
        )
        if evaluate_proposal(inputs, CONFIG).outcome is Outcome.AUTO:
            approved_total += amount
            history.append(
                RefundRecord(
                    f"act_{i}",
                    order_id,
                    amount,
                    "USD",
                    RunStatus.APPROVED,
                    RefundOutcome.NONE,
                    None,
                    ApprovalSource.POLICY,
                    NOW,
                )
            )
    assert approved_total <= CONFIG.auto_approve_budget_minor


fired_entries = st.builds(Fired, st.sampled_from(list(RuleId)), st.sampled_from(list(Outcome)))


@given(st.lists(fired_entries), fired_entries)
def test_adding_a_fired_rule_never_lowers_severity(fired: list[Fired], extra: Fired) -> None:
    before = _most_severe(fired, Outcome.AUTO)
    after = _most_severe([*fired, extra], Outcome.AUTO)
    assert after >= before


# --- negative controls: planted bugs must be found ----------------------------------


def test_negative_control_hypothesis_finds_a_disabled_ownership_rule(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(policy_module, "_order_ownership", lambda order, session_user_id: True)
    counterexample = hypothesis_find(
        any_inputs(),
        lambda i: (
            evaluate_proposal(i, CONFIG).outcome is Outcome.AUTO
            and "cross-user" in auto_violations(i)
        ),
        settings=settings(max_examples=2_000, database=None),
    )
    assert counterexample.order.owner_user_id != counterexample.session_user_id


def test_negative_control_hypothesis_finds_a_missing_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(policy_module, "policy_approved_in_window", lambda *args, **kwargs: 0)
    counterexample = hypothesis_find(
        st.lists(st.integers(1, 5_000), min_size=1, max_size=12),
        lambda splits: _approved_total(splits) > CONFIG.auto_approve_budget_minor,
        settings=settings(max_examples=2_000, database=None),
    )
    assert sum(counterexample) > CONFIG.auto_approve_budget_minor


def _approved_total(splits: list[int]) -> int:
    history: list[RefundRecord] = []
    total = 0
    for i, amount in enumerate(splits):
        order_id = f"ord_{i}"
        inputs = proposal_inputs(
            proposal=RefundProposal(f"act_{i}", order_id, amount, "USD"),
            order=replace(ORDER, order_id=order_id, charged_minor=10_000),
            looked_up_order_ids=frozenset({order_id}),
            history=tuple(history),
        )
        if evaluate_proposal(inputs, CONFIG).outcome is Outcome.AUTO:
            total += amount
            history.append(
                RefundRecord(
                    f"act_{i}",
                    order_id,
                    amount,
                    "USD",
                    RunStatus.APPROVED,
                    RefundOutcome.NONE,
                    None,
                    ApprovalSource.POLICY,
                    NOW,
                )
            )
    return total
