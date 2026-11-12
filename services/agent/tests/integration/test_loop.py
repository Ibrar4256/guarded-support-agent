"""The run loop (ADR-004 step 6-7, ADR-012 Part 4, ADR-014 "One claimed run", Part 4, S6, S7,
S12). Every wait is bounded."""

import asyncio
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import replace
from datetime import timedelta

import pytest
from agent_testing.fakes import ScriptedRefundClient, config_result, created_result
from sqlalchemy.ext.asyncio import AsyncEngine
from worker_harness import TIMINGS, Harness, only
from worker_world import World

from agent.alerts import Severity
from agent.core.lifecycle import RefundOutcome, RunStatus
from agent.core.policy import PresendInputs, PresendOutcome, RefundProposal, evaluate_presend
from agent.core.refund_outcome import HttpResult, RetryPolicy
from agent.core.replies import ReplyTemplate
from agent.db import presend as presend_db
from agent.refund_client.models import RefundPayload
from agent.worker import execute, presend
from agent.worker.hooks import WorkerHooks
from agent.worker.loop import Ended, process_claim, run_worker
from agent.worker.timings import WorkerConfigError, WorkerTimings

QUICK = RetryPolicy(
    attempts=3,
    backoff_base=timedelta(seconds=0.05),
    backoff_cap=timedelta(seconds=0.1),
    client_timeout=timedelta(seconds=0.5),
)  # worst case 1.7 s
LOOP = replace(
    TIMINGS,
    policy=QUICK,
    lease=timedelta(seconds=1),
    heartbeat=timedelta(seconds=0.1),
    liveness_margin=timedelta(seconds=0.05),
    lock_timeout=timedelta(seconds=0.1),
    statement_timeout=timedelta(seconds=0.3),
    shutdown_grace=timedelta(seconds=1),
)  # a step may take 0.75 s
WIDE = config_result(window_ms=300_000, retention_ms=3_600_000)
UNAVAILABLE = HttpResult(status=503)
BOUND = 8.0


def payload_of(world: World, run: uuid.UUID) -> RefundPayload:
    return RefundPayload.model_validate_json(world.row(run).canonical_payload)


def loop_harness(
    engine: AsyncEngine,
    client: ScriptedRefundClient,
    *,
    hooks: WorkerHooks | None = None,
    timings: WorkerTimings = LOOP,
) -> Harness:
    if hooks is None:
        return Harness(engine, client=client, timings=timings)
    return Harness(engine, client=client, timings=timings, hooks=hooks)


async def process(h: Harness, worker: str = "A") -> Ended:
    claimed = await h.claim(worker)
    async with asyncio.timeout(BOUND):
        return await process_claim(h.ctx, claimed)


async def until(condition: Callable[[], Awaitable[bool]], seconds: float = 3.0) -> None:
    async with asyncio.timeout(seconds):
        while not await condition():
            await asyncio.sleep(0.02)


# --- dispatch ----------------------------------------------------------------------------------


async def test_an_approved_run_goes_through_presend_and_completes_on_one_config_read(
    world: World, async_runtime_engine: AsyncEngine
) -> None:
    run = world.add_refund_run(world.add_order(), 2_000)
    client = ScriptedRefundClient(configs=[WIDE], sends=[created_result(payload_of(world, run))])
    h = loop_harness(async_runtime_engine, client)
    assert await process(h) is Ended.COMPLETED
    assert world.row(run).refund_outcome is RefundOutcome.CONFIRMED
    assert only(world.decisions(run), "decision row").outcome == "PROCEED"
    assert client.config_reads == 1, "pre-send's read bounds the first send too"


def tight_resumed_run(world: World) -> uuid.UUID:
    """19 000 already refunded against a 20 000 limit, and a 2 000 refund that may already
    have been sent before a crash: re-checking it now would count it twice."""
    world.add_refund_run(world.add_order(charged_minor=50_000), 19_000, RunStatus.COMPLETED)
    return world.add_executing(2_000)


