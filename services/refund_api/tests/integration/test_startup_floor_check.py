"""refund-api refuses to start without the key-retention floor (ADR-021 Part 2, S8).

The check runs in the app's lifespan, which in-process test clients don't run, so these tests
start the real thing: uvicorn on a free port, through both builds' factories.
"""

import os
import socket
import subprocess
import sys
import time
from collections.abc import Callable, Iterator
from datetime import timedelta

import httpx
import pytest
from conftest import SERVICE_ROOT, TEST_FLOOR, env, set_floor
from sqlalchemy import Connection, Engine, make_url, text

from refund_api import config

FLOOR_CHECK_SIGNATURE = "public.retention_floor_is_set()"
FLOOR_COMMAND = "python -m refund_api.retention_floor"
TOKEN = "s" * config.MIN_TOKEN_LENGTH
START_BOUND_SECONDS = 20.0
POOL_TIMEOUT_SECONDS = "2"
BUILDS = {
    "production": ("refund_api.app:app_from_env", {config.APP_ENV: "production"}),
    "fault": (
        "refund_api_testing.app:fault_app_from_env",
        {config.APP_ENV: "test", "DEBUG_FAULT_INJECTION": "true"},
    ),
}


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port: int = probe.getsockname()[1]
    return port


def service_env(build: str, database_url: str) -> dict[str, str]:
    environ = {
        name: value
        for name, value in os.environ.items()
        if name not in config.TEST_ONLY_FLAGS and not name.startswith("REFUND_API_")
    }
    return environ | {
        # pytest's pythonpath (pyproject) doesn't reach a subprocess; the fault build lives here.
        "PYTHONPATH": os.pathsep.join(str(SERVICE_ROOT / part) for part in ("src", "testing")),
        config.DATABASE_URL: database_url,
        config.TOKEN_CURRENT: TOKEN,
        config.PROCESSING_BUDGET: "5",
        config.KEY_RETENTION: "86400",
        config.MAX_RETRY_WINDOW: "600",
        config.MAX_RECOVERY_DELAY: "3600",
        config.POOL_TIMEOUT: POOL_TIMEOUT_SECONDS,
        **BUILDS[build][1],
    }


class Started:
    def __init__(self, process: subprocess.Popen[str], port: int) -> None:
        self.process, self.port = process, port

    def wait_for_exit_or_ready(self) -> int | None:
        """The exit code if it exited, or None once it answers GET /config, within a bound."""
        deadline = time.monotonic() + START_BOUND_SECONDS
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                return self.process.returncode
            try:
                response = httpx.get(
                    f"http://127.0.0.1:{self.port}/config",
                    headers={"Authorization": f"Bearer {TOKEN}"},
                    timeout=1.0,
                )
            except httpx.TransportError:
                time.sleep(0.1)
                continue
            assert response.status_code == 200, response.text
            return None
        raise AssertionError(f"refund-api neither started nor exited in {START_BOUND_SECONDS}s")

    def stderr(self) -> str:
        _, err = self.process.communicate(timeout=START_BOUND_SECONDS)
        return err


Start = Callable[..., Started]


@pytest.fixture
def start() -> Iterator[Start]:
    started: list[Started] = []

    def run(build: str, database_url: str | None = None) -> Started:
        port = free_port()
        process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "uvicorn",
                "--factory",
                BUILDS[build][0],
                "--host",
                "127.0.0.1",
                "--port",
                str(port),
            ],
            cwd=SERVICE_ROOT,
            env=service_env(build, database_url or env("REFUND_DB_RUNTIME_URL")),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
        )
        started.append(Started(process, port))
        return started[-1]

    yield run
    for service in started:
        if service.process.poll() is None:
            service.process.terminate()
            try:
                service.process.communicate(timeout=START_BOUND_SECONDS)
            except subprocess.TimeoutExpired:
                service.process.kill()
                service.process.communicate()


@pytest.fixture
def no_floor(owner_engine: Engine) -> Iterator[None]:
    set_floor(owner_engine, None)
    yield
    set_floor(owner_engine, None)


@pytest.mark.usefixtures("no_floor")
@pytest.mark.parametrize("build", sorted(BUILDS))
def test_without_a_floor_refund_api_refuses_to_start(start: Start, build: str) -> None:
    service = start(build)
    assert service.wait_for_exit_or_ready() not in (None, 0)
    err = service.stderr()
    assert FLOOR_COMMAND in err
    assert "key retention floor is not set" in err


@pytest.mark.usefixtures("retention_floor")
@pytest.mark.parametrize("build", sorted(BUILDS))
def test_control_with_the_floor_it_starts(start: Start, build: str) -> None:
    assert start(build).wait_for_exit_or_ready() is None


@pytest.mark.usefixtures("retention_floor")
def test_an_unreachable_database_stops_the_start(start: Start) -> None:
    """S8: its own message, not the floor's, within the startup timeout."""
    closed = (
        make_url(env("REFUND_DB_RUNTIME_URL"))
        .set(host="127.0.0.1", port=free_port())
        .render_as_string(hide_password=False)
    )
    service = start("production", closed)
    assert service.wait_for_exit_or_ready() not in (None, 0)
    err = service.stderr()
    assert "startup check failed" in err
    assert FLOOR_COMMAND not in err


# --- the function itself (migration 0005) --------------------------------------------------


def floor_is_set(connection: Connection) -> bool:
    value: bool = connection.execute(text(f"SELECT {FLOOR_CHECK_SIGNATURE}")).scalar_one()
    return value


def test_the_runtime_role_sees_whether_the_floor_is_set(
    runtime_engine: Engine, owner_engine: Engine
) -> None:
    try:
        for floor, expected in ((None, False), (TEST_FLOOR, True), (timedelta(hours=1), True)):
            set_floor(owner_engine, floor)
            with runtime_engine.connect() as runtime:
                assert floor_is_set(runtime) is expected
    finally:
        set_floor(owner_engine, None)


def can_execute(owner: Connection, role: str) -> bool:
    allowed: bool = owner.execute(
        text("SELECT has_function_privilege(:role, :signature, 'EXECUTE')"),
        {"role": role, "signature": FLOOR_CHECK_SIGNATURE},
    ).scalar_one()
    return allowed


def test_only_the_app_group_can_execute(owner: Connection) -> None:
    # has_function_privilege includes PUBLIC's grants, so a role outside refund_api_app
    # being refused proves the default PUBLIC grant was revoked.
    assert can_execute(owner, "refund_api_runtime") is True
    assert can_execute(owner, "agent_runtime") is False
    assert can_execute(owner, "agent_owner") is False


def test_function_is_security_definer_with_a_pinned_search_path(owner: Connection) -> None:
    row = owner.execute(
        text("SELECT prosecdef, proconfig FROM pg_proc WHERE oid = CAST(:sig AS regprocedure)"),
        {"sig": FLOOR_CHECK_SIGNATURE},
    ).one()
    assert row.prosecdef is True
    assert any(setting.startswith("search_path=") for setting in row.proconfig)
