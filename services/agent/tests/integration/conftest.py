"""Integration fixtures: a real Postgres from docker-compose (ADR-005: never SQLite).

Needs AGENT_DB_OWNER_URL, AGENT_DB_RUNTIME_URL and REFUND_DB_RUNTIME_URL (see .env.example).
Tests are skipped, not failed, when they are absent, so unit tests run anywhere.
"""

import os
import subprocess
import sys
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import Connection, Engine, create_engine, insert

from agent.db.schema import conversations, customers

SERVICE_ROOT = Path(__file__).resolve().parents[2]
REQUIRED = ("AGENT_DB_OWNER_URL", "AGENT_DB_RUNTIME_URL", "REFUND_DB_RUNTIME_URL")
T0 = datetime(2026, 10, 8, 12, 0, tzinfo=UTC)
USER = "user_alice"


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    missing = [name for name in REQUIRED if not os.environ.get(name)]
    if not missing:
        return
    skip = pytest.mark.skip(reason=f"integration database not configured: {', '.join(missing)}")
    for item in items:
        if "tests/integration" in str(item.path):
            item.add_marker(skip)


def env(name: str) -> str:
    return os.environ[name]


def run_alembic(*args: str) -> subprocess.CompletedProcess[str]:
    alembic = Path(sys.executable).with_name("alembic")
    return subprocess.run(
        [str(alembic), *args],
        cwd=SERVICE_ROOT,
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
    engine = create_engine(env("AGENT_DB_RUNTIME_URL"))
    yield engine
    engine.dispose()


@pytest.fixture(scope="session")
def owner_engine(migrated_database: None) -> Iterator[Engine]:
    engine = create_engine(env("AGENT_DB_OWNER_URL"))
    yield engine
    engine.dispose()


@pytest.fixture
def runtime(runtime_engine: Engine) -> Iterator[Connection]:
    """A runtime-role connection whose work is always rolled back."""
    with runtime_engine.connect() as connection:
        transaction = connection.begin()
        try:
            yield connection
        finally:
            transaction.rollback()


@pytest.fixture
def conversation_id(runtime: Connection) -> uuid.UUID:
    runtime.execute(insert(customers).values(id=USER))
    conversation = uuid.uuid4()
    runtime.execute(insert(conversations).values(id=conversation, user_id=USER))
    return conversation
