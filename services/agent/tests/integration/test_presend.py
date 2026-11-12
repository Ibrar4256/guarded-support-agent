"""Pre-send: an APPROVED run's config read, the one customer-locked policy check, and every
way it ends (ADR-004 step 6, ADR-005, ADR-008, ADR-009, ADR-013, ADR-014 S4, S5, S12).

Every wait is bounded; sync world calls made while a worker transaction may be open go
through ``asyncio.to_thread`` (see ``worker_world``).
"""

import asyncio
import uuid
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from agent_testing.fakes import config_result
from sqlalchemy import select
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine
from worker_harness import BOUND, READ_WINDOW_MS, TIMINGS, only
from worker_harness import Harness as BaseHarness
from worker_world import World

from agent.alerts import Severity
from agent.core.lifecycle import RefundOutcome, RunStatus
from agent.core.policy import (
    Fired,
    PolicyConfig,
    PresendInputs,
    PresendOutcome,
    PresendResult,
    RuleId,
)
from agent.core.refund_outcome import HttpResult
from agent.core.replies import ReplyTemplate
from agent.db import presend as presend_db
from agent.db.claims import Claim
from agent.db.schema import customers
from agent.worker import presend
from agent.worker.hooks import WorkerHooks
from agent.worker.lease import StepClock
from agent.worker.presend import ApprovedStep, handle_approved


class Harness(BaseHarness):
    async def run(self, claimed: Claim) -> presend.ApprovedResult:
        async with asyncio.timeout(BOUND):
            return await handle_approved(self.ctx, claimed, StepClock(timedelta(seconds=60)))


# --- PROCEED ---------------------------------------------------------------------------------


async def test_proceed_moves_to_executing_with_the_window_just_read(
    world: World, async_runtime_engine: AsyncEngine
) -> None:
    run = world.add_refund_run(world.add_order(), 2_000, release_streak=2)
    harness = Harness(async_runtime_engine)
    claimed = await harness.claim()
    result = await harness.run(claimed)
    assert result.step is ApprovedStep.EXECUTING
    assert result.config is not None
    assert result.config.max_retry_window == timedelta(milliseconds=READ_WINDOW_MS)
    row = world.row(run)
    assert (row.status, row.refund_outcome) == (RunStatus.EXECUTING, RefundOutcome.UNKNOWN)
    assert row.first_send_started_at is not None
    assert row.first_send_started_at == row.last_attempt_at, "the quiet-period anchor"
    assert row.send_window_ms == READ_WINDOW_MS, "snapshot from the read, not the agent's cap"
    assert (row.worker_id, row.claim_version) == ("A", claimed.claim_version), "claim kept"
    assert (row.release_streak, row.send_attempts) == (0, 0)
    decision = only(world.decisions(run), "decision row")
    assert (decision.stage, decision.outcome, decision.rule_ids) == ("presend", "PROCEED", [])
    assert decision.decided_by == "policy"
    assert decision.input_snapshot["used_by_customer_minor"] == 0
    assert (harness.client.config_reads, harness.client.sent) == (1, [])


# --- OUT_OF_POLICY ---------------------------------------------------------------------------


def over_customer_limit(world: World) -> uuid.UUID:
    world.add_refund_run(world.add_order(charged_minor=50_000), 19_000, RunStatus.COMPLETED)
    return world.add_refund_run(world.add_order(), 2_000)


def chargeback_opened(world: World) -> uuid.UUID:
    return world.add_refund_run(world.add_order(chargeback_open=True), 2_000)


@pytest.mark.parametrize(
    ("scenario", "rule"),
    [
        (over_customer_limit, RuleId.R06_CUSTOMER_LIMIT),
        (chargeback_opened, RuleId.R10_NO_CHARGEBACK),
    ],
)
async def test_out_of_policy_escalates_with_a_customer_event_and_nothing_sent(
    world: World,
    async_runtime_engine: AsyncEngine,
    scenario: Callable[[World], uuid.UUID],
    rule: RuleId,
) -> None:
    run = scenario(world)
    harness = Harness(async_runtime_engine)
    claimed = await harness.claim()
    assert (await harness.run(claimed)).step is ApprovedStep.ESCALATED
    row = world.row(run)
    assert (row.status, row.refund_outcome) == (RunStatus.ESCALATED, RefundOutcome.NONE)
    assert row.escalated_at is not None and row.first_send_started_at is None
    assert (row.worker_id, row.claim_version) == (None, claimed.claim_version + 1)
    event = only(world.events(run), "customer event")
    assert (event.kind, event.payload) == ("escalated", {"template": ReplyTemplate.NEEDS_PERSON})
    decision = only(world.decisions(run), "decision row")
    assert decision.outcome == "OUT_OF_POLICY" and str(rule) in decision.rule_ids
    assert harness.client.sent == []


