"""Integration fixtures: a real Postgres from docker-compose (ADR-005: never SQLite).

Needs REFUND_DB_OWNER_URL and REFUND_DB_RUNTIME_URL (see .env.example). Outside CI the
tests are skipped when these are absent, so unit tests run anywhere. In CI a missing
variable is an error: a silently skipped integration suite looks exactly like a green one.
"""

import os
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy import Connection, Engine, create_engine

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
