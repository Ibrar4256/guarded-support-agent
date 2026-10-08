"""Role isolation, the append-only decision log, and migration health (ADR-005, ADR-009)."""

import uuid

import pytest
from conftest import env, run_alembic
from sqlalchemy import Connection, Engine, create_engine, insert, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import OperationalError, ProgrammingError

from agent.db.schema import decisions


def decision_row() -> dict[str, object]:
    return {
        "stage": "proposal",
        "outcome": "AUTO",
        "policy_version": "1.0.0",
        "config_snapshot": {},
        "input_snapshot": {},
        "decided_by": "policy",
        "run_id": None,
        "action_id": f"act_{uuid.uuid4().hex}",
    }


# --- append-only decision log ---------------------------------------------------------


def test_runtime_can_insert_and_read_decisions(runtime: Connection) -> None:
    runtime.execute(insert(decisions).values(**decision_row()))
    assert runtime.execute(text("SELECT count(*) FROM decisions")).scalar_one() >= 1


@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE decisions SET outcome = 'REVIEW'",
        "DELETE FROM decisions",
        "TRUNCATE decisions",
    ],
)
def test_runtime_cannot_modify_decisions(runtime: Connection, statement: str) -> None:
    runtime.execute(insert(decisions).values(**decision_row()))
    with pytest.raises(ProgrammingError, match="permission denied"):
        runtime.execute(text(statement))


def test_negative_control_owner_can_update_decisions(owner_engine: Engine) -> None:
    """Proves it's the grant, not something else, that blocks the runtime role."""
    with owner_engine.connect() as owner:
        transaction = owner.begin()
        owner.execute(insert(decisions).values(**decision_row()))
        owner.execute(text("UPDATE decisions SET outcome = 'REVIEW'"))
        transaction.rollback()


def test_runtime_role_owns_no_table(runtime: Connection) -> None:
    owned = runtime.execute(
        text(
            "SELECT tablename FROM pg_tables"
            " WHERE schemaname = 'public' AND tableowner = current_user"
        )
    ).all()
    assert owned == []


def test_runtime_cannot_grant_itself_update(runtime: Connection) -> None:
    """Postgres only WARNS ("no privileges were granted") here, so assert the effect."""
    runtime.execute(text("GRANT UPDATE ON decisions TO agent_app"))
    has_update = runtime.execute(
        text("SELECT has_table_privilege(current_user, 'decisions', 'UPDATE')")
    ).scalar_one()
    assert has_update is False
    with pytest.raises(ProgrammingError, match="permission denied"):
        runtime.execute(text("UPDATE decisions SET outcome = 'REVIEW'"))


def test_runtime_cannot_delete_from_other_tables(runtime: Connection) -> None:
    with pytest.raises(ProgrammingError, match="permission denied"):
        runtime.execute(text("DELETE FROM runs"))


# --- role isolation (four login roles) -------------------------------------------------


def can_connect(url: str) -> bool:
    engine = create_engine(url)
    try:
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
        return True
    except OperationalError:
        return False
    finally:
        engine.dispose()


def other_database(url_variable: str, database: str) -> str:
    return make_url(env(url_variable)).set(database=database).render_as_string(hide_password=False)


def test_agent_runtime_cannot_connect_to_refund_database() -> None:
    assert not can_connect(other_database("AGENT_DB_RUNTIME_URL", "refund_api"))


def test_refund_runtime_cannot_connect_to_agent_database() -> None:
    assert not can_connect(other_database("REFUND_DB_RUNTIME_URL", "agent"))


def test_negative_control_runtime_roles_reach_their_own_database() -> None:
    assert can_connect(env("AGENT_DB_RUNTIME_URL"))
    assert can_connect(env("REFUND_DB_RUNTIME_URL"))


# --- migrations -----------------------------------------------------------------------


def test_migrations_round_trip_and_match_the_schema(migrated_database: None) -> None:
    for args in (("downgrade", "base"), ("upgrade", "head"), ("check",)):
        result = run_alembic(*args)
        assert result.returncode == 0, f"alembic {' '.join(args)}: {result.stderr}"
