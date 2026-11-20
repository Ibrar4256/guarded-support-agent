"""Migration 0006's escalation reason: an ESCALATED run has one and no other run does, in the
shape agent.core.escalations writes; runs escalated before it existed are 'unrecorded'."""

import uuid
from collections.abc import Iterator
from typing import Any

import pytest
from conftest import T0, USER, run_alembic
from sqlalchemy import Connection, Engine, insert, text
from sqlalchemy.exc import IntegrityError

from agent.core import escalations
from agent.core.lifecycle import RefundOutcome, RunStatus
from agent.db.schema import conversations, customers, runs


def escalated_run(conversation: uuid.UUID, **values: Any) -> dict[str, Any]:
    """A valid run the turn escalated on its step budget."""
    row: dict[str, Any] = {
        "id": uuid.uuid4(),
        "conversation_id": conversation,
        "user_id": USER,
        "status": RunStatus.ESCALATED,
        "refund_outcome": RefundOutcome.NONE,
        "escalated_at": T0,
        "escalation_reason": escalations.STEP_BUDGET,
    }
    row.update(values)
    return row


NOT_ESCALATED = {"status": RunStatus.AGENT_STEP, "escalated_at": None, "escalation_reason": None}

# Each row breaks exactly one CHECK.
BAD_ROWS = [
    pytest.param("reason_iff_escalated", {"escalation_reason": None}, id="escalated-no-reason"),
    pytest.param(
        "reason_iff_escalated",
        {**NOT_ESCALATED, "escalation_reason": escalations.STEP_BUDGET},
        id="reason-on-a-live-run",
    ),
    pytest.param("escalation_reason_format", {"escalation_reason": "Policy"}, id="uppercase"),
    pytest.param("escalation_reason_format", {"escalation_reason": "policy:"}, id="empty-detail"),
    pytest.param(
        "escalation_reason_format",
        {"escalation_reason": "policy:R05 R06"},
        id="space-in-detail",
    ),
    pytest.param("escalation_reason_format", {"escalation_reason": ""}, id="empty"),
]


def rejected_by(connection: Connection, row: dict[str, Any]) -> str | None:
    """Insert inside a rolled-back savepoint; the violated constraint's name, or None."""
    savepoint = connection.begin_nested()
    try:
        connection.execute(insert(runs).values(**row))
    except IntegrityError as exc:
        savepoint.rollback()
        return str(exc.orig.diag.constraint_name)  # type: ignore[union-attr]
    savepoint.rollback()
    return None


def test_an_escalated_run_with_a_reason_and_a_live_run_without_are_accepted(
    runtime: Connection, conversation_id: uuid.UUID
) -> None:
    """Control: the CHECKs don't reject everything, and every shape the core writes fits."""
    reasons = [
        escalations.STEP_BUDGET,
        escalations.escalate_tool("customer_requested_person"),
        escalations.policy(["R05_ORDER_CHARGED_CAP", "R06_CUSTOMER_LIMIT"]),
        escalations.fail_closed(["FAIL_CLOSED:R09_ORDER_STATUS", "R07_AUTO_APPROVE_MAX"]),
        escalations.presend_policy([]),
        escalations.UNRECORDED,
    ]
    for reason in reasons:
        row = escalated_run(conversation_id, escalation_reason=reason)
        assert rejected_by(runtime, row) is None, reason
    assert rejected_by(runtime, escalated_run(conversation_id, **NOT_ESCALATED)) is None


@pytest.mark.parametrize(("constraint", "values"), BAD_ROWS)
def test_each_check_rejects_its_bad_row(
    runtime: Connection, conversation_id: uuid.UUID, constraint: str, values: dict[str, Any]
) -> None:
    assert rejected_by(runtime, escalated_run(conversation_id, **values)) == f"ck_runs_{constraint}"


@pytest.mark.parametrize(("constraint", "values"), BAD_ROWS)
def test_negative_control_row_is_accepted_without_its_check(
    owner_engine: Engine, constraint: str, values: dict[str, Any]
) -> None:
    """Drop only that CHECK (rolled back): the same row goes in, so the CHECK is the reason."""
    with owner_engine.connect() as owner:
        transaction = owner.begin()
        try:
            owner.execute(text(f"ALTER TABLE runs DROP CONSTRAINT ck_runs_{constraint}"))
            owner.execute(insert(customers).values(id=USER))
            conversation = uuid.uuid4()
            owner.execute(insert(conversations).values(id=conversation, user_id=USER))
            violated = rejected_by(owner, escalated_run(conversation, **values))
        finally:
            transaction.rollback()
    assert violated is None, f"still rejected by {violated}"


@pytest.fixture
def at_revision_0005(owner_engine: Engine) -> Iterator[None]:
    """Run a test against the pre-0006 schema; always clean up and return to head."""
    result = run_alembic("downgrade", "0005")
    assert result.returncode == 0, result.stderr
    try:
        yield
    finally:
        with owner_engine.begin() as connection:
            connection.execute(text("DELETE FROM runs WHERE user_id = :u"), {"u": USER})
            connection.execute(text("DELETE FROM conversations WHERE user_id = :u"), {"u": USER})
            connection.execute(text("DELETE FROM customers WHERE id = :u"), {"u": USER})
        result = run_alembic("upgrade", "head")
        assert result.returncode == 0, result.stderr


@pytest.mark.usefixtures("at_revision_0005")
def test_migration_marks_runs_escalated_before_it_unrecorded(owner_engine: Engine) -> None:
    conversation, escalated, live = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    with owner_engine.begin() as connection:
        connection.execute(insert(customers).values(id=USER))
        connection.execute(insert(conversations).values(id=conversation, user_id=USER))
        for run_id, status, escalated_at in (
            (escalated, "ESCALATED", T0),
            (live, "AGENT_STEP", None),
        ):
            connection.execute(
                text(
                    "INSERT INTO runs (id, conversation_id, user_id, status, refund_outcome, "
                    "escalated_at) VALUES (:id, :c, :u, :s, 'none', :e)"
                ),
                {"id": run_id, "c": conversation, "u": USER, "s": status, "e": escalated_at},
            )

    result = run_alembic("upgrade", "0006")

    assert result.returncode == 0, result.stderr
    with owner_engine.connect() as connection:
        reasons: dict[uuid.UUID, str | None] = dict(
            connection.execute(
                text("SELECT id, escalation_reason FROM runs WHERE user_id = :u"), {"u": USER}
            ).all()
        )
    assert reasons == {escalated: escalations.UNRECORDED, live: None}
