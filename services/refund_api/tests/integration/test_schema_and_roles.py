"""Schema invariants and runtime-role privileges (ADR-004, ADR-005).

Privilege tests assert the effect (the statement is refused), not the mechanism, and each
has a control showing the same statement succeeds for the owner, so a broken statement
can't pass as "refused".
"""

import shutil
import uuid
from pathlib import Path
from typing import Any

import pytest
from conftest import SERVICE_ROOT, run_alembic
from psycopg import errors
from sqlalchemy import Connection, delete, func, insert, select, text, update
from sqlalchemy.exc import DBAPIError

from refund_api.db.schema import charges, idempotency_keys, refunds

ORDER = "ord_1001"
HASH = "0" * 64
FREE_ORDER = "ord_unreferenced"
FREE_ACTION = "act_unreferenced"


def seed(connection: Connection) -> uuid.UUID:
    """A charge with a refund and its key, plus rows nothing references.

    The unreferenced rows let the delete statements succeed for the owner, so the privilege
    is the only thing that can make them fail (a foreign key would otherwise do it too).
    """
    connection.execute(insert(charges).values(order_id=ORDER, amount_minor=10_000, currency="USD"))
    refund_id: uuid.UUID = connection.execute(
        insert(refunds)
        .values(order_id=ORDER, action_id="act_1", amount_minor=2_000, currency="USD")
        .returning(refunds.c.id)
    ).scalar_one()
    connection.execute(
        insert(idempotency_keys).values(
            key="key_1",
            payload_hash=HASH,
            refund_id=refund_id,
            response_status=201,
            response_body='{"id": "x"}',
        )
    )
    connection.execute(insert(charges).values(order_id=FREE_ORDER, amount_minor=1, currency="USD"))
    connection.execute(
        insert(refunds).values(
            order_id=ORDER, action_id=FREE_ACTION, amount_minor=1, currency="USD"
        )
    )
    return refund_id


def refused(connection: Connection, statement: Any, error: type[Exception]) -> bool:
    """Run the statement in a savepoint; True if it fails with ``error``."""
    savepoint = connection.begin_nested()
    try:
        connection.execute(statement)
    except DBAPIError as exc:
        savepoint.rollback()
        return isinstance(exc.orig, error)
    savepoint.rollback()
    return False


# --- privileges ---------------------------------------------------------------------------

IMMUTABLE = [
    pytest.param(update(charges).values(amount_minor=1), id="update-charge"),
    pytest.param(update(refunds).values(amount_minor=1), id="update-refund"),
    pytest.param(update(idempotency_keys).values(response_status=200), id="update-key"),
    pytest.param(delete(refunds).where(refunds.c.action_id == FREE_ACTION), id="delete-refund"),
    pytest.param(delete(charges).where(charges.c.order_id == FREE_ORDER), id="delete-charge"),
    pytest.param(text("TRUNCATE refunds CASCADE"), id="truncate-refunds"),
]


@pytest.mark.parametrize("statement", IMMUTABLE)
def test_runtime_cannot_change_written_rows(runtime: Connection, statement: Any) -> None:
    seed(runtime)
    assert refused(runtime, statement, errors.InsufficientPrivilege)


@pytest.mark.parametrize("statement", IMMUTABLE)
def test_control_owner_can_run_the_same_statements(owner: Connection, statement: Any) -> None:
    seed(owner)
    assert not refused(owner, statement, Exception)


def test_runtime_can_prune_a_key_and_the_refund_survives(runtime: Connection) -> None:
    refund_id = seed(runtime)
    runtime.execute(delete(idempotency_keys).where(idempotency_keys.c.key == "key_1"))
    assert runtime.execute(select(func.count()).select_from(idempotency_keys)).scalar_one() == 0
    survivor = select(func.count()).select_from(refunds).where(refunds.c.id == refund_id)
    assert runtime.execute(survivor).scalar_one() == 1


# --- constraints --------------------------------------------------------------------------


