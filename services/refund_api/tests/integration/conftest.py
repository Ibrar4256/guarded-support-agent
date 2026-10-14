"""Integration fixtures: a real Postgres from docker-compose (ADR-005: never SQLite).

Needs REFUND_DB_OWNER_URL and REFUND_DB_RUNTIME_URL (see .env.example). Outside CI the
tests are skipped when these are absent, so unit tests run anywhere. In CI a missing
variable is an error: a silently skipped integration suite looks exactly like a green one.
"""

import os
import secrets
import subprocess
import sys
import uuid
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import Connection, Engine, create_engine, delete, func, insert, select

from refund_api.app import create_app
from refund_api.config import Settings
from refund_api.db.schema import charges, key_retention_policy, refunds
from refund_api.hooks import RefundHooks

SERVICE_ROOT = Path(__file__).resolve().parents[2]
REQUIRED = ("REFUND_DB_OWNER_URL", "REFUND_DB_RUNTIME_URL")


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    missing = [name for name in REQUIRED if not os.environ.get(name)]
    if not missing:
        return
    if os.environ.get("CI"):
        raise pytest.UsageError(f"CI must run the integration tests; unset: {', '.join(missing)}")
    skip = pytest.mark.skip(reason=f"integration database not configured: {', '.join(missing)}")
    for item in items:
        if "tests/integration" in str(item.path):
            item.add_marker(skip)


def env(name: str) -> str:
    return os.environ[name]


def run_alembic(*args: str, cwd: Path = SERVICE_ROOT) -> subprocess.CompletedProcess[str]:
    alembic = Path(sys.executable).with_name("alembic")
    return subprocess.run(
        [str(alembic), *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
    )


@pytest.fixture(scope="session")
def migrated_database() -> None:
    """Start every session from an empty schema migrated to head."""
    for args in (("downgrade", "base"), ("upgrade", "head")):
        result = run_alembic(*args)
        assert result.returncode == 0, result.stderr


@pytest.fixture(scope="session")
def runtime_engine(migrated_database: None) -> Iterator[Engine]:
    engine = create_engine(env("REFUND_DB_RUNTIME_URL"))
    yield engine
    engine.dispose()


@pytest.fixture(scope="session")
def owner_engine(migrated_database: None) -> Iterator[Engine]:
    engine = create_engine(env("REFUND_DB_OWNER_URL"))
    yield engine
    engine.dispose()


def rolled_back(engine: Engine) -> Iterator[Connection]:
    with engine.connect() as connection:
        transaction = connection.begin()
        try:
            yield connection
        finally:
            transaction.rollback()


@pytest.fixture
def runtime(runtime_engine: Engine) -> Iterator[Connection]:
    """A runtime-role connection whose work is always rolled back."""
    yield from rolled_back(runtime_engine)


@pytest.fixture
def owner(owner_engine: Engine) -> Iterator[Connection]:
    """An owner-role connection whose work is always rolled back."""
    yield from rolled_back(owner_engine)


# --- shared helpers for the database and the HTTP API --------------------------------------

TOKEN = secrets.token_urlsafe(32)
NEXT_TOKEN = secrets.token_urlsafe(32)
TEST_FLOOR = timedelta(seconds=1)


def set_floor(owner_engine: Engine, floor: timedelta | None) -> None:
    """Committed, because the service's own connections must see it."""
    with owner_engine.begin() as connection:
        connection.execute(delete(key_retention_policy))
        if floor is not None:
            connection.execute(insert(key_retention_policy).values(id=1, min_retention=floor))


@pytest.fixture
def retention_floor(owner_engine: Engine) -> Iterator[timedelta]:
    """The owner-set floor (ADR-010 Part 5); pruning fails closed without it."""
    set_floor(owner_engine, TEST_FLOOR)
    yield TEST_FLOOR
    set_floor(owner_engine, None)


def api_settings(**overrides: Any) -> Settings:
    base: dict[str, Any] = {
        "database_url": env("REFUND_DB_RUNTIME_URL"),
        "tokens": (TOKEN, NEXT_TOKEN),
        "processing_budget": timedelta(seconds=5),
        "key_retention": timedelta(hours=1),
        "max_retry_window": timedelta(minutes=1),
        "max_recovery_delay": timedelta(minutes=10),
        "pool_timeout": timedelta(seconds=2),
        "app_env": "test",
    }
    return Settings(**(base | overrides))


@asynccontextmanager
async def api_client(
    settings: Settings | None = None, hooks: RefundHooks | None = None
) -> AsyncIterator[AsyncClient]:
    """The app in-process. Logic only: no real sockets, so no real hung connections."""
    app = create_app(settings or api_settings(), hooks)
    transport = ASGITransport(app=app)
    try:
        async with AsyncClient(transport=transport, base_url="http://refund-api") as client:
            yield client
    finally:
        await app.state.engine.dispose()


def unique(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:16]}"


def create_charge(
    engine: Engine,
    refund_amount_minor: int,
    *,
    charge_amount_minor: int | None = None,
    currency: str = "USD",
) -> str:
    """A committed charge. Defaults to twice the refund (ADR-004 test-data rule), so a
    duplicate refund in a negative control can't be hidden by refund_exceeds_charge."""
    order_id = unique("ord")
    amount = charge_amount_minor if charge_amount_minor is not None else 2 * refund_amount_minor
    with engine.begin() as connection:
        connection.execute(
            insert(charges).values(order_id=order_id, amount_minor=amount, currency=currency)
        )
    return order_id


def refund_rows(engine: Engine, **where: str) -> int:
    query = select(func.count()).select_from(refunds)
    for column, value in where.items():
        query = query.where(refunds.c[column] == value)
    with engine.connect() as connection:
        count: int = connection.execute(query).scalar_one()
    return count
