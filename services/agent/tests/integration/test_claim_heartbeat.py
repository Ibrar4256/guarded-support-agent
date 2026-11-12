"""The heartbeat and liveness (ADR-004 step 7, ADR-014 S8, S11) with real, short timings.

Every wait is bounded: a test that would hang fails at its ``asyncio.timeout`` instead.
"""

import asyncio
from collections.abc import Callable, Coroutine
from datetime import timedelta
from typing import Any

import pytest
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine
from worker_world import World

from agent.core.refund_outcome import RetryPolicy
from agent.db import claims
from agent.db.claims import Claim
from agent.worker import lease
from agent.worker.lease import LostClaim, StepClock, StepOverrun, hold_claim
from agent.worker.timings import WorkerTimings

FAST = WorkerTimings(
    lease=timedelta(seconds=0.4),
    heartbeat=timedelta(seconds=0.1),
    liveness_margin=timedelta(seconds=0.05),
    claim_cooldown=timedelta(seconds=1),
    statement_timeout=timedelta(seconds=0.3),
    lock_timeout=timedelta(seconds=0.1),
    policy=RetryPolicy(
        attempts=1,
        backoff_base=timedelta(seconds=0.05),
        backoff_cap=timedelta(seconds=0.05),
        client_timeout=timedelta(seconds=0.2),
    ),
)
NEVER_OVERRUNS = timedelta(seconds=60)
BOUND = 3.0  # seconds; every test finishes far inside this or fails


async def claim(engine: AsyncEngine, worker: str) -> Claim | None:
    async with claims.worker_transaction(
        engine, statement_timeout=FAST.statement_timeout, lock_timeout=FAST.lock_timeout
    ) as conn:
        return await claims.claim_run(conn, worker, FAST.lease)


async def claim_within(engine: AsyncEngine, worker: str, seconds: float) -> Claim | None:
    """Poll for a claim; None if none came within ``seconds``."""
    deadline = asyncio.get_running_loop().time() + seconds
    while asyncio.get_running_loop().time() < deadline:
        claimed = await claim(engine, worker)
        if claimed is not None:
            return claimed
        await asyncio.sleep(0.05)
    return None


def sleeper(seconds: float, marks: list[str]) -> Callable[[], Coroutine[Any, Any, str]]:
    async def work() -> str:
        await asyncio.sleep(seconds)
        marks.append("wrote after the sleep")
        return "done"

    return work


async def test_work_finishes_while_the_heartbeat_keeps_the_claim(
    world: World, async_runtime_engine: AsyncEngine
) -> None:
    run = world.add_run()
    claimed = await claim(async_runtime_engine, "A")
    assert claimed is not None
    marks: list[str] = []
    async with asyncio.timeout(BOUND):
        result = await hold_claim(
            async_runtime_engine, claimed, FAST, StepClock(NEVER_OVERRUNS), sleeper(1.0, marks)
        )
    assert (result, marks) == ("done", ["wrote after the sleep"])
    assert world.row(run).worker_id == "A", "1 s of work outlived a 0.4 s lease: renewed"


async def test_a_lost_claim_cancels_the_work_before_it_writes(
    world: World, async_runtime_engine: AsyncEngine
) -> None:
    run = world.add_run()
    claimed = await claim(async_runtime_engine, "A")
    assert claimed is not None

    async def steal() -> None:
        await asyncio.sleep(0.15)
        await asyncio.to_thread(world.steal, run, "B", FAST.lease)

    marks: list[str] = []
    async with asyncio.timeout(BOUND):
        thief = asyncio.create_task(steal())
        with pytest.raises(LostClaim):
            await hold_claim(
                async_runtime_engine, claimed, FAST, StepClock(NEVER_OVERRUNS), sleeper(2.0, marks)
            )
        await thief
    assert marks == [], "the work was cancelled before its write"
    assert world.row(run).worker_id == "B"


