"""Per-key expiry over HTTP and through migration 0004 (ADR-012 Part 5)."""

import asyncio
from collections.abc import Iterator
from datetime import timedelta

import pytest
from conftest import (
    TOKEN,
    api_client,
    api_settings,
    create_charge,
    refund_rows,
    run_alembic,
    set_floor,
    unique,
)
from sqlalchemy import Connection, Engine, select, text

from refund_api.db.schema import idempotency_keys
from refund_api.service import REPLAYED_HEADER

AMOUNT = 1_000


def body(order_id: str, action_id: str) -> dict[str, object]:
    return {"order_id": order_id, "amount_minor": AMOUNT, "currency": "USD", "action_id": action_id}


def headers(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {TOKEN}", "Idempotency-Key": key}


def expires_at(engine: Engine, key: str) -> object:
    with engine.connect() as connection:
        return connection.execute(
            select(idempotency_keys.c.expires_at).where(idempotency_keys.c.key == key)
        ).scalar_one()


@pytest.mark.usefixtures("retention_floor")
async def test_shrinking_retention_leaves_a_live_keys_life_unchanged(
    runtime_engine: Engine, owner: Connection
) -> None:
    order, action, key = create_charge(runtime_engine, AMOUNT), unique("act"), unique("key")
    async with api_client() as client:  # retention 1 hour
        assert (
            await client.post("/refunds", json=body(order, action), headers=headers(key))
        ).status_code == 201
    stamped = expires_at(runtime_engine, key)
    shrunk = api_settings(
        key_retention=timedelta(seconds=2),
        max_retry_window=timedelta(milliseconds=500),
        max_recovery_delay=timedelta(milliseconds=500),
    )
    await asyncio.sleep(2.5)  # older than the new retention, and than the 1 s test floor
    async with api_client(shrunk) as client:
        replay = await client.post("/refunds", json=body(order, action), headers=headers(key))
    assert replay.status_code == 201 and replay.headers[REPLAYED_HEADER] == "true"
    assert expires_at(runtime_engine, key) == stamped
    assert refund_rows(runtime_engine, action_id=action) == 1
    # Control: the old age-based rule (ADR-010 as first built) would have deleted this key.
    would_delete = owner.execute(
        text(
            "DELETE FROM idempotency_keys "
            "WHERE key = :k AND created_at < now() - interval '2 seconds'"
        ),
        {"k": key},
    ).rowcount
    assert would_delete == 1  # rolled back with the fixture's transaction


@pytest.fixture
def at_revision_0003(owner_engine: Engine) -> Iterator[None]:
    """Run a test against the pre-0004 schema; always return to head afterwards."""
    result = run_alembic("downgrade", "0003")
    assert result.returncode == 0, result.stderr
    try:
        yield
    finally:
        set_floor(owner_engine, timedelta(hours=1))
        result = run_alembic("upgrade", "head")
        set_floor(owner_engine, None)
        assert result.returncode == 0, result.stderr


def insert_old_key(owner_engine: Engine, key: str) -> None:
    """A key as 0003 stored it: no expires_at."""
    order, action = unique("ord"), unique("act")
    with owner_engine.begin() as connection:
        connection.execute(
            text("INSERT INTO charges (order_id, amount_minor, currency) VALUES (:o, 5000, 'USD')"),
            {"o": order},
        )
        refund_id = connection.execute(
            text(
                "INSERT INTO refunds (order_id, action_id, amount_minor, currency) "
                "VALUES (:o, :a, 100, 'USD') RETURNING id"
            ),
            {"o": order, "a": action},
        ).scalar_one()
        connection.execute(
            text(
                "INSERT INTO idempotency_keys (key, payload_hash, refund_id, response_status, "
                "response_body, created_at) "
                "VALUES (:k, :h, :r, 201, '{}', now() - interval '1 day')"
            ),
            {"k": key, "h": "0" * 64, "r": refund_id},
        )


@pytest.mark.usefixtures("at_revision_0003")
def test_migration_backfills_expiry_from_the_floor(owner_engine: Engine) -> None:
    key = unique("key")
    insert_old_key(owner_engine, key)
    set_floor(owner_engine, timedelta(hours=6))
    result = run_alembic("upgrade", "0004")
    assert result.returncode == 0, result.stderr
    with owner_engine.connect() as connection:
        exact = connection.execute(
            text(
                "SELECT expires_at = created_at + interval '6 hours' "
                "FROM idempotency_keys WHERE key = :k"
            ),
            {"k": key},
        ).scalar_one()
    assert exact is True


@pytest.mark.usefixtures("at_revision_0003")
def test_migration_refuses_keys_without_a_floor(owner_engine: Engine) -> None:
    insert_old_key(owner_engine, unique("key"))
    set_floor(owner_engine, None)
    result = run_alembic("upgrade", "0004")
    assert result.returncode != 0
    assert "no key retention floor is set" in result.stderr
