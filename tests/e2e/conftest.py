"""Session and per-test fixtures for the crash tests (ADR-015).

Needs the compose Postgres and its credentials (the same variables ``write_test_env.sh``
writes). Outside CI the suite is skipped when they're missing; in CI that's an error: a
skipped crash suite looks exactly like a passing one.
"""

import asyncio
import json
import os
import secrets
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from profile import REFUND_API as REFUND_API_PROFILE
from profile import REFUND_API_DEPLOY
from profile import WORKER as WORKER_PROFILE
from typing import Any
from urllib.parse import urlsplit

import httpx
import pytest

from harness import (
    AGENT,
    REFUND_API,
    REPO,
    Postgres,
    Process,
    Processes,
    RefundApi,
    free_port,
    libpq,
    run_to_completion,
    venv_python,
    with_database,
)

REQUIRED = (
    "AGENT_DB_OWNER_URL",
    "AGENT_DB_RUNTIME_URL",
    "REFUND_DB_OWNER_URL",
    "REFUND_DB_RUNTIME_URL",
    "POSTGRES_SUPERUSER",
    "POSTGRES_SUPERUSER_PASSWORD",
)
BOUND = 30.0


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    missing = [name for name in REQUIRED if not os.environ.get(name)]
    if not missing:
        return
    if os.environ.get("CI"):
        raise pytest.UsageError(f"CI must run the crash tests; unset: {', '.join(missing)}")
    skip = pytest.mark.skip(reason=f"database not configured: {', '.join(missing)}")
    for item in items:
        item.add_marker(skip)


def base_env() -> dict[str, str]:
    """Only these pass from the runner to a service; a stray CRASH_POINT or
    DEBUG_FAULT_INJECTION in the runner's environment can't change a service's mode."""
    keep = ("PATH", "HOME", "LANG", "LC_ALL", "TMPDIR", "SYSTEMROOT")
    return {name: os.environ[name] for name in keep if name in os.environ}


@pytest.fixture(scope="session")
def postgres() -> Postgres:
    port = urlsplit_port(os.environ["AGENT_DB_OWNER_URL"])
    superuser = (
        f"postgresql://{os.environ['POSTGRES_SUPERUSER']}:"
        f"{os.environ['POSTGRES_SUPERUSER_PASSWORD']}@localhost:{port}/postgres"
    )
    return Postgres(
        superuser_url=superuser,
        agent_owner_url=libpq(os.environ["AGENT_DB_OWNER_URL"]),
        agent_runtime_url=libpq(os.environ["AGENT_DB_RUNTIME_URL"]),
    )


def urlsplit_port(url: str) -> int:
    port = urlsplit(libpq(url)).port
    assert port is not None, "the database URLs name a port"
    return port


@pytest.fixture(scope="session")
async def prepared(postgres: Postgres) -> AsyncIterator[None]:
    """Both services' locked environments, refund-api's database at head, and a fresh
    migrated agent template. Leftover per-test databases are dropped before and after."""
    for service in (AGENT, REFUND_API):
        await run_to_completion(
            ["uv", "sync", "--locked"], env=base_env(), cwd=service, seconds=300
        )
    await run_to_completion(
        [str(REFUND_API / ".venv" / "bin" / "alembic"), "upgrade", "head"],
        env={**base_env(), "REFUND_DB_OWNER_URL": os.environ["REFUND_DB_OWNER_URL"]},
        cwd=REFUND_API,
        seconds=120,
    )
    # refund-api's demo deploy (ADR-021): the key-retention floor, without which refund-api fails
    # closed on every refund (ADR-010 Part 5) exactly as a deploy that skipped it would, then
    # the demo seed's charges.
    for command in (
        ["refund_api.retention_floor"],
        ["refund_api.seed", str(REPO / "infra" / "seed" / "demo_manifest.json")],
    ):
        await run_to_completion(
            [venv_python(REFUND_API), "-m", *command],
            env={
                **base_env(),
                "REFUND_DB_OWNER_URL": os.environ["REFUND_DB_OWNER_URL"],
                **REFUND_API_DEPLOY,
            },
            cwd=REFUND_API,
            seconds=60,
        )
    # The real agent database too: test_harness compares a test database's grants with it.
    await run_to_completion(
        [str(AGENT / ".venv" / "bin" / "alembic"), "upgrade", "head"],
        env={**base_env(), "AGENT_DB_OWNER_URL": os.environ["AGENT_DB_OWNER_URL"]},
        cwd=AGENT,
        seconds=120,
    )
    await postgres.drop_test_databases()
    await postgres.execute("postgres", "DROP DATABASE IF EXISTS agent_e2e_template WITH (FORCE)")
    await postgres.create_template(base_env())
    yield
    await postgres.drop_test_databases()
    await postgres.execute("postgres", "DROP DATABASE IF EXISTS agent_e2e_template WITH (FORCE)")


@pytest.fixture(scope="session")
def token() -> str:
    return secrets.token_urlsafe(32)