async def test_negative_control_ignoring_the_heartbeat_result_lets_the_work_write(
    world: World, async_runtime_engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    run = world.add_run()
    claimed = await claim(async_runtime_engine, "A")
    assert claimed is not None

    async def always_renewed(conn: AsyncConnection, claim: Claim, lease: timedelta) -> bool:
        return True

    monkeypatch.setattr(claims, "renew_lease", always_renewed)

    async def steal() -> None:
        await asyncio.sleep(0.15)
        await asyncio.to_thread(world.steal, run, "B", FAST.lease)

    marks: list[str] = []
    async with asyncio.timeout(BOUND):
        thief = asyncio.create_task(steal())
        await hold_claim(
            async_runtime_engine, claimed, FAST, StepClock(NEVER_OVERRUNS), sleeper(1.0, marks)
        )
        await thief
    assert marks == ["wrote after the sleep"]


async def test_a_stuck_step_stops_renewal_and_another_worker_takes_over(
    world: World, async_runtime_engine: AsyncEngine
) -> None:
    run = world.add_run()
    claimed = await claim(async_runtime_engine, "A")
    assert claimed is not None
    stuck_step = StepClock(timedelta(seconds=0.3))
    marks: list[str] = []
    async with asyncio.timeout(BOUND):
        with pytest.raises(StepOverrun):
            await hold_claim(async_runtime_engine, claimed, FAST, stuck_step, sleeper(30, marks))
        taken = await claim_within(async_runtime_engine, "B", seconds=2.0)
    assert taken is not None and taken.run_id == run
    assert marks == []


async def test_negative_control_a_heartbeat_without_liveness_keeps_a_stuck_claim(
    world: World, async_runtime_engine: AsyncEngine
) -> None:
    world.add_run()
    claimed = await claim(async_runtime_engine, "A")
    assert claimed is not None
    marks: list[str] = []
    holder = asyncio.create_task(
        hold_claim(
            async_runtime_engine, claimed, FAST, StepClock(NEVER_OVERRUNS), sleeper(30, marks)
        )
    )
    try:
        async with asyncio.timeout(BOUND):
            taken = await claim_within(async_runtime_engine, "B", seconds=1.5)
    finally:
        holder.cancel()
        await asyncio.gather(holder, return_exceptions=True)
    assert taken is None, "the stuck worker kept renewing"


def failing_once() -> Callable[[AsyncConnection, Claim, timedelta], Coroutine[Any, Any, bool]]:
    original = claims.renew_lease
    calls = 0

    async def renew(conn: AsyncConnection, claim: Claim, lease: timedelta) -> bool:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise DBAPIError("UPDATE runs", {}, Exception("connection reset"))
        return await original(conn, claim, lease)

    return renew


async def test_a_heartbeat_database_error_is_not_a_lost_claim(
    world: World, async_runtime_engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    world.add_run()
    claimed = await claim(async_runtime_engine, "A")
    assert claimed is not None
    monkeypatch.setattr(claims, "renew_lease", failing_once())
    marks: list[str] = []
    async with asyncio.timeout(BOUND):
        result = await hold_claim(
            async_runtime_engine, claimed, FAST, StepClock(NEVER_OVERRUNS), sleeper(0.5, marks)
        )
    assert result == "done"


async def test_negative_control_treating_the_error_as_fatal_cancels_the_work(
    world: World, async_runtime_engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    world.add_run()
    claimed = await claim(async_runtime_engine, "A")
    assert claimed is not None
    monkeypatch.setattr(claims, "renew_lease", failing_once())
    monkeypatch.setattr(lease, "RETRYABLE_HEARTBEAT_ERRORS", ())
    marks: list[str] = []
    async with asyncio.timeout(BOUND):
        with pytest.raises(DBAPIError):
            await hold_claim(
                async_runtime_engine, claimed, FAST, StepClock(NEVER_OVERRUNS), sleeper(0.5, marks)
            )
    assert marks == []
