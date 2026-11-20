"""Several eval runners on one database server: each owns its databases and an advisory lock,
and a runner's cleanup drops only the databases of runners that no longer hold their lock
(found 2026-11-20: the first cleanup dropped every eval database, a live runner's included)."""

import os
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path

import pytest
from agent_testing.evals.databases import PREFIX, EvalDatabases

AGENT = Path(__file__).resolve().parents[2]
BOUND = 30  # seconds for a child process to get ready or die


@pytest.fixture
def server() -> Iterator[EvalDatabases]:
    if not os.environ.get("AGENT_DB_OWNER_URL"):
        pytest.skip("the agent database settings aren't set")
    if not os.environ.get("POSTGRES_SUPERUSER"):
        pytest.fail("evals clone databases as the superuser: POSTGRES_SUPERUSER isn't set")
    cleaner = EvalDatabases.from_env(os.environ)
    yield cleaner
    cleaner.drop_abandoned()  # whatever a failed test left behind


def runner_database(databases: EvalDatabases, suffix: str = "case") -> str:
    name = f"{PREFIX}{databases.runner}_{suffix}"
    databases.execute("postgres", f"CREATE DATABASE {name}")
    return name


def test_a_live_runners_databases_survive_another_runners_cleanup(
    server: EvalDatabases,
) -> None:
    live = EvalDatabases.from_env(os.environ)
    with live.hold():
        name = runner_database(live)
        dropped = server.drop_abandoned()

        assert name not in dropped
        assert name in server.eval_databases()
    # The runner finished without dropping it (as a crash would): now it's abandoned.
    assert name in server.drop_abandoned()
    assert name not in server.eval_databases()


def test_a_killed_runners_databases_are_dropped_by_the_next_runner(
    server: EvalDatabases,
) -> None:
    """SIGKILL: no cleanup code runs, but Postgres ends the session and with it the lock."""
    child = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import os, sys, time\n"
            "from agent_testing.evals.databases import EvalDatabases, PREFIX\n"
            "d = EvalDatabases.from_env(os.environ)\n"
            "with d.hold():\n"
            "    name = f'{PREFIX}{d.runner}_case'\n"
            "    d.execute('postgres', f'CREATE DATABASE {name}')\n"
            "    print(name, flush=True)\n"
            "    time.sleep(600)\n",
        ],
        cwd=AGENT,
        env={**os.environ, "PYTHONPATH": "testing"},
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert child.stdout is not None
        name = child.stdout.readline().strip()
        assert name.startswith(PREFIX), "the child runner didn't start"
        assert name not in server.drop_abandoned(), "dropped a live runner's database"
    finally:
        child.kill()
        child.wait(timeout=BOUND)

    deadline = time.monotonic() + BOUND
    while name not in (dropped := server.drop_abandoned()):
        assert time.monotonic() < deadline, "the killed runner's lock was never released"
        time.sleep(0.2)
    assert name in dropped and name not in server.eval_databases()


def test_the_old_shared_template_name_counts_as_abandoned(server: EvalDatabases) -> None:
    """``agent_eval_template`` from before runners had ids: no runner holds its lock."""
    server.execute("postgres", f"CREATE DATABASE {PREFIX}template")

    assert f"{PREFIX}template" in server.drop_abandoned()


def test_a_runner_never_drops_its_own_databases_as_abandoned(server: EvalDatabases) -> None:
    with server.hold():
        name = runner_database(server, "mine")
        try:
            assert name not in server.drop_abandoned()
        finally:
            server.drop(name)
