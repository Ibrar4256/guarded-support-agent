"""The worker as a real process (ADR-014 Part 1, Part 5, S7): it starts against the real
database with refund-api unreachable (not fatal: the gate stays closed), and SIGTERM stops it
cleanly. The entry points' refusals are checked as processes too. Every wait is bounded."""

import asyncio
import os
import signal
import sys
from collections.abc import AsyncIterator, Mapping

import pytest
from agent_testing.env import example_worker_env
from conftest import SERVICE_ROOT, env

READY = b"running; claim gate"
BOUND = 20.0


def process_env(**overrides: str) -> dict[str, str]:
    base = {
        k: v for k, v in os.environ.items() if k not in ("CRASH_POINT", "DEBUG_FAULT_INJECTION")
    }
    worker = example_worker_env(db_url=env("AGENT_DB_RUNTIME_URL"))
    return {**base, **worker, "PYTHONPATH": "src:testing", **overrides}


async def start(module: str, environ: Mapping[str, str]) -> asyncio.subprocess.Process:
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        module,
        cwd=SERVICE_ROOT,
        env=dict(environ),
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    STARTED.append(process)
    return process


STARTED: list[asyncio.subprocess.Process] = []


@pytest.fixture(autouse=True)
async def no_stray_processes() -> AsyncIterator[None]:
    """A worker that should have refused to start but didn't must not outlive its test: it
    would keep claiming runs in the shared test database."""
    yield
    while STARTED:
        process = STARTED.pop()
        if process.returncode is None:
            process.kill()
            await process.wait()


async def read_until(process: asyncio.subprocess.Process, marker: bytes) -> bytes:
    assert process.stderr is not None
    seen = b""
    while marker not in seen:
        line = await process.stderr.readline()
        if not line:
            raise AssertionError(f"process ended before {marker!r}:\n{seen.decode()}")
        seen += line
    return seen


async def test_the_worker_starts_with_refund_api_down_and_stops_on_sigterm() -> None:
    process = await start("agent.worker", process_env())
    async with asyncio.timeout(BOUND):
        started = await read_until(process, READY)
        assert b"claim gate read_failed" in started, "unreachable refund-api isn't fatal"
        process.send_signal(signal.SIGTERM)
        rest = await read_until(process, b"worker stopped")
        assert await process.wait() == 0, rest.decode()


async def test_the_production_worker_refuses_a_crash_point() -> None:
    process = await start("agent.worker", process_env(CRASH_POINT="before_refund_call"))
    async with asyncio.timeout(BOUND):
        _, stderr = await process.communicate()
    assert process.returncode == 2
    assert b"only python -m agent_testing.worker honors it" in stderr


async def test_the_crash_worker_refuses_production() -> None:
    process = await start(
        "agent_testing.worker",
        process_env(APP_ENV="production", DEBUG_FAULT_INJECTION="true"),
    )
    async with asyncio.timeout(BOUND):
        _, stderr = await process.communicate()
    assert process.returncode == 2
    assert b"never runs with APP_ENV=production" in stderr


async def test_a_missing_setting_exits_with_a_message_not_a_traceback() -> None:
    environ = process_env()
    del environ["POLICY_CURRENCY"]
    process = await start("agent.worker", environ)
    async with asyncio.timeout(BOUND):
        _, stderr = await process.communicate()
    assert process.returncode == 2
    assert b"POLICY_CURRENCY is not set" in stderr and b"Traceback" not in stderr
