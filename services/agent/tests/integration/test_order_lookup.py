"""The order-lookup tool against a real Postgres, as the runtime role (ADR-020's test table).

PROJECT_BRIEF requirement 2 and EVAL_PLAN category 4: a missing order and another customer's
order get byte-for-byte identical results, through the same queries; only the server-side
security log tells them apart.
"""

import asyncio
import json
import logging
import uuid
from collections.abc import AsyncIterator, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Any

import pytest
from conftest import env
from sqlalchemy import Engine, event, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine
from worker_world import World

from agent.core.lifecycle import RunStatus
from agent.core.policy import FulfillmentStatus
from agent.db import order_lookup as order_lookup_db
from agent.tools import order_lookup
from agent.tools.contract import LostTurn, ToolContext
from agent.tools.order_lookup import NOT_FOUND, UNAVAILABLE, OrderLookupArgs, lookup_order

SECURITY_LOGGER = "agent.security"
NEVER_MINTED = "ord_never_minted_0"
TAKEOVER_LOCK_TIMEOUT = "200ms"
BOUND_SECONDS = 10


@pytest.fixture
def stranger(owner_engine: Engine) -> Iterator[World]:
    """Another customer. Request it before ``world`` so it's torn down after it."""
    stranger = World(owner_engine)
    try:
        yield stranger
    finally:
        stranger.teardown()


@pytest.fixture
async def conn(async_runtime_engine: AsyncEngine) -> AsyncIterator[AsyncConnection]:
    async with async_runtime_engine.connect() as connection:
        yield connection


@pytest.fixture
def turn(world: World) -> tuple[uuid.UUID, uuid.UUID]:
    """A run in AGENT_STEP whose conversation lease this turn holds."""
    run = world.add_run(RunStatus.AGENT_STEP)
    return run, world.hold_turn(run)


def context(world: World, turn: tuple[uuid.UUID, uuid.UUID], conn: AsyncConnection) -> ToolContext:
    run, turn_id = turn
    conversation = world.row(run).conversation_id
    return ToolContext(
        session_user=world.user,
        conversation_id=conversation,
        turn_id=turn_id,
        run_id=run,
        conn=conn,
    )


async def lookup(ctx: ToolContext, order_id: str) -> str:
    return await lookup_order(ctx, OrderLookupArgs(order_id=order_id))


@contextmanager
def recorded_statements(conn: AsyncConnection) -> Iterator[list[str]]:
    statements: list[str] = []

    def record(*args: Any) -> None:
        statements.append(args[2])  # (conn, cursor, statement, parameters, context, many)

    target = conn.sync_connection
    assert target is not None
    event.listen(target, "before_cursor_execute", record)
    try:
        yield statements
    finally:
        event.remove(target, "before_cursor_execute", record)


def security_lines(caplog: pytest.LogCaptureFixture) -> list[dict[str, Any]]:
    return [
        json.loads(record.getMessage())
        for record in caplog.records
        if record.name == SECURITY_LOGGER and record.getMessage().startswith("{")
    ]


# --- Uniform not-found -------------------------------------------------------------------


async def test_missing_and_foreign_orders_get_byte_identical_results(
    stranger: World, world: World, turn: tuple[uuid.UUID, uuid.UUID], conn: AsyncConnection
) -> None:
    foreign = stranger.add_order()
    ctx = context(world, turn, conn)

    missing_result = await lookup(ctx, NEVER_MINTED)
    foreign_result = await lookup(ctx, foreign)

    assert foreign_result.encode() == missing_result.encode() == NOT_FOUND.encode()


async def test_both_not_found_cases_run_the_same_queries(
    stranger: World, world: World, turn: tuple[uuid.UUID, uuid.UUID], conn: AsyncConnection
) -> None:
    foreign = stranger.add_order()
    ctx = context(world, turn, conn)

    with recorded_statements(conn) as missing_statements:
        await lookup(ctx, NEVER_MINTED)
    with recorded_statements(conn) as foreign_statements:
        await lookup(ctx, foreign)

    assert len(missing_statements) == 2, missing_statements  # the answering query, the probe
    assert foreign_statements == missing_statements


async def test_security_log_has_one_line_for_a_foreign_order_and_none_for_a_missing_one(
    stranger: World,
    world: World,
    turn: tuple[uuid.UUID, uuid.UUID],
    conn: AsyncConnection,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.WARNING, logger=SECURITY_LOGGER)
    foreign = stranger.add_order()
    ctx = context(world, turn, conn)

    missing_result = await lookup(ctx, NEVER_MINTED)
    assert security_lines(caplog) == []
    foreign_result = await lookup(ctx, foreign)

    assert security_lines(caplog) == [
        {
            "kind": "cross_user_order_lookup",
            "order_id": foreign,
            "run_id": str(ctx.run_id),
            "session_user": world.user,
        }
    ]
    for result in (missing_result, foreign_result):
        assert "cross_user" not in result
        assert stranger.user not in result