async def test_a_resumed_executing_run_is_never_policy_checked(
    world: World, async_runtime_engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    run = tight_resumed_run(world)

    def must_not_run(inputs: PresendInputs, config: object) -> None:
        raise AssertionError("the policy check ran on a run that may already be sent")

    monkeypatch.setattr(presend, "evaluate_presend", must_not_run)
    client = ScriptedRefundClient(configs=[WIDE], sends=[created_result(payload_of(world, run))])
    h = loop_harness(async_runtime_engine, client)
    assert await process(h) is Ended.COMPLETED
    assert world.row(run).refund_outcome is RefundOutcome.CONFIRMED


async def test_negative_control_a_recheck_of_that_run_would_reject_it(
    world: World, async_runtime_engine: AsyncEngine
) -> None:
    """The run above is really tight: the policy, asked now, says no (ADR-004 step 6)."""
    run = tight_resumed_run(world)
    row = world.row(run)
    resumed = presend_db.PresendRun(
        run_id=run,
        conversation_id=row.conversation_id,
        user_id=row.user_id,
        proposal=RefundProposal(
            action_id=row.action_id,
            order_id=row.order_id,
            amount_minor=row.amount_minor,
            currency=row.currency,
        ),
        approval_source=row.approval_source,
        review_hash=row.review_hash,
    )
    async with async_runtime_engine.connect() as conn:
        inputs = await presend_db.load_inputs(conn, resumed, await presend_db.db_now(conn))
    h = loop_harness(async_runtime_engine, ScriptedRefundClient())
    # Its own unknown outcome would be excluded, but a re-check after a crash counts the
    # confirmed 19 000 plus this 2 000: over the 20 000 limit.
    assert evaluate_presend(inputs, h.ctx.policy).outcome is PresendOutcome.OUT_OF_POLICY


# --- S6: unexpected errors, stuck steps and the streak ------------------------------------------


def explode(*args: object, **kwargs: object) -> None:
    raise RuntimeError("a bug")


async def test_an_unexpected_error_alerts_and_releases_with_a_cooldown(
    world: World, async_runtime_engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    run = world.add_executing()
    monkeypatch.setattr(execute, "classify_send", explode)
    client = ScriptedRefundClient(configs=[WIDE], sends=[created_result(payload_of(world, run))])
    h = loop_harness(async_runtime_engine, client)
    assert await process(h) is Ended.COOLDOWN
    row = world.row(run)
    assert (row.status, row.refund_outcome) == (RunStatus.EXECUTING, RefundOutcome.UNKNOWN)
    assert (row.worker_id, row.release_streak) == (None, 1)
    assert row.claimable_after is not None, "not reclaimed straight into the same bug"
    assert h.alerter.kinds(Severity.ALERT) == ["worker_unexpected_error"]


async def test_at_the_streak_limit_an_unsent_run_escalates(
    world: World, async_runtime_engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    run = world.add_refund_run(
        world.add_order(), 2_000, release_streak=LOOP.release_streak_limit - 1
    )
    monkeypatch.setattr(presend, "evaluate_presend", explode)
    h = loop_harness(async_runtime_engine, ScriptedRefundClient(configs=[WIDE]))
    assert await process(h) is Ended.ESCALATED
    row = world.row(run)
    assert (row.status, row.refund_outcome) == (RunStatus.ESCALATED, RefundOutcome.NONE)
    assert only(world.events(run), "customer event").payload == {
        "template": ReplyTemplate.NEEDS_PERSON
    }
    assert h.alerter.kinds(Severity.ALERT) == ["worker_unexpected_error", "release_streak_limit"]


async def test_at_the_streak_limit_a_run_that_may_have_sent_pages_and_keeps_its_status(
    world: World, async_runtime_engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """S12's case: the error comes before the age check, so the run never makes progress (an
    attempt record would reset the streak) and never ages into reconciliation."""
    run = world.add_executing(release_streak=LOOP.release_streak_limit - 1)
    monkeypatch.setattr(execute, "send_allowed", explode)
    client = ScriptedRefundClient(configs=[WIDE])
    h = loop_harness(async_runtime_engine, client)
    assert await process(h) is Ended.COOLDOWN
    row = world.row(run)
    assert (row.status, row.refund_outcome) == (RunStatus.EXECUTING, RefundOutcome.UNKNOWN)
    assert (row.release_streak, client.sent) == (LOOP.release_streak_limit, [])
    pages = [r for r in h.alerter.raised if r.severity is Severity.PAGE]
    assert [(p.kind, p.run_id) for p in pages] == [("release_streak_limit", run)]


async def test_an_attempt_record_resets_the_streak(
    world: World, async_runtime_engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """S12: progress resets it. An error after the attempt is recorded starts a new streak."""
    run = world.add_executing(release_streak=LOOP.release_streak_limit - 1)
    monkeypatch.setattr(execute, "classify_send", explode)
    client = ScriptedRefundClient(configs=[WIDE], sends=[created_result(payload_of(world, run))])
    h = loop_harness(async_runtime_engine, client)
    assert await process(h) is Ended.COOLDOWN
    assert world.row(run).release_streak == 1
    assert [r.kind for r in h.alerter.raised if r.severity is Severity.PAGE] == []


class Stuck(WorkerHooks):
    def __init__(self, seconds: float = 30) -> None:
        self.seconds = seconds

    async def before_refund_call(self, run_id: uuid.UUID) -> None:
        await asyncio.sleep(self.seconds)


async def test_a_stuck_step_is_released_with_a_cooldown_before_anything_is_sent(
    world: World, async_runtime_engine: AsyncEngine
) -> None:
    run = world.add_executing()
    client = ScriptedRefundClient(configs=[WIDE])
    h = loop_harness(async_runtime_engine, client, hooks=Stuck())
    assert await process(h) is Ended.COOLDOWN
    assert client.sent == []
    assert world.row(run).claimable_after is not None
    assert h.alerter.kinds(Severity.ALERT) == ["worker_step_overrun"]


class StealThenWait(WorkerHooks):
    def __init__(self, world: World) -> None:
        self.world = world

    async def before_refund_call(self, run_id: uuid.UUID) -> None:
        await asyncio.to_thread(self.world.steal, run_id, "B", timedelta(seconds=30))
        await asyncio.sleep(2)  # the heartbeat notices well before this ends


async def test_a_lost_claim_ends_the_run_without_sending_or_writing(
    world: World, async_runtime_engine: AsyncEngine
) -> None:
    run = world.add_executing()
    client = ScriptedRefundClient(configs=[WIDE])
    h = loop_harness(async_runtime_engine, client, hooks=StealThenWait(world))
    assert await process(h) is Ended.LOST
    row = world.row(run)
    assert (client.sent, row.worker_id, row.claimable_after) == ([], "B", None)
    assert h.alerter.raised == []


# --- S7: stopping ------------------------------------------------------------------------------


async def test_stopping_between_attempts_releases_the_run(
    world: World, async_runtime_engine: AsyncEngine
) -> None:
    run = world.add_executing()
    client = ScriptedRefundClient(configs=[WIDE, WIDE], sends=[UNAVAILABLE])
    h = loop_harness(async_runtime_engine, client)

    async def stop_during_backoff(seconds: float) -> None:
        h.ctx.stopping.set()

    h.ctx = replace(h.ctx, sleep=stop_during_backoff)
    assert await process(h) is Ended.RELEASED
    row = world.row(run)
    assert (row.status, row.worker_id, row.send_attempts) == (RunStatus.EXECUTING, None, 1)
    assert len(client.sent) == 1, "no further attempt once stopping"


# --- run_worker --------------------------------------------------------------------------------


class Worker:
    """``run_worker`` in a task, stopped and awaited (bounded) on exit."""

    def __init__(self, h: Harness) -> None:
        self.h = h
        self.task: asyncio.Task[None] | None = None

    async def __aenter__(self) -> "Worker":
        self.task = asyncio.create_task(run_worker(self.h.ctx, "W"))
        return self

    async def __aexit__(self, *exc: object) -> None:
        assert self.task is not None
        self.h.ctx.stopping.set()
        async with asyncio.timeout(BOUND):
            await self.task


async def status_of(world: World, run: uuid.UUID) -> RunStatus:
    return RunStatus((await asyncio.to_thread(world.row, run)).status)


async def test_the_worker_claims_and_completes_runs_until_stopped(
    world: World, async_runtime_engine: AsyncEngine
) -> None:
    first = world.add_refund_run(world.add_order(), 1_000)
    second = world.add_refund_run(world.add_order(), 1_500)
    sends = [created_result(payload_of(world, first)), created_result(payload_of(world, second))]
    client = ScriptedRefundClient(configs=[WIDE] * 3, sends=sends)
    async with Worker(loop_harness(async_runtime_engine, client)):

        async def both_done() -> bool:
            return {await status_of(world, first), await status_of(world, second)} == {
                RunStatus.COMPLETED
            }

        await until(both_done)


async def test_a_closed_gate_at_startup_claims_nothing_and_pages(
    world: World, async_runtime_engine: AsyncEngine
) -> None:
    run = world.add_refund_run(world.add_order(), 2_000)
    h = loop_harness(async_runtime_engine, ScriptedRefundClient(configs=[HttpResult(status=401)]))
    async with Worker(h):
        await asyncio.sleep(0.5)
    row = world.row(run)
    assert (row.status, row.claim_version) == (RunStatus.APPROVED, 0), "never claimed"
    assert h.alerter.kinds(Severity.PAGE) == ["refund_api_auth_failed"]


async def test_a_window_below_the_worst_case_is_fatal_at_startup(
    world: World, async_runtime_engine: AsyncEngine
) -> None:
    run = world.add_refund_run(world.add_order(), 2_000)
    too_small = config_result(window_ms=1_000, retention_ms=60_000)
    h = loop_harness(async_runtime_engine, ScriptedRefundClient(configs=[too_small]))
    with pytest.raises(WorkerConfigError):
        async with asyncio.timeout(BOUND):
            await run_worker(h.ctx, "W")
    assert world.row(run).claim_version == 0


async def test_a_failed_per_send_read_stops_reclaiming_until_a_read_succeeds(
    world: World, async_runtime_engine: AsyncEngine
) -> None:
    """ADR-014 Part 4: startup opens the gate; the run's own read fails and the run is
    released; the gate is now closed, so it isn't reclaimed in a loop. The next refresh is
    30 s away, past this test."""
    run = world.add_refund_run(world.add_order(), 2_000)
    client = ScriptedRefundClient(configs=[WIDE, UNAVAILABLE])
    h = loop_harness(async_runtime_engine, client)
    async with Worker(h):

        async def released() -> bool:
            return bool((await asyncio.to_thread(world.row, run)).claim_version == 2)

        await until(released)
        await asyncio.sleep(0.5)  # several poll intervals
    row = world.row(run)
    assert (row.claim_version, row.worker_id, row.status) == (2, None, RunStatus.APPROVED)
    assert client.config_reads == 2, "claimed once, released once, not reclaimed"


class HeldSend(ScriptedRefundClient):
    def __init__(self, *, configs: list[HttpResult], sends: list[HttpResult]) -> None:
        super().__init__(configs=configs, sends=sends)
        self.on_the_wire = asyncio.Event()
        self.finish = asyncio.Event()

    async def send(
        self, refund: RefundPayload, idempotency_key: str, *, timeout: float | None = None
    ) -> HttpResult:
        self.on_the_wire.set()
        await self.finish.wait()
        return await super().send(refund, idempotency_key, timeout=timeout)


async def test_stopping_lets_an_attempt_on_the_wire_finish_and_be_recorded(
    world: World, async_runtime_engine: AsyncEngine
) -> None:
    """Grace, not liveness, is under test: the fake send ignores its timeout, so a patient step
    bound keeps a slow moment under load from ending the attempt as a stuck step instead."""
    run = world.add_refund_run(world.add_order(), 2_000)
    client = HeldSend(configs=[WIDE, WIDE], sends=[created_result(payload_of(world, run))])
    patient = replace(LOOP, liveness_margin=timedelta(seconds=30))
    h = loop_harness(async_runtime_engine, client, timings=patient)
    task = asyncio.create_task(run_worker(h.ctx, "W"))
    async with asyncio.timeout(BOUND):
        await client.on_the_wire.wait()
        h.ctx.stopping.set()
        await asyncio.sleep(0.2)
        client.finish.set()
        await task
    assert h.alerter.raised == [], "nothing went wrong on the way"
    assert world.row(run).refund_outcome is RefundOutcome.CONFIRMED


async def test_a_run_still_busy_after_the_grace_period_is_abandoned_to_its_lease(
    world: World, async_runtime_engine: AsyncEngine
) -> None:
    run = world.add_executing()
    patient = replace(LOOP, liveness_margin=timedelta(seconds=30))  # liveness won't fire first
    h = loop_harness(
        async_runtime_engine,
        ScriptedRefundClient(configs=[WIDE, WIDE]),
        hooks=Stuck(),
        timings=patient,
    )
    task = asyncio.create_task(run_worker(h.ctx, "W"))
    await until(lambda: _claimed_by(world, run, "W"))
    h.ctx.stopping.set()
    started = asyncio.get_running_loop().time()
    async with asyncio.timeout(BOUND):
        await task
    waited = asyncio.get_running_loop().time() - started
    assert (
        patient.shutdown_grace.total_seconds()
        <= waited
        < patient.shutdown_grace.total_seconds() + 1
    )
    assert world.row(run).worker_id == "W", "no release: the lease expires on its own"


async def _claimed_by(world: World, run: uuid.UUID, worker: str) -> bool:
    return bool((await asyncio.to_thread(world.row, run)).worker_id == worker)