# --- HOLD ------------------------------------------------------------------------------------


async def test_a_hold_after_the_claim_releases_and_leaves_the_run_approved(
    world: World, async_runtime_engine: AsyncEngine
) -> None:
    run = world.add_refund_run(world.add_order(), 2_000, release_streak=2)
    harness = Harness(async_runtime_engine)
    claimed = await harness.claim()
    world.controls(sends_enabled=False)
    assert (await harness.run(claimed)).step is ApprovedStep.RELEASED
    row = world.row(run)
    assert (row.status, row.worker_id) == (RunStatus.APPROVED, None)
    assert (row.claim_version, row.release_streak) == (claimed.claim_version + 1, 0)
    decision = only(world.decisions(run), "decision row")
    assert decision.outcome == "HOLD" and str(RuleId.R14_SENDS_HELD) in decision.rule_ids
    assert world.events(run) == []


# --- FAIL_CLOSED -----------------------------------------------------------------------------


def always_fail_closed(inputs: PresendInputs, config: PolicyConfig) -> PresendResult:
    return PresendResult(
        PresendOutcome.FAIL_CLOSED,
        (Fired(RuleId.R05_ORDER_CHARGED_CAP, PresendOutcome.FAIL_CLOSED, error="boom"),),
    )


async def test_fail_closed_is_counted_retried_then_escalated(
    world: World, async_runtime_engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    run = world.add_refund_run(world.add_order(), 2_000)
    monkeypatch.setattr(presend, "evaluate_presend", always_fail_closed)
    harness = Harness(async_runtime_engine)
    claimed = await harness.claim()
    assert (await harness.run(claimed)).step is ApprovedStep.ESCALATED
    row = world.row(run)
    assert (row.status, row.presend_attempts) == (RunStatus.ESCALATED, 3)
    assert row.first_send_started_at is None
    outcomes = [(d.outcome, d.rule_ids) for d in world.decisions(run)]
    assert outcomes == [("FAIL_CLOSED", ["FAIL_CLOSED:R05_ORDER_CHARGED_CAP"])] * 3
    assert len(harness.sleeps) == 2, "backed off between tries, keeping the claim"
    assert harness.alerter.kinds(Severity.ALERT) == ["presend_fail_closed_escalated"]


async def test_a_missing_input_fails_closed_and_still_logs(
    world: World, async_runtime_engine: AsyncEngine
) -> None:
    run = world.add_refund_run(world.add_order(), None)  # an approved run with no amount
    harness = Harness(async_runtime_engine)
    claimed = await harness.claim()
    assert (await harness.run(claimed)).step is ApprovedStep.ESCALATED
    decisions = world.decisions(run)
    assert [d.outcome for d in decisions] == ["FAIL_CLOSED"] * 3
    assert "missing input" in decisions[0].input_snapshot["reason"]


class SerializationFailure(Exception):
    sqlstate = "40001"


async def failing_inputs(*args: object, **kwargs: object) -> PresendInputs:
    raise DBAPIError("SELECT orders", {}, SerializationFailure())


async def test_another_database_error_fails_closed_not_as_a_lock_wait(
    world: World, async_runtime_engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    run = world.add_refund_run(world.add_order(), 2_000)
    monkeypatch.setattr(presend_db, "load_inputs", failing_inputs)
    harness = Harness(async_runtime_engine)
    claimed = await harness.claim()
    assert (await harness.run(claimed)).step is ApprovedStep.ESCALATED
    row = world.row(run)
    assert (row.presend_attempts, row.claimable_after) == (3, None)


async def test_negative_control_every_error_as_a_lock_wait_retries_a_real_failure(
    world: World, async_runtime_engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    run = world.add_refund_run(world.add_order(), 2_000)
    monkeypatch.setattr(presend_db, "load_inputs", failing_inputs)
    monkeypatch.setattr(presend, "is_lock_timeout", lambda exc: True)
    harness = Harness(async_runtime_engine)
    claimed = await harness.claim()
    assert (await harness.run(claimed)).step is ApprovedStep.COOLDOWN
    row = world.row(run)
    assert (row.presend_attempts, row.status) == (0, RunStatus.APPROVED), "never counted"


# --- lock timeouts (ADR-005) and the streak (S12) ----------------------------------------------


async def run_with_customer_locked(
    world: World, harness: Harness, claimed: Claim
) -> presend.ApprovedResult:
    with world.owner.connect() as holder, holder.begin():
        holder.execute(select(customers.c.id).where(customers.c.id == world.user).with_for_update())
        return await harness.run(claimed)


async def test_lock_timeouts_keep_the_claim_then_release_with_a_cooldown(
    world: World, async_runtime_engine: AsyncEngine
) -> None:
    run = world.add_refund_run(world.add_order(), 2_000)
    harness = Harness(async_runtime_engine)
    claimed = await harness.claim()
    owners_during_backoff: list[str | None] = []

    async def sleep(seconds: float) -> None:
        owners_during_backoff.append((await asyncio.to_thread(world.row, run)).worker_id)

    harness.ctx = replace(harness.ctx, sleep=sleep)
    assert (await run_with_customer_locked(world, harness, claimed)).step is ApprovedStep.COOLDOWN
    assert owners_during_backoff == ["A"], "the claim is kept while backing off"
    row = world.row(run)
    assert (row.status, row.worker_id, row.release_streak) == (RunStatus.APPROVED, None, 1)
    assert row.claimable_after is not None and row.presend_attempts == 0
    assert world.decisions(run) == [], "nothing was decided: every try rolled back"
    assert harness.alerter.kinds(Severity.ALERT) == ["presend_lock_timeouts"]


async def test_at_the_streak_limit_an_unsent_run_escalates_instead(
    world: World, async_runtime_engine: AsyncEngine
) -> None:
    run = world.add_refund_run(
        world.add_order(), 2_000, release_streak=TIMINGS.release_streak_limit - 1
    )
    harness = Harness(async_runtime_engine)
    claimed = await harness.claim()
    assert (await run_with_customer_locked(world, harness, claimed)).step is ApprovedStep.ESCALATED
    row = world.row(run)
    assert (row.status, row.refund_outcome, row.worker_id) == (
        RunStatus.ESCALATED,
        RefundOutcome.NONE,
        None,
    )
    event = only(world.events(run), "customer event")
    assert event.payload == {"template": ReplyTemplate.NEEDS_PERSON}
    assert harness.alerter.kinds(Severity.ALERT) == [
        "presend_lock_timeouts",
        "release_streak_limit",
    ]


# --- a lost claim ----------------------------------------------------------------------------


class StealAfterRead(WorkerHooks):
    def __init__(self, world: World) -> None:
        self.world = world

    async def after_capacity_read(self, run_id: uuid.UUID) -> None:
        await asyncio.to_thread(self.world.steal, run_id, "B", TIMINGS.lease)


async def test_a_claim_lost_inside_the_check_rolls_everything_back(
    world: World, async_runtime_engine: AsyncEngine
) -> None:
    run = world.add_refund_run(world.add_order(), 2_000)
    harness = Harness(async_runtime_engine, hooks=StealAfterRead(world))
    claimed = await harness.claim()
    assert (await harness.run(claimed)).step is ApprovedStep.LOST
    row = world.row(run)
    assert (row.status, row.worker_id, row.first_send_started_at) == (RunStatus.APPROVED, "B", None)
    assert world.decisions(run) == [], "the decision row rolled back with the 0-row move"


# --- the config read before pre-send (ADR-013, ADR-014 Part 4, S4) ----------------------------


@pytest.mark.parametrize(
    ("result", "closed", "paged"),
    [
        pytest.param(HttpResult(status=503), "read_failed", False, id="failed"),
        pytest.param(HttpResult(status=401), "auth_failed", True, id="unauthorized"),
        pytest.param(
            config_result(window_ms=60_000, retention_ms=3_600_000),
            "window_too_small",
            False,
            id="window-below-worst-case",
        ),
    ],
)
async def test_a_bad_config_read_releases_before_any_check(
    world: World,
    async_runtime_engine: AsyncEngine,
    result: HttpResult,
    closed: str,
    paged: bool,
) -> None:
    run = world.add_refund_run(world.add_order(), 2_000)
    harness = Harness(async_runtime_engine, configs=[result])
    claimed = await harness.claim()
    assert (await harness.run(claimed)).step is ApprovedStep.RELEASED
    row = world.row(run)
    assert (row.status, row.worker_id) == (RunStatus.APPROVED, None)
    assert world.decisions(run) == [], "the policy check never ran"
    assert harness.ctx.gate.closed_reason() == closed
    assert bool(harness.alerter.kinds(Severity.PAGE)) is paged


# --- ADR-005: two runs for one customer, with controlled timing -------------------------------


class PauseAfterRead(WorkerHooks):
    def __init__(self) -> None:
        self.reached: dict[uuid.UUID, asyncio.Event] = {}
        self.proceed: dict[uuid.UUID, asyncio.Event] = {}

    def events_for(self, run_id: uuid.UUID) -> tuple[asyncio.Event, asyncio.Event]:
        return (
            self.reached.setdefault(run_id, asyncio.Event()),
            self.proceed.setdefault(run_id, asyncio.Event()),
        )

    async def after_capacity_read(self, run_id: uuid.UUID) -> None:
        reached, proceed = self.events_for(run_id)
        reached.set()
        await proceed.wait()


async def race(world: World, engine: AsyncEngine, *, expect_blocked: bool) -> list[RunStatus]:
    """Run 1 pauses after its capacity read; run 2 starts. With the lock, run 2 must block on
    the customer row; without it, run 2 reaches its own read too. Then both go on."""
    earlier = datetime.now(UTC) - timedelta(hours=2)
    first = world.add_refund_run(world.add_order(charged_minor=50_000), 12_000, approved_at=earlier)
    second = world.add_refund_run(world.add_order(charged_minor=50_000), 12_000)
    hooks = PauseAfterRead()
    slow_locks = replace(TIMINGS, lock_timeout=timedelta(seconds=2))
    a, b = (
        Harness(engine, hooks=hooks, timings=slow_locks),
        Harness(engine, hooks=hooks, timings=slow_locks),
    )
    claim_a, claim_b = await a.claim("A"), await b.claim("B")
    assert (claim_a.run_id, claim_b.run_id) == (first, second)
    reached_a, proceed_a = hooks.events_for(first)
    reached_b, proceed_b = hooks.events_for(second)
    async with asyncio.timeout(BOUND):
        task_a = asyncio.create_task(a.run(claim_a))
        await reached_a.wait()
        task_b = asyncio.create_task(b.run(claim_b))
        if expect_blocked:
            while await asyncio.to_thread(world.waiting_locks) == 0:
                await asyncio.sleep(0.01)
            assert not reached_b.is_set(), "run 2 read capacity while run 1 held the lock"
        else:
            await reached_b.wait()
        proceed_a.set()
        proceed_b.set()
        await asyncio.gather(task_a, task_b)
    return [world.row(first).status, world.row(second).status]


async def test_two_runs_for_one_customer_only_one_executes(
    world: World, async_runtime_engine: AsyncEngine
) -> None:
    statuses = await race(world, async_runtime_engine, expect_blocked=True)
    assert statuses == [RunStatus.EXECUTING, RunStatus.ESCALATED]


async def test_negative_control_without_the_customer_lock_both_execute(
    world: World, async_runtime_engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(presend_db, "LOCK_CUSTOMER", False)
    statuses = await race(world, async_runtime_engine, expect_blocked=False)
    assert statuses == [RunStatus.EXECUTING, RunStatus.EXECUTING], "write skew: limit exceeded"
