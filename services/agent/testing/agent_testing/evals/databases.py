"""A fresh agent database per case run (ADR-026 Part 1), cloned the way ADR-015's e2e harness
does it: one migrated template, ``CREATE DATABASE … TEMPLATE … OWNER agent_owner`` as the
Postgres superuser, and ADR-005's database-level grants applied again to every copy, because
database privileges aren't copied from a template. The e2e harness is a separate uv project
that imports nothing from the service, so this is its own small copy, not a shared module.

Several runners can share a database server: each has its own id, its own template and case
databases (``agent_eval_<runner>_...``), and holds a session advisory lock on its id for as
long as it runs (``hold``). Postgres releases that lock when the holder's connection ends, a
crash included, so ``drop_abandoned`` drops only the databases of runners whose lock nobody
holds: a crashed run's leftovers, never a live runner's.
"""

import os
import subprocess
import uuid
import zlib
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import psycopg

AGENT = Path(__file__).resolve().parents[3]
PREFIX = "agent_eval_"
MIGRATE_SECONDS = 120
LOCK_CLASS = 2026  # the advisory-lock namespace for eval runners (pg_advisory_lock(int, int))


class DatabaseSetupError(RuntimeError):
    pass


def with_database(url: str, database: str) -> str:
    parts = urlsplit(url)
    return urlunsplit(parts._replace(path=f"/{database}"))


def libpq(url: str) -> str:
    return url.replace("postgresql+psycopg://", "postgresql://", 1)


@dataclass(frozen=True)
class EvalDatabases:
    # Each URL holds a password, so none of them is in the repr (a failing test prints it).
    superuser_url: str = field(repr=False)  # libpq, database "postgres"
    owner_url: str = field(repr=False)  # SQLAlchemy, the agent database
    runtime_url: str = field(repr=False)
    runner: str = field(default_factory=lambda: uuid.uuid4().hex[:7])

    @property
    def template(self) -> str:
        return f"{PREFIX}{self.runner}_template"

    @classmethod
    def from_env(cls, environ: Mapping[str, str]) -> "EvalDatabases":
        names = (
            "POSTGRES_SUPERUSER",
            "POSTGRES_SUPERUSER_PASSWORD",
            "AGENT_DB_OWNER_URL",
            "AGENT_DB_RUNTIME_URL",
        )
        if missing := [name for name in names if not environ.get(name)]:
            raise DatabaseSetupError(f"evals clone databases as the superuser; unset: {missing}")
        owner = urlsplit(environ["AGENT_DB_OWNER_URL"])
        superuser = urlunsplit(
            (
                "postgresql",
                f"{environ['POSTGRES_SUPERUSER']}:{environ['POSTGRES_SUPERUSER_PASSWORD']}"
                f"@{owner.hostname}:{owner.port}",
                "/postgres",
                "",
                "",
            )
        )
        return cls(superuser, environ["AGENT_DB_OWNER_URL"], environ["AGENT_DB_RUNTIME_URL"])

    def execute(self, database: str, *statements: str) -> None:
        with psycopg.connect(with_database(self.superuser_url, database), autocommit=True) as conn:
            for statement in statements:
                conn.execute(statement)

    def drop(self, name: str) -> None:
        self.execute("postgres", f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')

    def create_template(self, environ: Mapping[str, str]) -> None:
        """Set up as ``infra/postgres/init`` sets up ``agent``, then migrated to head as the
        owner role. Rebuilt on every runner start, so it's always the current schema."""
        self.drop(self.template)
        self.execute(
            "postgres",
            f"CREATE DATABASE {self.template} OWNER agent_owner",
            *self._grants(self.template),
        )
        self.execute(
            self.template,
            "REVOKE ALL ON SCHEMA public FROM PUBLIC",
            "GRANT USAGE ON SCHEMA public TO agent_app",
            "ALTER DEFAULT PRIVILEGES FOR ROLE agent_owner IN SCHEMA public "
            "GRANT SELECT, INSERT, UPDATE ON TABLES TO agent_app",
            "ALTER DEFAULT PRIVILEGES FOR ROLE agent_owner IN SCHEMA public "
            "GRANT USAGE, SELECT ON SEQUENCES TO agent_app",
        )
        env = {**os.environ, **environ, "AGENT_DB_OWNER_URL": self.owner(self.template)}
        result = subprocess.run(
            [str(AGENT / ".venv" / "bin" / "alembic"), "upgrade", "head"],
            cwd=AGENT,
            env=env,
            capture_output=True,
            text=True,
            timeout=MIGRATE_SECONDS,
            check=False,
        )
        if result.returncode != 0:
            raise DatabaseSetupError(f"migrating {self.template} failed:\n{result.stderr}")

    @contextmanager
    def clone(self) -> Iterator[str]:
        """A copy of the template for one case run, always dropped afterwards."""
        name = f"{PREFIX}{self.runner}_{uuid.uuid4().hex[:12]}"
        self.execute(
            "postgres",
            f"CREATE DATABASE {name} TEMPLATE {self.template} OWNER agent_owner",
            *self._grants(name),
        )
        try:
            yield name
        finally:
            self.drop(name)

    def owner(self, database: str) -> str:
        return with_database(self.owner_url, database)

    def runtime(self, database: str) -> str:
        return with_database(self.runtime_url, database)

    def eval_databases(self) -> list[str]:
        """Every eval database on the server, any runner's, templates included."""
        with psycopg.connect(self.superuser_url) as conn:
            rows = conn.execute(
                "SELECT datname FROM pg_database WHERE datname LIKE %s ORDER BY 1",
                (PREFIX.replace("_", r"\_") + "%",),
            ).fetchall()
        return [name for (name,) in rows]

    def clones(self) -> list[str]:
        """This runner's case databases that still exist."""
        mine = f"{PREFIX}{self.runner}_"
        return [n for n in self.eval_databases() if n.startswith(mine) and n != self.template]

    @contextmanager
    def hold(self) -> Iterator[None]:
        """Hold this runner's lock while it runs; Postgres drops it if the process dies."""
        with psycopg.connect(self.superuser_url, autocommit=True) as conn:
            conn.execute("SELECT pg_advisory_lock(%s, %s)", (LOCK_CLASS, lock_key(self.runner)))
            yield

    def drop_abandoned(self) -> list[str]:
        """Drop the databases of runners that no longer hold their lock; returns them."""
        by_runner: dict[str, list[str]] = {}
        for name in self.eval_databases():
            runner = name.removeprefix(PREFIX).split("_", 1)[0]
            if runner != self.runner:
                by_runner.setdefault(runner, []).append(name)
        dropped = []
        with psycopg.connect(self.superuser_url, autocommit=True) as conn:
            for runner, names in by_runner.items():
                key = (LOCK_CLASS, lock_key(runner))
                taken = conn.execute("SELECT pg_try_advisory_lock(%s, %s)", key).fetchone()
                if not (taken and taken[0]):
                    continue  # its runner is alive
                try:
                    for name in names:
                        self.drop(name)
                        dropped.append(name)
                finally:
                    conn.execute("SELECT pg_advisory_unlock(%s, %s)", key)
        return dropped

    @staticmethod
    def _grants(name: str) -> tuple[str, ...]:
        return (
            f"REVOKE CONNECT, TEMPORARY ON DATABASE {name} FROM PUBLIC",
            f"GRANT CONNECT ON DATABASE {name} TO agent_owner, agent_runtime",
        )


def lock_key(runner: str) -> int:
    """A runner id (or a legacy name such as the old ``template``) as the lock's second key."""
    return zlib.crc32(runner.encode()) & 0x7FFFFFFF
