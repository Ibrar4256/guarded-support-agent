"""prune_expired_key(key) and per-key expiry (migrations 0002 and 0004, ADR-012 Part 5).

A key is pruned only when its own stamped ``expires_at`` has passed **and** it's older than
the owner-set floor. Each guarantee has a control showing the same call deletes once the
guarantee no longer applies.
"""

from collections.abc import Iterator
from datetime import timedelta
from typing import Any

import pytest
from conftest import set_floor
from psycopg import errors
from sqlalchemy import Connection, Engine, delete, func, insert, select, text, update
from sqlalchemy.exc import DBAPIError

from refund_api.db.schema import charges, idempotency_keys, key_retention_policy, refunds

PRUNE_SIGNATURE = "public.prune_expired_key(text)"
FLOOR = timedelta(hours=1)
KEY = "key_prune"
PRUNE = text("SELECT public.prune_expired_key(:key)")


@pytest.fixture
def floor(owner_engine: Engine) -> Iterator[timedelta]:
    set_floor(owner_engine, FLOOR)
    yield FLOOR
    set_floor(owner_engine, None)


def key_aged(runtime: Connection, age: timedelta, expires_in: timedelta) -> None:
    """A refund plus its key, first seen ``age`` ago, expiring ``expires_in`` from now."""
    runtime.execute(insert(charges).values(order_id="ord_p", amount_minor=10_000, currency="USD"))
    refund_id = runtime.execute(
        insert(refunds)
        .values(order_id="ord_p", action_id="act_p", amount_minor=100, currency="USD")
        .returning(refunds.c.id)
    ).scalar_one()
    runtime.execute(
        insert(idempotency_keys).values(
            key=KEY,
            payload_hash="0" * 64,
            refund_id=refund_id,
            response_status=201,
            response_body="{}",
            created_at=func.now() - age,
            expires_at=func.now() + expires_in,
        )
    )


def prune(runtime: Connection) -> bool:
    result: bool = runtime.execute(PRUNE, {"key": KEY}).scalar_one()
    return result


def count(connection: Connection, table: Any, **where: str) -> int:
    query = select(func.count()).select_from(table)
    for column, value in where.items():
        query = query.where(table.c[column] == value)
    n: int = connection.execute(query).scalar_one()
    return n


def outcome(connection: Connection, statement: Any, params: dict[str, Any] | None = None) -> Any:
    """Run in a savepoint; return the database error, or None if it succeeded."""
    savepoint = connection.begin_nested()
    try:
        connection.execute(statement, params or {})
    except DBAPIError as exc:
        savepoint.rollback()
        return exc.orig
    savepoint.rollback()
    return None


# --- what the function deletes ------------------------------------------------------------


def test_expired_and_past_the_floor_is_pruned_and_the_refund_survives(
    runtime: Connection, floor: timedelta
) -> None:
    key_aged(runtime, age=floor * 2, expires_in=-timedelta(minutes=1))
    assert prune(runtime) is True
    assert count(runtime, idempotency_keys, key=KEY) == 0
    assert count(runtime, refunds, action_id="act_p") == 1


def test_a_key_not_yet_expired_survives_however_old(runtime: Connection, floor: timedelta) -> None:
    # Per-key expiry: age alone never prunes; a longer stamped life is honored.
    key_aged(runtime, age=floor * 10, expires_in=timedelta(minutes=5))
    assert prune(runtime) is False
    assert count(runtime, idempotency_keys, key=KEY) == 1


def test_the_floor_overrides_a_short_stamped_life(runtime: Connection, floor: timedelta) -> None:
    # A bad config or fault-plan override stamped a life shorter than the floor.
    key_aged(runtime, age=timedelta(minutes=10), expires_in=-timedelta(minutes=5))
    assert prune(runtime) is False
    assert count(runtime, idempotency_keys, key=KEY) == 1


def test_control_without_the_floor_the_same_key_is_pruned(
    runtime: Connection, owner_engine: Engine, floor: timedelta
) -> None:
    set_floor(owner_engine, timedelta(seconds=1))
    key_aged(runtime, age=timedelta(minutes=10), expires_in=-timedelta(minutes=5))
    assert prune(runtime) is True


