from datetime import UTC, datetime
from itertools import product

import pytest

from agent.core.lifecycle import (
    ALLOWED_TRANSITIONS,
    IllegalTransitionError,
    RefundOutcome,
    RunState,
    RunStatus,
    invariant_violations,
    is_allowed_transition,
    require_transition,
)

T0 = datetime(2026, 10, 8, 12, 0, tzinfo=UTC)


def state(
    status: RunStatus,
    outcome: RefundOutcome = RefundOutcome.NONE,
    *,
    first_send: datetime | None = None,
    worker_id: str | None = None,
    review_deadline: datetime | None = None,
    escalated_at: datetime | None = None,
) -> RunState:
    return RunState(status, outcome, first_send, worker_id, review_deadline, escalated_at)


def test_seven_saved_statuses_stay_under_adr001_threshold() -> None:
    assert len(RunStatus) == 7


def test_transition_table_matches_adr008_exactly() -> None:
    expected_count = 15
    assert len(ALLOWED_TRANSITIONS) == expected_count


@pytest.mark.parametrize(
    ("source", "target"),
    [
        (s, t)
        for s, t in product([None, *RunStatus], RunStatus)
        if (s, t) not in ALLOWED_TRANSITIONS
    ],
)
def test_every_unlisted_transition_is_refused(source: RunStatus | None, target: RunStatus) -> None:
    assert not is_allowed_transition(source, target)
    with pytest.raises(IllegalTransitionError):
        require_transition(source, target)


@pytest.mark.parametrize("terminal", [RunStatus.COMPLETED, RunStatus.ESCALATED])
def test_terminal_statuses_have_no_outgoing_transitions(terminal: RunStatus) -> None:
    assert not any(source == terminal for source, _ in ALLOWED_TRANSITIONS)


def test_nothing_leaves_uncertainty_except_reconciliation_or_resolution() -> None:
    from_executing = {t for s, t in ALLOWED_TRANSITIONS if s == RunStatus.EXECUTING}
    assert from_executing == {
        RunStatus.COMPLETED,
        RunStatus.ESCALATED,
        RunStatus.NEEDS_RECONCILIATION,
    }


LEGAL_STATES = [
    pytest.param(state(RunStatus.AGENT_STEP), id="agent-step"),
    pytest.param(state(RunStatus.AWAIT_APPROVAL, review_deadline=T0), id="await-approval"),
    pytest.param(state(RunStatus.APPROVED, worker_id="w1"), id="approved-claimed"),
    pytest.param(
        state(RunStatus.EXECUTING, RefundOutcome.UNKNOWN, first_send=T0, worker_id="w1"),
        id="executing",
    ),
    pytest.param(
        state(
            RunStatus.NEEDS_RECONCILIATION, RefundOutcome.UNKNOWN, first_send=T0, escalated_at=T0
        ),
        id="needs-reconciliation",
    ),
    pytest.param(
        state(RunStatus.COMPLETED, RefundOutcome.CONFIRMED, first_send=T0),
        id="completed-confirmed",
    ),
    pytest.param(
        state(RunStatus.COMPLETED, RefundOutcome.CONFIRMED, first_send=T0, escalated_at=T0),
        id="completed-after-alert",
    ),
    pytest.param(state(RunStatus.COMPLETED), id="reviewer-declined"),
    pytest.param(
        state(RunStatus.ESCALATED, escalated_at=T0), id="rejected-at-execution-nothing-sent"
    ),
    pytest.param(
        state(RunStatus.ESCALATED, RefundOutcome.REJECTED, first_send=T0, escalated_at=T0),
        id="escalated-rejected",
    ),
]


@pytest.mark.parametrize("run_state", LEGAL_STATES)
def test_legal_states_have_no_violations(run_state: RunState) -> None:
    assert invariant_violations(run_state) == []


ILLEGAL_STATES = [
    pytest.param(
        state(RunStatus.COMPLETED, RefundOutcome.UNKNOWN, first_send=T0),
        id="completed-with-unknown-outcome",
    ),
    pytest.param(
        state(RunStatus.ESCALATED, RefundOutcome.UNKNOWN, first_send=T0, escalated_at=T0),
        id="escalated-with-unknown-outcome",
    ),
    pytest.param(state(RunStatus.EXECUTING), id="executing-with-outcome-none"),
    pytest.param(
        state(RunStatus.APPROVED, RefundOutcome.UNKNOWN, first_send=T0),
        id="approved-with-unknown-outcome",
    ),
    pytest.param(
        state(RunStatus.COMPLETED, RefundOutcome.CONFIRMED, first_send=None),
        id="outcome-set-without-first-send",
    ),
    pytest.param(state(RunStatus.AGENT_STEP, first_send=T0), id="first-send-with-outcome-none"),
    pytest.param(
        state(RunStatus.COMPLETED, RefundOutcome.CONFIRMED, first_send=T0, worker_id="w1"),
        id="finished-run-holds-claim",
    ),
    pytest.param(state(RunStatus.AWAIT_APPROVAL), id="await-approval-without-deadline"),
    pytest.param(state(RunStatus.ESCALATED), id="escalated-without-escalated-at"),
]


@pytest.mark.parametrize("run_state", ILLEGAL_STATES)
def test_illegal_states_are_reported(run_state: RunState) -> None:
    assert invariant_violations(run_state) != []