def test_one_action_can_have_two_refund_rows(runtime: Connection) -> None:
    # Needed so dedup_disabled and the expired-key hazard can show a real second refund.
    seed(runtime)
    second = {"order_id": ORDER, "action_id": "act_1", "amount_minor": 2_000, "currency": "USD"}
    runtime.execute(insert(refunds).values(second))
    count = select(func.count()).select_from(refunds).where(refunds.c.action_id == "act_1")
    assert runtime.execute(count).scalar_one() == 2


def key_row(refund_id: uuid.UUID, **overrides: Any) -> dict[str, Any]:
    row = {
        "key": "key_2",
        "payload_hash": HASH,
        "refund_id": refund_id,
        "response_status": 201,
        "response_body": "{}",
    }
    return row | overrides


VIOLATIONS = [
    ("charge amount zero", "charge", {"amount_minor": 0}, errors.CheckViolation),
    ("charge lowercase currency", "charge", {"currency": "usd"}, errors.CheckViolation),
    ("refund amount negative", "refund", {"amount_minor": -1}, errors.CheckViolation),
    ("refund lowercase currency", "refund", {"currency": "usd"}, errors.CheckViolation),
    ("refund for unknown charge", "refund", {"order_id": "ord_none"}, errors.ForeignKeyViolation),
    ("key with short hash", "key", {"payload_hash": "abc"}, errors.CheckViolation),
    ("key with uppercase hash", "key", {"payload_hash": "A" * 64}, errors.CheckViolation),
    ("key storing a 400", "key", {"response_status": 400}, errors.CheckViolation),
    ("key for unknown refund", "key", {"refund_id": uuid.uuid4()}, errors.ForeignKeyViolation),
    ("duplicate key", "key", {"key": "key_1"}, errors.UniqueViolation),
]


@pytest.mark.parametrize(
    ("table", "overrides", "error"),
    [pytest.param(table, change, error, id=name) for name, table, change, error in VIOLATIONS],
)
def test_constraint_rejects_bad_row(
    runtime: Connection, table: str, overrides: dict[str, Any], error: type[Exception]
) -> None:
    refund_id = seed(runtime)
    valid = {
        "charge": (charges, {"order_id": "ord_2", "amount_minor": 1, "currency": "USD"}),
        "refund": (
            refunds,
            {"order_id": ORDER, "action_id": "act_2", "amount_minor": 1, "currency": "USD"},
        ),
        "key": (idempotency_keys, key_row(refund_id)),
    }
    target, row = valid[table]
    # Control: the unmodified row is accepted, so the failure below is the override's.
    assert not refused(runtime, insert(target).values(row), Exception)
    assert refused(runtime, insert(target).values(row | overrides), error)


# --- migrations ---------------------------------------------------------------------------


def copy_service(destination: Path) -> Path:
    for name in ("src", "migrations"):
        shutil.copytree(SERVICE_ROOT / name, destination / name)
    shutil.copy(SERVICE_ROOT / "alembic.ini", destination / "alembic.ini")
    return destination


def test_migrations_match_the_schema(migrated_database: None, tmp_path: Path) -> None:
    result = run_alembic("check", cwd=copy_service(tmp_path))
    assert result.returncode == 0, result.stdout + result.stderr


def test_control_drifted_schema_fails_the_check(migrated_database: None, tmp_path: Path) -> None:
    root = copy_service(tmp_path)
    schema = root / "src/refund_api/db/schema.py"
    source = schema.read_text(encoding="utf-8")
    planted = source.replace(
        '    Column("action_id", Text, nullable=False),\n',
        '    Column("action_id", Text, nullable=False),\n    Column("planted_drift", Text),\n',
    )
    assert planted != source, "plant site not found; update the negative control"
    schema.write_text(planted, encoding="utf-8")
    result = run_alembic("check", cwd=root)
    assert result.returncode != 0
    assert "planted_drift" in result.stdout + result.stderr
