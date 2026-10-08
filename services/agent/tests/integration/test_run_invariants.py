"""The database CHECK constraints must agree with agent.core exactly (ADR-008, ADR-009)."""

import uuid
from datetime import timedelta
from itertools import product
from typing import Any

import pytest
from conftest import T0, USER
from sqlalchemy import Connection, Engine, insert, text, update
from sqlalchemy.exc import IntegrityError

from agent.core.lifecycle import RefundOutcome, RunState, RunStatus, invariant_violations
from agent.db.schema import conversations, customers, runs

APPROVED_OR_LATER = {RunStatus.APPROVED, RunStatus.EXECUTING, RunStatus.NEEDS_RECONCILIATION}


def run_row(conversation: uuid.UUID, **values: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "id": uuid.uuid4(),
        "conversation_id": conversation,
        "user_id": USER,
        "status": RunStatus.AGENT_STEP,
        "refund_outcome": RefundOutcome.NONE,
    }
    row.update(values)
    return row


def accepts(connection: Connection, row: dict[str, Any]) -> bool:
    """Insert inside a savepoint that is always rolled back; True if the DB accepted it."""
    savepoint = connection.begin_nested()
    try:
        connection.execute(insert(runs).values(**row))
    except IntegrityError:
        savepoint.rollback()
        return False
    savepoint.rollback()
    return True


def parity_mismatches(
    connection: Connection, conversation: uuid.UUID
) -> list[tuple[RunState, str]]:
    """Insert every ADR-008 state; return the ones where the DB and core disagree."""
    mismatches: list[tuple[RunState, str]] = []
    flags = (None, T0)
    for status, outcome, first_send, worker, deadline, escalated in product(
        RunStatus, RefundOutcome, flags, (None, "w1"), flags, flags
    ):
        state = RunState(status, outcome, first_send, worker, deadline, escalated)
        # Approval fields are set consistently, so only the ADR-008 invariants vary here.
        approved = status in APPROVED_OR_LATER or outcome is not RefundOutcome.NONE
        row = run_row(
            conversation,
            status=status,
            refund_outcome=outcome,
            first_send_started_at=first_send,
            worker_id=worker,
            review_deadline=deadline,
            escalated_at=escalated,
            approval_source="policy" if approved else None,
            approved_at=T0 if approved else None,
        )
        core_ok = invariant_violations(state) == []
        if accepts(connection, row) != core_ok:
            mismatches.append((state, "core accepts" if core_ok else "core rejects"))
    return mismatches


def test_database_agrees_with_core_on_every_state(
    runtime: Connection, conversation_id: uuid.UUID
) -> None:
    mismatches = parity_mismatches(runtime, conversation_id)
    assert mismatches == [], f"{len(mismatches)} disagreements, first: {mismatches[:3]}"


def test_negative_control_parity_detects_a_dropped_check(owner_engine: Engine) -> None:
    """Drop one CHECK inside a rolled-back transaction; parity must notice."""
    with owner_engine.connect() as owner:
        transaction = owner.begin()
        try:
            owner.execute(text("ALTER TABLE runs DROP CONSTRAINT ck_runs_terminal_not_unknown"))
            owner.execute(insert(customers).values(id=USER))
            conversation = uuid.uuid4()
            owner.execute(insert(conversations).values(id=conversation, user_id=USER))
            mismatches = parity_mismatches(owner, conversation)
        finally:
            transaction.rollback()
    assert mismatches, "parity test did not notice a missing CHECK constraint"
    assert all(reason == "core rejects" for _, reason in mismatches)


APPROVED = {"status": RunStatus.APPROVED, "approved_at": T0}


@pytest.mark.parametrize(
    ("values", "reason"),
    [
        ({**APPROVED, "approval_source": "reviewer"}, "reviewer approval without approver"),
        (
            {**APPROVED, "approval_source": "policy", "approved_by": "user_bob"},
            "policy approval with an approver",
        ),
        ({**APPROVED, "approval_source": "reviewer", "approved_by": USER}, "self-approval"),
        ({"status": RunStatus.APPROVED, "approval_source": "policy"}, "approved without time"),
        (
            {
                "status": RunStatus.COMPLETED,
                "refund_outcome": RefundOutcome.CONFIRMED,
                "first_send_started_at": T0,
            },
            "completed refund that was never approved",
        ),
    ],
)
def test_approval_invariants(
    runtime: Connection, conversation_id: uuid.UUID, values: dict[str, Any], reason: str
) -> None:
    assert not accepts(runtime, run_row(conversation_id, **values)), reason


def test_valid_reviewer_approval_is_accepted(
    runtime: Connection, conversation_id: uuid.UUID
) -> None:
    row = run_row(conversation_id, **APPROVED, approval_source="reviewer", approved_by="user_bob")
    assert accepts(runtime, row)


def test_one_unfinished_run_per_conversation(
    runtime: Connection, conversation_id: uuid.UUID
) -> None:
    first = run_row(conversation_id)
    runtime.execute(insert(runs).values(**first))
    assert not accepts(runtime, run_row(conversation_id)), "second unfinished run accepted"
    runtime.execute(update(runs).where(runs.c.id == first["id"]).values(status=RunStatus.COMPLETED))
    assert accepts(runtime, run_row(conversation_id)), "new run refused after completion"


@pytest.mark.parametrize("column", ["action_id", "idempotency_key"])
def test_unique_columns(runtime: Connection, conversation_id: uuid.UUID, column: str) -> None:
    runtime.execute(
        insert(runs).values(
            **run_row(conversation_id, status=RunStatus.COMPLETED, **{column: "k1"})
        )
    )
    duplicate = run_row(conversation_id, status=RunStatus.COMPLETED, **{column: "k1"})
    assert not accepts(runtime, duplicate)


def test_amount_must_be_positive(runtime: Connection, conversation_id: uuid.UUID) -> None:
    assert not accepts(runtime, run_row(conversation_id, amount_minor=0))
    assert accepts(runtime, run_row(conversation_id, amount_minor=1))


def test_review_deadline_must_be_in_future_is_not_a_db_rule(
    runtime: Connection, conversation_id: uuid.UUID
) -> None:
    """Deadlines are enforced by the compare-and-set (ADR-003), not by a CHECK."""
    past = run_row(
        conversation_id, status=RunStatus.AWAIT_APPROVAL, review_deadline=T0 - timedelta(days=1)
    )
    assert accepts(runtime, past)
