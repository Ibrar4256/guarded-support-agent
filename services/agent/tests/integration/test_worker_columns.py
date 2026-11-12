"""Migration 0002's worker columns (ADR-014 S1, S12): each CHECK rejects its own bad row, and
the migration refuses runs that were already sent."""

import uuid
from collections.abc import Iterator
from typing import Any

import pytest
from conftest import T0, USER, run_alembic
from sqlalchemy import Connection, Engine, insert, text
from sqlalchemy.exc import IntegrityError

from agent.core.lifecycle import RefundOutcome, RunStatus
from agent.db.schema import conversations, customers, runs

WINDOW_MS = 60_000


def sent_run(conversation: uuid.UUID, **values: Any) -> dict[str, Any]:
    """A valid run that has been sent once."""
    row: dict[str, Any] = {
        "id": uuid.uuid4(),
        "conversation_id": conversation,
        "user_id": USER,
        "status": RunStatus.EXECUTING,
        "refund_outcome": RefundOutcome.UNKNOWN,
        "approval_source": "policy",
        "approved_at": T0,
        "first_send_started_at": T0,
        "send_window_ms": WINDOW_MS,
        "send_attempts": 1,
        "max_client_timeout_ms": 8_000,
        "in_flight_bound_ms": 13_000,
    }
    row.update(values)
    return row


UNSENT = {
    "status": RunStatus.APPROVED,
    "refund_outcome": RefundOutcome.NONE,
    "first_send_started_at": None,
    "send_window_ms": None,
    "send_attempts": 0,
    "max_client_timeout_ms": None,
    "in_flight_bound_ms": None,
}

