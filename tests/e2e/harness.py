"""The crash-test harness (ADR-015): real processes, real databases, nothing imported from
either service.

- **Processes** start from each service's own venv Python (never under ``uv run``, which
  would sit between a signal and the worker) in their own process group, so a signal reaches
  exactly the process meant and ``killpg`` reaches everything it started. ``Processes``
  kills every group it started when the test ends, whatever happened.
- **Databases:** a migrated template, and a copy per test, so a worker can only ever claim its
  own test's runs. Database-level grants aren't copied from a template, so each copy gets
  ADR-005's ``REVOKE``/``GRANT CONNECT`` again.
"""

import asyncio
import contextlib
import json
import os
import signal
import socket
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import httpx
import psycopg

REPO = Path(__file__).resolve().parents[2]
AGENT = REPO / "services" / "agent"
REFUND_API = REPO / "services" / "refund_api"
TEMPLATE = "agent_e2e_template"
TEST_DB_PREFIX = "agent_e2e_"


def venv_python(service: Path) -> str:
    return str(service / ".venv" / "bin" / "python")


def libpq(url: str) -> str:
    """SQLAlchemy URLs (``postgresql+psycopg://``) to plain libpq ones."""
    return url.replace("postgresql+psycopg://", "postgresql://", 1)


def with_database(url: str, database: str) -> str:
    parts = urlsplit(url)
    return urlunsplit(parts._replace(path=f"/{database}"))


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


# --- processes -----------------------------------------------------------------------------------


def process_state(pid: int) -> str:
    """The one-letter state from /proc (``T`` = stopped). Linux only, like CI."""
    stat = Path(f"/proc/{pid}/stat").read_text()
    return stat[stat.rindex(")") + 2]


@dataclass
class Process:
    name: str
    proc: asyncio.subprocess.Process
    log: Path

    @property
    def pid(self) -> int:
        return self.proc.pid

    def signal_group(self, sig: signal.Signals) -> None:
        os.killpg(self.pid, sig)

    async def stop(self) -> None:
        """SIGSTOP, then prove it: a stall test must not pass with a running worker."""
        self.signal_group(signal.SIGSTOP)
        async with asyncio.timeout(5):
            while process_state(self.pid) != "T":
                await asyncio.sleep(0.01)

    async def resume(self) -> None:
        self.signal_group(signal.SIGCONT)
        async with asyncio.timeout(5):
            while process_state(self.pid) == "T":
                await asyncio.sleep(0.01)

    async def wait(self, seconds: float) -> int:
        async with asyncio.timeout(seconds):
            return await self.proc.wait()

    async def terminate(self, seconds: float = 10) -> int:
        if self.proc.returncode is None:
            self.signal_group(signal.SIGTERM)
        return await self.wait(seconds)

    def output(self) -> str:
        return self.log.read_text(errors="replace") if self.log.exists() else ""

    async def wait_for_output(self, marker: str, seconds: float) -> str:
        async with asyncio.timeout(seconds):
            while marker not in (text := self.output()):
                if self.proc.returncode is not None:
                    raise AssertionError(f"{self.name} exited before {marker!r}:\n{text}")
                await asyncio.sleep(0.05)
        return text


@dataclass
class Processes:
    logs: Path
    started: list[Process] = field(default_factory=list)

    async def start(
        self, name: str, argv: Sequence[str], *, env: Mapping[str, str], cwd: Path
    ) -> Process:
        log = self.logs / f"{name}-{len(self.started)}.log"
        with log.open("wb") as sink:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                cwd=cwd,
                env=dict(env),
                stdin=asyncio.subprocess.DEVNULL,
                stdout=sink,
                stderr=sink,
                start_new_session=True,
            )
        started = Process(name, proc, log)
        self.started.append(started)
        return started

    async def kill_all(self) -> None:
        while self.started:
            process = self.started.pop()
            if process.proc.returncode is None:
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(process.pid, signal.SIGKILL)
                await process.proc.wait()

    def dump(self) -> str:
        return "\n".join(f"--- {p.name} ({p.log.name})\n{p.output()[-4000:]}" for p in self.started)


async def run_to_completion(
    argv: Sequence[str], *, env: Mapping[str, str], cwd: Path, seconds: float
) -> str:
    proc = await asyncio.create_subprocess_exec(
        *argv,
        cwd=cwd,
        env=dict(env),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        start_new_session=True,
    )
    try:
        async with asyncio.timeout(seconds):
            out, err = await proc.communicate()
    finally:
        if proc.returncode is None:
            os.killpg(proc.pid, signal.SIGKILL)
            await proc.wait()
    if proc.returncode != 0:
        raise AssertionError(f"{' '.join(argv)} exited {proc.returncode}:\n{err.decode()}")
    return out.decode()


# --- databases -----------------------------------------------------------------------------------


