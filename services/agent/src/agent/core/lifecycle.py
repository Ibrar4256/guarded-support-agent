"""Run statuses, allowed transitions and row invariants (ADR-008).

This module is the single source of truth for the run lifecycle. The database CHECK
constraints mirror ``invariant_violations``; the two are tested against each other.
"""

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum


class RunStatus(StrEnum):
    AGENT_STEP = "AGENT_STEP"
    AWAIT_APPROVAL = "AWAIT_APPROVAL"
    APPROVED = "APPROVED"
    EXECUTING = "EXECUTING"
    NEEDS_RECONCILIATION = "NEEDS_RECONCILIATION"
    COMPLETED = "COMPLETED"
    ESCALATED = "ESCALATED"


class RefundOutcome(StrEnum):
    NONE = "none"
    UNKNOWN = "unknown"
    CONFIRMED = "confirmed"
    REJECTED = "rejected"


TERMINAL_STATUSES = frozenset({RunStatus.COMPLETED, RunStatus.ESCALATED})
PRE_SEND_STATUSES = frozenset({RunStatus.AGENT_STEP, RunStatus.AWAIT_APPROVAL, RunStatus.APPROVED})
UNCERTAIN_STATUSES = frozenset({RunStatus.EXECUTING, RunStatus.NEEDS_RECONCILIATION})

# Statuses each claimant may take a lease on (ADR-004 step 7, ADR-008 Claiming).
WORKER_CLAIMABLE = frozenset({RunStatus.APPROVED, RunStatus.EXECUTING})
RECONCILER_CLAIMABLE = frozenset({RunStatus.NEEDS_RECONCILIATION})

# ADR-008 transition table. ``None`` as the source means "run created" (INTAKE).
ALLOWED_TRANSITIONS: frozenset[tuple[RunStatus | None, RunStatus]] = frozenset(
    {
        (None, RunStatus.AGENT_STEP),
        (RunStatus.AGENT_STEP, RunStatus.COMPLETED),
        (RunStatus.AGENT_STEP, RunStatus.AWAIT_APPROVAL),
        (RunStatus.AGENT_STEP, RunStatus.APPROVED),
        (RunStatus.AGENT_STEP, RunStatus.ESCALATED),
        (RunStatus.AWAIT_APPROVAL, RunStatus.APPROVED),
        (RunStatus.AWAIT_APPROVAL, RunStatus.COMPLETED),
        (RunStatus.AWAIT_APPROVAL, RunStatus.ESCALATED),
        (RunStatus.APPROVED, RunStatus.EXECUTING),
        (RunStatus.APPROVED, RunStatus.ESCALATED),
        (RunStatus.EXECUTING, RunStatus.COMPLETED),
        (RunStatus.EXECUTING, RunStatus.ESCALATED),
        (RunStatus.EXECUTING, RunStatus.NEEDS_RECONCILIATION),
        (RunStatus.NEEDS_RECONCILIATION, RunStatus.COMPLETED),
        (RunStatus.NEEDS_RECONCILIATION, RunStatus.ESCALATED),
    }
)


class IllegalTransitionError(ValueError):
    pass


def is_allowed_transition(source: RunStatus | None, target: RunStatus) -> bool:
    return (source, target) in ALLOWED_TRANSITIONS


def require_transition(source: RunStatus | None, target: RunStatus) -> None:
    if not is_allowed_transition(source, target):
        raise IllegalTransitionError(f"transition {source} -> {target} is not allowed")


@dataclass(frozen=True, slots=True)
class RunState:
    """The columns the ADR-008 invariants constrain."""

    status: RunStatus
    refund_outcome: RefundOutcome
    first_send_started_at: datetime | None
    worker_id: str | None
    review_deadline: datetime | None
    escalated_at: datetime | None


def invariant_violations(state: RunState) -> list[str]:
    """Return every ADR-008 invariant the state breaks (empty list means valid)."""
    violations: list[str] = []
    outcome_is_none = state.refund_outcome is RefundOutcome.NONE
    if outcome_is_none != (state.first_send_started_at is None):
        violations.append("refund_outcome = none iff first_send_started_at is null")
    if state.status in PRE_SEND_STATUSES and not outcome_is_none:
        violations.append(f"{state.status} requires refund_outcome = none")
    if state.status in UNCERTAIN_STATUSES and state.refund_outcome is not RefundOutcome.UNKNOWN:
        violations.append(f"{state.status} requires refund_outcome = unknown")
    if state.status in TERMINAL_STATUSES:
        if state.refund_outcome is RefundOutcome.UNKNOWN:
            violations.append(f"{state.status} cannot have refund_outcome = unknown")
        if state.worker_id is not None:
            violations.append(f"{state.status} cannot hold a worker claim")
    if state.status is RunStatus.AWAIT_APPROVAL and state.review_deadline is None:
        violations.append("AWAIT_APPROVAL requires review_deadline")
    if state.status is RunStatus.ESCALATED and state.escalated_at is None:
        violations.append("ESCALATED requires escalated_at")
    return violations