BAD_ROWS = [
    pytest.param("send_attempts_non_negative", {"send_attempts": -1}, id="negative-attempts"),
    pytest.param("window_iff_first_send", {"send_window_ms": None}, id="sent-without-snapshot"),
    pytest.param(
        "window_iff_first_send", {**UNSENT, "send_window_ms": WINDOW_MS}, id="snapshot-unsent"
    ),
    pytest.param("window_positive", {"send_window_ms": 0}, id="zero-window"),
    pytest.param(
        "no_attempts_before_send",
        {
            **UNSENT,
            "send_attempts": 1,
            "max_client_timeout_ms": 8_000,
            "in_flight_bound_ms": 13_000,
        },
        id="attempt-before-send",
    ),
    pytest.param("release_streak_non_negative", {"release_streak": -1}, id="negative-streak"),
    # ADR-016 S4
    pytest.param("in_flight_bound_iff_sent", {"in_flight_bound_ms": None}, id="sent-without-bound"),
    pytest.param(
        "max_timeout_iff_sent", {"max_client_timeout_ms": None}, id="sent-without-timeout"
    ),
    pytest.param(
        "in_flight_bound_iff_sent",
        {**UNSENT, "in_flight_bound_ms": 13_000, "max_client_timeout_ms": None},
        id="bound-without-send",
    ),
    pytest.param(
        "in_flight_bound_covers_timeout",
        {"in_flight_bound_ms": 8_000},
        id="bound-not-above-timeout",
    ),
    pytest.param(
        "max_timeout_positive",
        {"max_client_timeout_ms": 0, "in_flight_bound_ms": 5_000},
        id="zero-timeout",
    ),
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


def test_valid_sent_and_unsent_rows_are_accepted(
    runtime: Connection, conversation_id: uuid.UUID
) -> None:
    """Control: the new CHECKs don't reject everything."""
    assert rejected_by(runtime, sent_run(conversation_id)) is None
    assert rejected_by(runtime, sent_run(conversation_id, **UNSENT)) is None


@pytest.mark.parametrize(("constraint", "values"), BAD_ROWS)
def test_each_check_rejects_its_bad_row(
    runtime: Connection, conversation_id: uuid.UUID, constraint: str, values: dict[str, Any]
) -> None:
    assert rejected_by(runtime, sent_run(conversation_id, **values)) == f"ck_runs_{constraint}"


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
            violated = rejected_by(owner, sent_run(conversation, **values))
        finally:
            transaction.rollback()
    assert violated is None, f"still rejected by {violated}"


@pytest.fixture
def at_revision_0001(owner_engine: Engine) -> Iterator[None]:
    """Run a test against the pre-0002 schema; always clean up and return to head."""
    result = run_alembic("downgrade", "0001")
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


def insert_0001_run(owner_engine: Engine, *, sent: bool) -> None:
    """A run as 0001 stored it: no worker columns."""
    conversation = uuid.uuid4()
    row = sent_run(conversation) if sent else sent_run(conversation, **UNSENT)
    for column in ("send_window_ms", "send_attempts"):
        row.pop(column)
    with owner_engine.begin() as connection:
        connection.execute(insert(customers).values(id=USER))
        connection.execute(insert(conversations).values(id=conversation, user_id=USER))
        connection.execute(
            text(
                "INSERT INTO runs (id, conversation_id, user_id, status, refund_outcome, "
                "approval_source, approved_at, first_send_started_at) VALUES (:id, "
                ":conversation_id, :user_id, :status, :refund_outcome, :approval_source, "
                ":approved_at, :first_send_started_at)"
            ),
            {**row, "status": row["status"].value, "refund_outcome": row["refund_outcome"].value},
        )


@pytest.mark.usefixtures("at_revision_0001")
def test_migration_refuses_runs_already_sent(owner_engine: Engine) -> None:
    insert_0001_run(owner_engine, sent=True)
    result = run_alembic("upgrade", "0002")
    assert result.returncode != 0
    assert "window snapshot is unknown" in result.stderr


@pytest.mark.usefixtures("at_revision_0001")
def test_negative_control_migration_runs_with_only_unsent_runs(owner_engine: Engine) -> None:
    insert_0001_run(owner_engine, sent=False)
    result = run_alembic("upgrade", "0002")
    assert result.returncode == 0, result.stderr
    with owner_engine.connect() as connection:
        attempts, streak = connection.execute(
            text("SELECT send_attempts, release_streak FROM runs WHERE user_id = :u"), {"u": USER}
        ).one()
    assert (attempts, streak) == (0, 0)


# --- migration 0003 (ADR-016 S4) -----------------------------------------------------------------


@pytest.fixture
def at_revision_0002(owner_engine: Engine) -> Iterator[None]:
    result = run_alembic("downgrade", "0002")
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


def insert_0002_run(owner_engine: Engine, *, sends: int) -> None:
    """A run as 0002 stored it: no bounds."""
    conversation = uuid.uuid4()
    row = sent_run(conversation) if sends else sent_run(conversation, **UNSENT)
    with owner_engine.begin() as connection:
        connection.execute(insert(customers).values(id=USER))
        connection.execute(insert(conversations).values(id=conversation, user_id=USER))
        connection.execute(
            text(
                "INSERT INTO runs (id, conversation_id, user_id, status, refund_outcome, "
                "approval_source, approved_at, first_send_started_at, send_window_ms, "
                "send_attempts) VALUES (:id, :conversation_id, :user_id, :status, "
                ":refund_outcome, :approval_source, :approved_at, :first_send_started_at, "
                ":send_window_ms, :sends)"
            ),
            {
                **row,
                "status": row["status"].value,
                "refund_outcome": row["refund_outcome"].value,
                "sends": sends,
            },
        )


@pytest.mark.usefixtures("at_revision_0002")
def test_migration_0003_refuses_runs_already_sent(owner_engine: Engine) -> None:
    insert_0002_run(owner_engine, sends=1)
    result = run_alembic("upgrade", "0003")
    assert result.returncode != 0
    assert "in-flight bounds are unknown" in result.stderr


@pytest.mark.usefixtures("at_revision_0002")
def test_negative_control_migration_0003_runs_with_only_unsent_runs(owner_engine: Engine) -> None:
    insert_0002_run(owner_engine, sends=0)
    result = run_alembic("upgrade", "0003")
    assert result.returncode == 0, result.stderr