@dataclass(frozen=True)
class Postgres:
    superuser_url: str  # libpq, database "postgres"
    agent_owner_url: str  # libpq, database "agent"
    agent_runtime_url: str

    def superuser(self, database: str = "postgres") -> str:
        return with_database(self.superuser_url, database)

    async def execute(self, database: str, *statements: str) -> None:
        async with await psycopg.AsyncConnection.connect(
            self.superuser(database), autocommit=True
        ) as conn:
            for statement in statements:
                await conn.execute(statement)

    async def drop_test_databases(self) -> None:
        async with await psycopg.AsyncConnection.connect(self.superuser(), autocommit=True) as conn:
            rows = await (
                await conn.execute(
                    "SELECT datname FROM pg_database WHERE datname LIKE %s",
                    (TEST_DB_PREFIX + "%",),
                )
            ).fetchall()
            for (name,) in rows:
                await conn.execute(f'DROP DATABASE "{name}" WITH (FORCE)')

    async def create_template(self, alembic_env: Mapping[str, str]) -> None:
        """A database set up as ``infra/postgres/init`` sets up ``agent``, then migrated to
        head as the agent's owner role."""
        await self.execute(
            "postgres",
            f"CREATE DATABASE {TEMPLATE} OWNER agent_owner",
            f"REVOKE CONNECT, TEMPORARY ON DATABASE {TEMPLATE} FROM PUBLIC",
            f"GRANT CONNECT ON DATABASE {TEMPLATE} TO agent_owner, agent_runtime",
        )
        await self.execute(
            TEMPLATE,
            "REVOKE ALL ON SCHEMA public FROM PUBLIC",
            "GRANT USAGE ON SCHEMA public TO agent_app",
            "ALTER DEFAULT PRIVILEGES FOR ROLE agent_owner IN SCHEMA public "
            "GRANT SELECT, INSERT, UPDATE ON TABLES TO agent_app",
            "ALTER DEFAULT PRIVILEGES FOR ROLE agent_owner IN SCHEMA public "
            "GRANT USAGE, SELECT ON SEQUENCES TO agent_app",
        )
        owner = with_database(self.agent_owner_url, TEMPLATE).replace(
            "postgresql://", "postgresql+psycopg://", 1
        )
        await run_to_completion(
            [str(AGENT / ".venv" / "bin" / "alembic"), "upgrade", "head"],
            env={**alembic_env, "AGENT_DB_OWNER_URL": owner},
            cwd=AGENT,
            seconds=120,
        )

    async def create_test_database(self, *, grants: bool = True) -> str:
        name = f"{TEST_DB_PREFIX}{uuid.uuid4().hex[:12]}"
        statements = [f"CREATE DATABASE {name} TEMPLATE {TEMPLATE} OWNER agent_owner"]
        if grants:
            statements += [
                f"REVOKE CONNECT, TEMPORARY ON DATABASE {name} FROM PUBLIC",
                f"GRANT CONNECT ON DATABASE {name} TO agent_owner, agent_runtime",
            ]
        await self.execute("postgres", *statements)
        return name

    async def fetch(self, database: str, query: str, *params: Any) -> list[tuple[Any, ...]]:
        async with await psycopg.AsyncConnection.connect(self.superuser(database)) as conn:
            cursor = await conn.execute(query, params)
            return await cursor.fetchall()


# --- refund-api ----------------------------------------------------------------------------------


@dataclass
class RefundApi:
    base_url: str
    token: str

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            base_url=self.base_url,
            headers={"Authorization": f"Bearer {self.token}"},
            timeout=10,
        )

    async def create_charge(self, order_id: str, amount_minor: int) -> None:
        async with self.client() as http:
            response = await http.post(
                "/_test/charges",
                json={"order_id": order_id, "amount_minor": amount_minor, "currency": "USD"},
            )
        assert response.status_code == 201, response.text

    async def plan(self, action_id: str, mode: str, seconds: float | None = None) -> None:
        body: dict[str, Any] = {"action_id": action_id, "mode": mode}
        if seconds is not None:
            body["seconds"] = seconds
        async with self.client() as http:
            response = await http.post("/_test/faults", json=body)
        assert response.status_code == 201, response.text

    async def applied(self, action_id: str) -> list[str]:
        async with self.client() as http:
            response = await http.get(f"/_test/faults/{action_id}")
        assert response.status_code == 200, response.text
        return list(response.json()["applied"])

    async def refunds(self, order_id: str, action_id: str) -> list[dict[str, Any]]:
        """refund-api's own record, through its reconciliation endpoint."""
        async with self.client() as http:
            response = await http.get(
                "/refunds", params={"order_id": order_id, "action_id": action_id}
            )
        assert response.status_code == 200, response.text
        refunds: list[dict[str, Any]] = json.loads(response.text)["refunds"]
        return refunds
