"""The customer lock's mode (ADR-024): limit checks on one customer still exclude each other, but
inserting a row that references the customer (a foreign-key check, ``FOR KEY SHARE``) never waits
on it.
"""

import asyncio
import json
import threading
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import timedelta
from pathlib import Path

import pytest
from agent_testing.fakes import RecordingAlerter
from agent_testing.scripted_llm import ScriptedLLM, Turn
from conftest import env
from sqlalchemy import func, insert, select, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine
from turn_helpers import SETTINGS, turn_deps
from worker_world import World

from agent import seed as seed_module
from agent.core.lifecycle import RunStatus
from agent.db import presend
from agent.db.claims import worker_transaction
from agent.db.schema import conversations, orders, runs
from agent.turn.run import TurnOutcome, run_turn

BOUND = 15
SHORT_WAIT = "300ms"
QUICK = timedelta(seconds=0.3)


@asynccontextmanager
async def limit_check_holding(engine: AsyncEngine, user: str) -> AsyncIterator[AsyncConnection]:
    """A transaction that has taken the customer lock the way every limit check does."""
    async with worker_transaction(
        engine, statement_timeout=timedelta(seconds=5), lock_timeout=timedelta(seconds=2)
    ) as conn:
        await presend.lock_customer(conn, user)
        yield conn


def insert_referencing_rows(world: World) -> None:
    """A conversation, a run and an order for the customer, each with a foreign-key check on it."""
    conversation = uuid.uuid4()
    with world.owner.begin() as connection:
        connection.execute(text(f"SET LOCAL lock_timeout = '{SHORT_WAIT}'"))
        connection.execute(insert(conversations).values(id=conversation, user_id=world.user))
        connection.execute(
            insert(runs).values(
                id=uuid.uuid4(),
                conversation_id=conversation,
                user_id=world.user,
                status=RunStatus.AGENT_STEP,
            )
        )
        connection.execute(
            insert(orders).values(
                id=f"ord_{uuid.uuid4().hex[:12]}",
                owner_user_id=world.user,
                charged_minor=1_000,
                currency="USD",
                status="pending",
            )
        )


async def test_rows_referencing_a_locked_customer_insert_without_waiting(
    world: World, async_runtime_engine: AsyncEngine
) -> None:
    async with limit_check_holding(async_runtime_engine, world.user):
        await asyncio.wait_for(asyncio.to_thread(insert_referencing_rows, world), BOUND)

    with world.owner.connect() as connection:
        made = connection.execute(
            select(func.count()).select_from(runs).where(runs.c.user_id == world.user)
        ).scalar_one()
    assert made == 1


async def test_a_turn_starts_and_answers_while_a_limit_check_holds_the_customer(
    world: World, async_runtime_engine: AsyncEngine
) -> None:
    """A proposal or pre-send in one conversation doesn't stall a new run in another."""
    conversation = world.add_conversation()
    model = ScriptedLLM([Turn(text="Happy to help.")])
    deps = turn_deps(
        async_runtime_engine,
        model,
        alerter=RecordingAlerter(),
        records=[],
        settings=replace(SETTINGS, lock_timeout=QUICK),
    )

    async with limit_check_holding(async_runtime_engine, world.user):
        async with asyncio.timeout(BOUND):
            result = await run_turn(
                deps, conversation_id=conversation, user=world.user, message="hello"
            )

    assert result.outcome is TurnOutcome.REPLIED
    model.assert_finished()


async def test_a_second_limit_check_on_the_same_customer_still_waits(
    world: World, async_runtime_engine: AsyncEngine
) -> None:
    async with limit_check_holding(async_runtime_engine, world.user):
        with pytest.raises(OperationalError, match="lock timeout"):
            async with (
                asyncio.timeout(BOUND),
                worker_transaction(
                    async_runtime_engine,
                    statement_timeout=timedelta(seconds=5),
                    lock_timeout=QUICK,
                ) as other,
            ):
                await presend.lock_customer(other, world.user)


# --- The seed takes the same lock ------------------------------------------------------------


def test_the_seed_locks_customers_with_the_limit_checks_statement() -> None:
    assert vars(seed_module)["customer_lock"] is presend.customer_lock


def test_the_seed_does_not_wait_for_an_insert_in_flight_for_its_customer(
    world: World, tmp_path: Path
) -> None:
    """An uncommitted conversation insert holds ``FOR KEY SHARE`` on the customer; the seed's
    lock must not conflict with it. (The seeded order is the customer's: World's teardown
    deletes it.)"""
    order = f"ord_{uuid.uuid4().hex[:12]}"
    path = tmp_path / "manifest.json"
    item = {
        "order_id": order,
        "owner_user_id": world.user,
        "charged_minor": 5_000,
        "currency": "USD",
        "status": "delivered",
        "delivered_days_ago": 3,
        "chargeback_open": False,
    }
    path.write_text(json.dumps({"manifest_version": 1, "orders": [item]}), encoding="utf-8")
    environ = {"AGENT_DB_OWNER_URL": env("AGENT_DB_OWNER_URL"), "DEMO_SEED_ENABLED": "true"}
    done = threading.Event()

    def run_seed() -> None:
        seed_module.seed(path, environ)
        done.set()

    seeder = threading.Thread(target=run_seed, daemon=True)
    with world.owner.connect() as in_flight:
        pending = in_flight.begin()
        in_flight.execute(insert(conversations).values(id=uuid.uuid4(), user_id=world.user))
        seeder.start()
        waited = not done.wait(5)
        pending.rollback()
    seeder.join(BOUND)

    assert not waited, "the seed waited for a foreign-key check on its customer"
