"""A fresh agent database per case run (ADR-026 Part 1), cloned the way ADR-015's e2e harness
does it: one migrated template, ``CREATE DATABASE … TEMPLATE … OWNER agent_owner`` as the
Postgres superuser, and ADR-005's database-level grants applied again to every copy, because
database privileges aren't copied from a template. The e2e harness is a separate uv project
that imports nothing from the service, so this is its own small copy, not a shared module.
"""

import os
import subprocess
import uuid
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import psycopg

AGENT = Path(__file__).resolve().parents[3]
TEMPLATE = "agent_eval_template"
PREFIX = "agent_eval_"
MIGRATE_SECONDS = 120


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
        self.drop(TEMPLATE)
        self.execute(
            "postgres",
            f"CREATE DATABASE {TEMPLATE} OWNER agent_owner",
            *self._grants(TEMPLATE),
        )
        self.execute(
            TEMPLATE,
            "REVOKE ALL ON SCHEMA public FROM PUBLIC",
            "GRANT USAGE ON SCHEMA public TO agent_app",
            "ALTER DEFAULT PRIVILEGES FOR ROLE agent_owner IN SCHEMA public "
            "GRANT SELECT, INSERT, UPDATE ON TABLES TO agent_app",
            "ALTER DEFAULT PRIVILEGES FOR ROLE agent_owner IN SCHEMA public "
            "GRANT USAGE, SELECT ON SEQUENCES TO agent_app",
        )
        env = {**os.environ, **environ, "AGENT_DB_OWNER_URL": self.owner(TEMPLATE)}
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
            raise DatabaseSetupError(f"migrating {TEMPLATE} failed:\n{result.stderr}")

    @contextmanager
    def clone(self) -> Iterator[str]:
        """A copy of the template for one case run, always dropped afterwards."""
        name = f"{PREFIX}{uuid.uuid4().hex[:12]}"
        self.execute(
            "postgres",
            f"CREATE DATABASE {name} TEMPLATE {TEMPLATE} OWNER agent_owner",
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

    def clones(self) -> list[str]:
        """Case databases that still exist (a crashed run's leftovers)."""
        with psycopg.connect(self.superuser_url) as conn:
            rows = conn.execute(
                "SELECT datname FROM pg_database WHERE datname LIKE %s AND datname <> %s",
                (PREFIX + "%", TEMPLATE),
            ).fetchall()
        return [name for (name,) in rows]

    @staticmethod
    def _grants(name: str) -> tuple[str, ...]:
        return (
            f"REVOKE CONNECT, TEMPORARY ON DATABASE {name} FROM PUBLIC",
            f"GRANT CONNECT ON DATABASE {name} TO agent_owner, agent_runtime",
        )
