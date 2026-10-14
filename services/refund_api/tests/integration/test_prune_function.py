"""prune_expired_key and the retention floor (migration 0002, ADR-010 Part 5).

The floor is the bug containment the ADR claims: a caller passing too small a retention
can't get a key deleted before the owner-set floor. Each guarantee has a control showing
the same call does delete once the guarantee no longer applies.
"""

from collections.abc import Iterator
from datetime import timedelta
from typing import Any

import pytest
from psycopg import errors
from sqlalchemy import Connection, Engine, delete, func, insert, select, text, update
from sqlalchemy.exc import DBAPIError

from refund_api.db.schema import charges, idempotency_keys, key_retention_policy, refunds

PRUNE_SIGNATURE = "public.prune_expired_key(text, interval)"
FLOOR = timedelta(hours=1)
KEY = "key_prune"
PRUNE = text("SELECT public.prune_expired_key(:key, :retention)")


def set_floor(owner_engine: Engine, floor: timedelta | None) -> None:
    """Committed, because the runtime connection must see it."""
    with owner_engine.begin() as connection:
        connection.execute(delete(key_retention_policy))
        if floor is not None:
            connection.execute(insert(key_retention_policy).values(id=1, min_retention=floor))


@pytest.fixture
def floor(owner_engine: Engine) -> Iterator[timedelta]:
    set_floor(owner_engine, FLOOR)
    yield FLOOR
    set_floor(owner_engine, None)


def key_aged(runtime: Connection, age: timedelta) -> None:
    """A committed-looking refund plus its key, first seen ``age`` ago."""
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
        )
    )


def prune(runtime: Connection, retention: timedelta) -> bool:
    result: bool = runtime.execute(PRUNE, {"key": KEY, "retention": retention}).scalar_one()
    return result


def key_count(connection: Connection) -> int:
    count: int = connection.execute(
        select(func.count()).select_from(idempotency_keys).where(idempotency_keys.c.key == KEY)
    ).scalar_one()
    return count


def refund_count(connection: Connection) -> int:
    count: int = connection.execute(
        select(func.count()).select_from(refunds).where(refunds.c.action_id == "act_p")
    ).scalar_one()
    return count


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


def prune_error(runtime: Connection, retention: timedelta) -> Any:
    return outcome(runtime, PRUNE, {"key": KEY, "retention": retention})


# --- what the function deletes ------------------------------------------------------------


def test_expired_key_is_pruned_and_the_refund_survives(
    runtime: Connection, floor: timedelta
) -> None:
    key_aged(runtime, floor * 2)
    assert prune(runtime, floor / 2) is True
    assert key_count(runtime) == 0
    assert refund_count(runtime) == 1


def test_floor_overrides_a_too_small_retention(runtime: Connection, floor: timedelta) -> None:
    # A code bug asks for 1 second; the key is 10 minutes old, younger than the floor.
    key_aged(runtime, timedelta(minutes=10))
    assert prune(runtime, timedelta(seconds=1)) is False
    assert key_count(runtime) == 1


def test_control_without_the_floor_the_same_call_deletes(
    runtime: Connection, owner_engine: Engine, floor: timedelta
) -> None:
    # Same key age and same retention as above, floor lowered: now it's deleted, so the
    # floor (not something else) is what kept the key.
    set_floor(owner_engine, timedelta(seconds=1))
    key_aged(runtime, timedelta(minutes=10))
    assert prune(runtime, timedelta(seconds=1)) is True


def test_a_larger_given_retention_is_respected(runtime: Connection, floor: timedelta) -> None:
    key_aged(runtime, floor * 2)
    assert prune(runtime, floor * 3) is False
    assert key_count(runtime) == 1


# --- failing closed -----------------------------------------------------------------------


@pytest.mark.parametrize(
    "retention", [timedelta(0), timedelta(seconds=-5)], ids=["zero", "negative"]
)
def test_non_positive_retention_raises(
    runtime: Connection, floor: timedelta, retention: timedelta
) -> None:
    key_aged(runtime, floor * 2)
    assert isinstance(prune_error(runtime, retention), errors.InvalidParameterValue)


def test_missing_floor_raises_instead_of_pruning(runtime: Connection, owner_engine: Engine) -> None:
    set_floor(owner_engine, None)
    key_aged(runtime, timedelta(days=365))
    assert isinstance(
        prune_error(runtime, timedelta(minutes=1)), errors.ObjectNotInPrerequisiteState
    )
    assert key_count(runtime) == 1


# --- who can do what ----------------------------------------------------------------------

LOWER = timedelta(seconds=1)
# (statement, whether the floor row must exist first for the owner's control to succeed)
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


def test_function_is_security_definer_with_a_pinned_search_path(owner: Connection) -> None:
    row = owner.execute(
        text("SELECT prosecdef, proconfig FROM pg_proc WHERE oid = CAST(:sig AS regprocedure)"),
        {"sig": PRUNE_SIGNATURE},
    ).one()
    assert row.prosecdef is True
    assert any(setting.startswith("search_path=") for setting in row.proconfig)