async def test_another_customers_order_is_never_returned(
    stranger: World, world: World, turn: tuple[uuid.UUID, uuid.UUID], conn: AsyncConnection
) -> None:
    foreign = stranger.add_order(charged_minor=12_345)

    result = await lookup(context(world, turn, conn), foreign)

    assert result == NOT_FOUND
    assert foreign not in result
    assert "12345" not in result


@pytest.mark.parametrize(
    "order_id",
    ["", "ord demo 1001", "ord_1001\n", "ord_\x00_1001", "ord_1001;--", "o" * 129, "ördér_1"],
)
async def test_a_malformed_id_gets_the_same_not_found_without_touching_the_database(
    world: World, turn: tuple[uuid.UUID, uuid.UUID], conn: AsyncConnection, order_id: str
) -> None:
    ctx = context(world, turn, conn)

    with recorded_statements(conn) as statements:
        result = await lookup(ctx, order_id)

    assert result == NOT_FOUND
    assert statements == []


# --- A found order and R02 -----------------------------------------------------------------


async def test_a_found_order_returns_its_own_facts_and_is_recorded_once(
    world: World, turn: tuple[uuid.UUID, uuid.UUID], conn: AsyncConnection
) -> None:
    delivered = datetime(2026, 11, 10, 15, 30, tzinfo=UTC)
    order = world.add_order(charged_minor=5_000, delivered_at=delivered)
    ctx = context(world, turn, conn)

    first = await lookup(ctx, order)
    second = await lookup(ctx, order)

    assert first == second
    assert json.loads(first) == {
        "order_id": order,
        "status": "delivered",
        "charged_minor": 5_000,
        "currency": "USD",
        "charged_display": "50.00 USD",
        "delivered_on": "2026-11-10",
    }
    assert world.lookups(ctx.run_id) == [order]


async def test_an_undelivered_order_has_no_delivery_date(
    world: World, turn: tuple[uuid.UUID, uuid.UUID], conn: AsyncConnection
) -> None:
    order = world.add_order(status=FulfillmentStatus.SHIPPED)

    result = json.loads(await lookup(context(world, turn, conn), order))

    assert result["status"] == "shipped"
    assert "delivered_on" not in result


async def test_a_not_found_records_nothing(
    stranger: World, world: World, turn: tuple[uuid.UUID, uuid.UUID], conn: AsyncConnection
) -> None:
    foreign = stranger.add_order()
    ctx = context(world, turn, conn)

    await lookup(ctx, NEVER_MINTED)
    await lookup(ctx, foreign)

    assert world.lookups(ctx.run_id) == []


async def test_the_tool_runs_as_the_runtime_role(
    world: World, turn: tuple[uuid.UUID, uuid.UUID], conn: AsyncConnection
) -> None:
    order = world.add_order()

    await lookup(context(world, turn, conn), order)  # a missing grant would be a tool error

    assert (await conn.scalar(text("SELECT current_user"))) == make_url(
        env("AGENT_DB_RUNTIME_URL")
    ).username
    await conn.rollback()
    assert world.lookups(turn[0]) == [order]


# --- Display amounts and dates ---------------------------------------------------------------


async def test_charged_display_uses_the_currency_exponent(
    world: World, turn: tuple[uuid.UUID, uuid.UUID], conn: AsyncConnection
) -> None:
    yen = world.add_order(charged_minor=5_000, currency="JPY")

    result = json.loads(await lookup(context(world, turn, conn), yen))

    assert result["charged_display"] == "5000 JPY"
    assert result["charged_minor"] == 5_000


async def test_an_unknown_currency_is_the_generic_error_and_records_nothing(
    world: World,
    turn: tuple[uuid.UUID, uuid.UUID],
    conn: AsyncConnection,
    caplog: pytest.LogCaptureFixture,
) -> None:
    order = world.add_order(currency="XTS")  # ISO 4217's testing code: never in the table
    ctx = context(world, turn, conn)

    result = await lookup(ctx, order)

    assert result == UNAVAILABLE
    assert world.lookups(ctx.run_id) == []
    assert any(r.name == "agent.tools.order_lookup" and r.exc_info for r in caplog.records)