def test_missing_floor_raises_instead_of_pruning(runtime: Connection, owner_engine: Engine) -> None:
    set_floor(owner_engine, None)
    key_aged(runtime, age=timedelta(days=365), expires_in=-timedelta(days=300))
    assert isinstance(outcome(runtime, PRUNE, {"key": KEY}), errors.ObjectNotInPrerequisiteState)
    assert count(runtime, idempotency_keys, key=KEY) == 1


# --- a stamped life can't be changed --------------------------------------------------------


def test_runtime_cannot_change_a_keys_expiry(runtime: Connection, floor: timedelta) -> None:
    key_aged(runtime, age=timedelta(minutes=1), expires_in=timedelta(hours=1))
    statement = update(idempotency_keys).values(expires_at=func.now() + timedelta(days=30))
    assert isinstance(outcome(runtime, statement), errors.InsufficientPrivilege)


def test_control_owner_can_change_a_keys_expiry(owner: Connection) -> None:
    key_aged(owner, age=timedelta(minutes=1), expires_in=timedelta(hours=1))
    statement = update(idempotency_keys).values(expires_at=func.now() + timedelta(days=30))
    assert outcome(owner, statement) is None


def test_expires_at_is_required(runtime: Connection) -> None:
    runtime.execute(insert(charges).values(order_id="ord_n", amount_minor=10_000, currency="USD"))
    refund_id = runtime.execute(
        insert(refunds)
        .values(order_id="ord_n", action_id="act_n", amount_minor=100, currency="USD")
        .returning(refunds.c.id)
    ).scalar_one()
    statement = insert(idempotency_keys).values(
        key="key_n",
        payload_hash="0" * 64,
        refund_id=refund_id,
        response_status=201,
        response_body="{}",
    )
    assert isinstance(outcome(runtime, statement), errors.NotNullViolation)


# --- who can do what ----------------------------------------------------------------------

LOWER = timedelta(seconds=1)
FLOOR_STATEMENTS = {
    "read-floor": (select(key_retention_policy), True),
    "lower-floor": (update(key_retention_policy).values(min_retention=LOWER), True),
    "delete-floor": (delete(key_retention_policy), True),
    "insert-floor": (insert(key_retention_policy).values(id=1, min_retention=LOWER), False),
}


@pytest.mark.parametrize("name", sorted(FLOOR_STATEMENTS))
def test_runtime_cannot_touch_the_floor(runtime: Connection, name: str) -> None:
    statement, _ = FLOOR_STATEMENTS[name]
    assert isinstance(outcome(runtime, statement), errors.InsufficientPrivilege)


@pytest.mark.parametrize("name", sorted(FLOOR_STATEMENTS))
def test_control_owner_can_touch_the_floor(owner: Connection, name: str) -> None:
    statement, needs_row = FLOOR_STATEMENTS[name]
    owner.execute(delete(key_retention_policy))
    if needs_row:
        owner.execute(insert(key_retention_policy).values(id=1, min_retention=FLOOR))
    assert outcome(owner, statement) is None


def can_execute(owner: Connection, role: str) -> bool:
    allowed: bool = owner.execute(
        text("SELECT has_function_privilege(:role, :signature, 'EXECUTE')"),
        {"role": role, "signature": PRUNE_SIGNATURE},
    ).scalar_one()
    return allowed


def test_only_the_app_group_can_execute(owner: Connection) -> None:
    # has_function_privilege includes PUBLIC's grants, so a role outside refund_api_app
    # being refused proves the default PUBLIC grant was revoked.
    assert can_execute(owner, "refund_api_runtime") is True
    assert can_execute(owner, "agent_runtime") is False
    assert can_execute(owner, "agent_owner") is False


def test_the_old_two_argument_function_is_gone(owner: Connection) -> None:
    exists = owner.execute(
        text("SELECT to_regprocedure('public.prune_expired_key(text, interval)') IS NOT NULL")
    ).scalar_one()
    assert exists is False


def test_function_is_security_definer_with_a_pinned_search_path(owner: Connection) -> None:
    row = owner.execute(
        text("SELECT prosecdef, proconfig FROM pg_proc WHERE oid = CAST(:sig AS regprocedure)"),
        {"sig": PRUNE_SIGNATURE},
    ).one()
    assert row.prosecdef is True
    assert any(setting.startswith("search_path=") for setting in row.proconfig)