@pytest.fixture(scope="session")
async def refund_api(
    prepared: None, token: str, tmp_path_factory: pytest.TempPathFactory
) -> AsyncIterator[RefundApi]:
    """refund-api's fault build, one uvicorn worker (ADR-010 Part 3, ADR-011), shared by every
    test: everything in it is keyed by a unique action, order and key."""
    processes = Processes(tmp_path_factory.mktemp("refund-api"))
    port = free_port()
    env = {
        **base_env(),
        **REFUND_API_PROFILE,
        "REFUND_API_TOKEN_CURRENT": token,
        "REFUND_DB_RUNTIME_URL": os.environ["REFUND_DB_RUNTIME_URL"],
        "PYTHONPATH": "src:testing",
    }
    server = await processes.start(
        "refund-api",
        [
            venv_python(REFUND_API), "-m", "uvicorn", "--factory",
            "refund_api_testing.app:fault_app_from_env",
            "--host", "127.0.0.1", "--port", str(port), "--workers", "1",
        ],
        env=env,
        cwd=REFUND_API,
    )  # fmt: skip
    api = RefundApi(f"http://127.0.0.1:{port}", token)
    try:
        async with asyncio.timeout(BOUND):
            while True:
                try:
                    async with api.client() as http:
                        if (await http.get("/config")).status_code == 200:
                            break
                except httpx.TransportError:
                    pass  # not listening yet
                if server.proc.returncode is not None:
                    raise AssertionError(f"refund-api exited:\n{server.output()}")
                await asyncio.sleep(0.1)
        yield api
    finally:
        await processes.kill_all()


@dataclass
class E2E:
    """One test's world: its own agent database, its processes, the shared refund-api."""

    postgres: Postgres
    database: str
    refund_api: RefundApi
    processes: Processes

    @property
    def runtime_url(self) -> str:
        url = with_database(self.postgres.agent_runtime_url, self.database)
        return url.replace("postgresql://", "postgresql+psycopg://", 1)

    def worker_env(self, crash_point: str = "") -> dict[str, str]:
        return {
            **base_env(),
            **WORKER_PROFILE,
            "AGENT_DB_RUNTIME_URL": self.runtime_url,
            "REFUND_API_URL": self.refund_api.base_url,
            "REFUND_API_TOKEN": self.refund_api.token,
            "CRASH_POINT": crash_point,
            "PYTHONPATH": "src:testing",
        }

    async def start_worker(self, crash_point: str = "") -> Process:
        name = f"worker-{crash_point or 'plain'}"
        return await self.processes.start(
            name,
            [venv_python(AGENT), "-m", "agent_testing.worker"],
            env=self.worker_env(crash_point),
            cwd=AGENT,
        )

    async def make_run(
        self,
        amount_minor: int,
        *,
        charged_minor: int | None = None,
        order_id: str | None = None,
        user_id: str | None = None,
    ) -> dict[str, Any]:
        argv = [
            venv_python(AGENT),
            "-m",
            "agent_testing.make_approved_run",
            "--amount",
            str(amount_minor),
        ]
        if charged_minor is not None:
            argv += ["--charged", str(charged_minor)]
        if order_id is not None:
            argv += ["--order", order_id]
        if user_id is not None:
            argv += ["--user", user_id]
        out = await run_to_completion(argv, env=self.worker_env(), cwd=AGENT, seconds=BOUND)
        made: dict[str, Any] = json.loads(out)
        return made

    async def run_row(self, run_id: str) -> dict[str, Any]:
        rows = await self.postgres.fetch(
            self.database,
            "SELECT status, refund_outcome, send_attempts, worker_id FROM runs WHERE id = %s",
            uuid.UUID(run_id),
        )
        assert len(rows) == 1, f"run {run_id} not found"
        status, outcome, attempts, worker = rows[0]
        return {
            "status": status,
            "outcome": outcome,
            "send_attempts": attempts,
            "worker_id": worker,
        }

    async def events(self, run_id: str) -> list[tuple[str, dict[str, Any]]]:
        rows = await self.postgres.fetch(
            self.database,
            "SELECT kind, payload FROM events WHERE run_id = %s ORDER BY id",
            uuid.UUID(run_id),
        )
        return [(kind, payload) for kind, payload in rows]

    async def until_status(
        self, run_id: str, statuses: set[str], seconds: float = BOUND
    ) -> dict[str, Any]:
        async with asyncio.timeout(seconds):
            while (row := await self.run_row(run_id))["status"] not in statuses:
                await asyncio.sleep(0.1)
        return row


@pytest.fixture
async def e2e(postgres: Postgres, refund_api: RefundApi, tmp_path: Path) -> AsyncIterator[E2E]:
    processes = Processes(tmp_path)
    world = E2E(postgres, await postgres.create_test_database(), refund_api, processes)
    try:
        yield world
    finally:
        print(processes.dump())  # shown only when the test fails
        await processes.kill_all()