async def test_the_delivery_date_is_the_utc_date_whatever_the_session_time_zone(
    world: World, turn: tuple[uuid.UUID, uuid.UUID], conn: AsyncConnection
) -> None:
    # 02:30 UTC on the 10th is still the 9th in New York.
    order = world.add_order(delivered_at=datetime(2026, 11, 10, 2, 30, tzinfo=UTC))
    await conn.execute(text("SET TIME ZONE 'America/New_York'"))
    await conn.commit()

    result = json.loads(await lookup(context(world, turn, conn), order))

    assert result["delivered_on"] == "2026-11-10"


# --- Failures --------------------------------------------------------------------------------


async def test_a_failing_probe_leaves_the_not_found_bytes_unchanged_and_is_logged(
    stranger: World,
    world: World,
    turn: tuple[uuid.UUID, uuid.UUID],
    conn: AsyncConnection,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    caplog.set_level(logging.WARNING, logger=SECURITY_LOGGER)
    foreign = stranger.add_order()
    ctx = context(world, turn, conn)

    async def dropped_connection(*_: object) -> bool:
        raise OperationalError("SELECT EXISTS ...", {}, Exception("server closed the connection"))

    monkeypatch.setattr(order_lookup, "order_exists", dropped_connection)

    for order_id in (NEVER_MINTED, foreign):
        assert (await lookup(ctx, order_id)).encode() == NOT_FOUND.encode()

    failures = [r for r in caplog.records if r.name == SECURITY_LOGGER and r.exc_info]
    assert len(failures) == 2
    assert security_lines(caplog) == []


async def test_a_failing_answering_query_is_one_generic_error_never_not_found(
    stranger: World, world: World, turn: tuple[uuid.UUID, uuid.UUID], conn: AsyncConnection
) -> None:
    foreign, mine = stranger.add_order(), world.add_order()
    ctx = context(world, turn, conn)
    await conn.execute(text("SET lock_timeout = '100ms'"))
    await conn.commit()

    with world.transaction() as owner:
        owner.execute(text("LOCK TABLE orders IN ACCESS EXCLUSIVE MODE"))
        results = [await lookup(ctx, order_id) for order_id in (NEVER_MINTED, foreign, mine)]

    assert {result.encode() for result in results} == {UNAVAILABLE.encode()}
    assert UNAVAILABLE != NOT_FOUND
    assert world.lookups(ctx.run_id) == []


# --- The turn lease fence (S7) -----------------------------------------------------------------


async def test_a_turn_that_lost_its_lease_records_nothing_and_raises(
    world: World, turn: tuple[uuid.UUID, uuid.UUID], conn: AsyncConnection
) -> None:
    order = world.add_order()
    stale = context(world, turn, conn)
    current_turn = world.hold_turn(stale.run_id)  # another turn took the conversation over

    with pytest.raises(LostTurn):
        await lookup(stale, order)
    assert world.lookups(stale.run_id) == []

    current = context(world, (stale.run_id, current_turn), conn)
    await lookup(current, order)
    assert world.lookups(stale.run_id) == [order]


def take_over(owner: Engine, run: uuid.UUID) -> None:
    """ADR-008's turn start for the run's conversation, as another request (lease ignored)."""
    with owner.begin() as connection:
        connection.execute(text(f"SET LOCAL lock_timeout = '{TAKEOVER_LOCK_TIMEOUT}'"))
        connection.execute(
            text(
                "UPDATE conversations SET active_turn_id = :turn,"
                " turn_expires_at = now() + interval '5 minutes'"
                " WHERE id = (SELECT conversation_id FROM runs WHERE id = :run)"
            ),
            {"turn": uuid.uuid4(), "run": run},
        )


async def test_a_takeover_waits_until_the_fenced_lookup_is_recorded(
    world: World,
    turn: tuple[uuid.UUID, uuid.UUID],
    conn: AsyncConnection,
    owner_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    order = world.add_order()
    ctx = context(world, turn, conn)
    record = order_lookup_db.record_lookup
    blocked: list[OperationalError] = []

    async def takeover_between_fence_and_insert(
        connection: AsyncConnection, run: uuid.UUID, order_id: str
    ) -> None:
        try:
            await asyncio.to_thread(take_over, owner_engine, run)
        except OperationalError as exc:
            blocked.append(exc)
        await record(connection, run, order_id)

    monkeypatch.setattr(order_lookup, "record_lookup", takeover_between_fence_and_insert)
    async with asyncio.timeout(BOUND_SECONDS):
        await lookup(ctx, order)

    assert len(blocked) == 1, "the takeover committed while the fenced insert was pending"
    assert "lock timeout" in str(blocked[0])
    assert world.lookups(ctx.run_id) == [order]
