"""lock_charge (migration 0003): a row lock the runtime role can take but not abuse.

Found the hard way: SELECT ... FOR UPDATE needs UPDATE privilege, which charges
deliberately don't grant. A superuser test connection would never have shown it.
"""

import pytest
from conftest import create_charge
from psycopg import errors
from sqlalchemy import Connection, Engine, text, update
from sqlalchemy.exc import DBAPIError

from refund_api.db.schema import charges

LOCK_SIGNATURE = "public.lock_charge(text)"
LOCK = text("SELECT * FROM public.lock_charge(:order_id)")


def test_runtime_cannot_lock_directly(runtime: Connection) -> None:
    # The reason the function exists: FOR UPDATE needs UPDATE privilege.
    with pytest.raises(DBAPIError) as caught:
        runtime.execute(text("SELECT * FROM charges FOR UPDATE"))
    assert isinstance(caught.value.orig, errors.InsufficientPrivilege)


def test_lock_returns_the_charge(runtime_engine: Engine) -> None:
    order = create_charge(runtime_engine, 500)
    with runtime_engine.begin() as connection:
        row = connection.execute(LOCK, {"order_id": order}).one()
    assert (row.order_id, row.amount_minor, row.currency) == (order, 1_000, "USD")


def test_unknown_order_returns_no_row(runtime: Connection) -> None:
    assert runtime.execute(LOCK, {"order_id": "ord_missing"}).one_or_none() is None


def second_lock_waits(runtime_engine: Engine, order: str) -> bool:
    """True if a second transaction can't take the lock within 200 ms."""
    with runtime_engine.connect() as other, other.begin():
        other.execute(text("SET LOCAL lock_timeout = '200ms'"))
        try:
            other.execute(LOCK, {"order_id": order})
        except DBAPIError as exc:
            assert isinstance(exc.orig, errors.LockNotAvailable)
            return True
        return False


def test_lock_is_held_until_the_callers_transaction_ends(runtime_engine: Engine) -> None:
    order = create_charge(runtime_engine, 500)
    with runtime_engine.connect() as holder, holder.begin():
        holder.execute(LOCK, {"order_id": order})
        assert second_lock_waits(runtime_engine, order) is True


def test_control_after_commit_the_lock_is_free(runtime_engine: Engine) -> None:
    order = create_charge(runtime_engine, 500)
    with runtime_engine.connect() as holder, holder.begin():
        holder.execute(LOCK, {"order_id": order})
    assert second_lock_waits(runtime_engine, order) is False


def test_charges_stay_immutable_to_the_runtime_role(runtime: Connection) -> None:
    with pytest.raises(DBAPIError) as caught:
        runtime.execute(update(charges).values(amount_minor=1))
    assert isinstance(caught.value.orig, errors.InsufficientPrivilege)


def test_only_the_app_group_can_execute(owner: Connection) -> None:
    def can_execute(role: str) -> bool:
        allowed: bool = owner.execute(
            text("SELECT has_function_privilege(:role, :signature, 'EXECUTE')"),
            {"role": role, "signature": LOCK_SIGNATURE},
        ).scalar_one()
        return allowed

    assert can_execute("refund_api_runtime") is True
    assert can_execute("agent_runtime") is False


def test_function_is_security_definer_with_a_pinned_search_path(owner: Connection) -> None:
    row = owner.execute(
        text("SELECT prosecdef, proconfig FROM pg_proc WHERE oid = CAST(:sig AS regprocedure)"),
        {"sig": LOCK_SIGNATURE},
    ).one()
    assert row.prosecdef is True
    assert any(setting.startswith("search_path=") for setting in row.proconfig)
