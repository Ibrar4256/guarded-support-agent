"""An EXECUTING run's send loop (ADR-004, ADR-008, ADR-012 Part 3, ADR-013, ADR-014 Part 3,
Part 4). Every wait is bounded."""

import asyncio
import uuid
from collections.abc import Iterable
from dataclasses import replace
from datetime import timedelta

import pytest
from agent_testing.fakes import (
    ScriptedRefundClient,
    business_error,
    config_result,
    created_result,
)
from sqlalchemy.ext.asyncio import AsyncEngine
from worker_harness import BOUND, TIMINGS, Harness, only
from worker_world import World

from agent.alerts import Severity
from agent.core.lifecycle import RefundOutcome, RunStatus
from agent.core.refund_outcome import ConfigRead, HttpResult, ProviderConfig, RetryPolicy
from agent.core.replies import ReplyTemplate
from agent.db.claims import Claim
from agent.refund_client.config import RefundClientSettings
from agent.refund_client.http import RefundClient
from agent.refund_client.models import RefundPayload
from agent.worker import execute, gate
from agent.worker.execute import ExecutingResult, ExecutingStep, handle_executing
from agent.worker.hooks import WorkerHooks
from agent.worker.lease import StepClock

RETRY = RetryPolicy(
    attempts=3,
    backoff_base=timedelta(seconds=0.1),
    backoff_cap=timedelta(seconds=0.5),
    client_timeout=timedelta(seconds=3),
)  # worst case 10 s
FAST = replace(TIMINGS, policy=RETRY, max_failed_reads=3)
TIMEOUT = RETRY.client_timeout.total_seconds()
WIDE = config_result(window_ms=300_000, retention_ms=3_600_000)
NO_RESPONSE = HttpResult(status=None)
UNAVAILABLE = HttpResult(status=503)


def payload_of(world: World, run: uuid.UUID) -> RefundPayload:
    return RefundPayload.model_validate_json(world.row(run).canonical_payload)


def harness(
    engine: AsyncEngine,
    *,
    configs: Iterable[HttpResult] = (),
    sends: Iterable[HttpResult] = (),
    hooks: WorkerHooks | None = None,
    client: ScriptedRefundClient | None = None,
) -> Harness:
    configs = list(configs) or [WIDE] * 5
    kwargs = {"hooks": hooks} if hooks else {}
    return Harness(engine, configs=configs, sends=sends, timings=FAST, client=client, **kwargs)


async def execute_run(
    h: Harness, claimed: Claim, first_read: ProviderConfig | None = None
) -> ExecutingResult:
    async with asyncio.timeout(BOUND + 10):
        return await handle_executing(h.ctx, claimed, StepClock(timedelta(seconds=60)), first_read)


# --- outcomes ----------------------------------------------------------------------------------


async def test_a_201_completes_the_run_with_the_saved_key_and_payload(
    world: World, async_runtime_engine: AsyncEngine
) -> None:
    run = world.add_executing()
    payload = payload_of(world, run)
    h = harness(async_runtime_engine, sends=[created_result(payload)])
    claimed = await h.claim()
    assert (await execute_run(h, claimed)).step is ExecutingStep.COMPLETED
    row = world.row(run)
    assert (row.status, row.refund_outcome, row.worker_id) == (
        RunStatus.COMPLETED,
        RefundOutcome.CONFIRMED,
        None,
    )
    assert (row.send_attempts, row.claim_version) == (1, claimed.claim_version + 1)
    assert row.last_attempt_at > row.first_send_started_at
    assert h.client.sent == [(payload, row.idempotency_key)]
    event = only(world.events(run), "customer event")
    assert (event.kind, event.payload["template"]) == ("reply", ReplyTemplate.REFUND_ISSUED)
    assert event.payload["amount_minor"] == payload.amount_minor
    assert h.client.config_reads == 1


async def test_the_first_attempt_reuses_the_presend_read(
    world: World, async_runtime_engine: AsyncEngine
) -> None:
    run = world.add_executing()
    no_reads = ScriptedRefundClient(sends=[created_result(payload_of(world, run))])
    h = harness(async_runtime_engine, client=no_reads)
    claimed = await h.claim()
    provider = ProviderConfig(timedelta(seconds=300), timedelta(hours=1), timedelta(seconds=5))
    assert (await execute_run(h, claimed, first_read=provider)).step is ExecutingStep.COMPLETED
    assert h.client.config_reads == 0


async def test_retries_with_the_same_key_then_confirms(
    world: World, async_runtime_engine: AsyncEngine
) -> None:
    run = world.add_executing()
    payload = payload_of(world, run)
    h = harness(async_runtime_engine, sends=[UNAVAILABLE, NO_RESPONSE, created_result(payload)])
    claimed = await h.claim()
    assert (await execute_run(h, claimed)).step is ExecutingStep.COMPLETED
    assert world.row(run).send_attempts == 3
    assert len({key for _, key in h.client.sent}) == 1, "one key for every attempt"
    assert h.client.config_reads == 3, "a config read before every send (ADR-013)"
    assert len(h.sleeps) == 2


async def test_retries_exhausted_go_to_reconciliation(
    world: World, async_runtime_engine: AsyncEngine
) -> None:
    run = world.add_executing()
    h = harness(async_runtime_engine, sends=[UNAVAILABLE] * 3)
    claimed = await h.claim()
    assert (await execute_run(h, claimed)).step is ExecutingStep.RECONCILING
    row = world.row(run)
    assert (row.status, row.refund_outcome) == (
        RunStatus.NEEDS_RECONCILIATION,
        RefundOutcome.UNKNOWN,
    )
    assert row.escalated_at is not None and row.worker_id is None
    assert (len(h.client.sent), row.send_attempts) == (3, 3)
    assert h.alerter.kinds(Severity.ALERT) == ["retries_exhausted"]
    assert world.events(run) == [], "still 'being processed' for the customer"


async def test_a_business_error_rejects_and_frees_capacity(
    world: World, async_runtime_engine: AsyncEngine
) -> None:
    run = world.add_executing()
    h = harness(async_runtime_engine, sends=[business_error("refund_exceeds_charge")])
    claimed = await h.claim()
    assert (await execute_run(h, claimed)).step is ExecutingStep.ESCALATED
    row = world.row(run)
    assert (row.status, row.refund_outcome) == (RunStatus.ESCALATED, RefundOutcome.REJECTED)
    event = only(world.events(run), "customer event")
    assert event.payload == {"template": ReplyTemplate.NEEDS_PERSON}


@pytest.mark.parametrize("answer", [HttpResult(status=422), business_error("invalid_request")])
async def test_an_unclassifiable_answer_goes_to_reconciliation(
    world: World, async_runtime_engine: AsyncEngine, answer: HttpResult
) -> None:
    run = world.add_executing()
    h = harness(async_runtime_engine, sends=[answer])
    claimed = await h.claim()
    assert (await execute_run(h, claimed)).step is ExecutingStep.RECONCILING
    assert world.row(run).refund_outcome is RefundOutcome.UNKNOWN
    assert h.alerter.kinds(Severity.ALERT) == ["refund_outcome_unknown"]


# --- the age check, the snapshot and the per-send read (ADR-012 Part 3, ADR-013) ---------------


async def test_a_run_resumed_close_to_its_window_doesnt_send(
    world: World, async_runtime_engine: AsyncEngine
) -> None:
    run = world.add_executing(sent_seconds_ago=20, window_ms=22_000)  # 20 + 3 > 22
    h = harness(async_runtime_engine)
    claimed = await h.claim()
    assert (await execute_run(h, claimed)).step is ExecutingStep.RECONCILING
    assert (h.client.sent, world.row(run).send_attempts) == ([], 0)
    assert h.alerter.kinds(Severity.ALERT) == ["send_window_passed"]


async def test_negative_control_without_the_age_check_it_sends(
    world: World, async_runtime_engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    run = world.add_executing(sent_seconds_ago=20, window_ms=22_000)
    monkeypatch.setattr(execute, "send_allowed", lambda *args: True)
    h = harness(async_runtime_engine, sends=[created_result(payload_of(world, run))])
    claimed = await h.claim()
    await execute_run(h, claimed)
    assert len(h.client.sent) == 1


async def test_a_larger_window_read_later_doesnt_lengthen_the_snapshot(
    world: World, async_runtime_engine: AsyncEngine
) -> None:
    """ADR-012 Part 3: started under a 22 s window, read 300 s now, 20 s old: no send."""
    world.add_executing(sent_seconds_ago=20, window_ms=22_000)
    h = harness(async_runtime_engine)  # reads 300 s
    claimed = await h.claim()
    assert (await execute_run(h, claimed)).step is ExecutingStep.RECONCILING
    assert h.client.sent == []


async def test_negative_control_an_age_check_on_the_read_alone_sends(
    world: World, async_runtime_engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    run = world.add_executing(sent_seconds_ago=20, window_ms=22_000)
    monkeypatch.setattr(
        execute, "send_allowed", lambda elapsed, timeout, snapshot, read: elapsed + timeout <= read
    )
    h = harness(async_runtime_engine, sends=[created_result(payload_of(world, run))])
    claimed = await h.claim()
    await execute_run(h, claimed)
    assert len(h.client.sent) == 1


SHRUNK = config_result(window_ms=12_000, retention_ms=60_000)  # >= the 10 s worst case


async def test_a_window_decrease_before_a_retry_stops_the_retry(
    world: World, async_runtime_engine: AsyncEngine
) -> None:
    """ADR-013: the first attempt's answer is lost; refund-api's window then drops to 12 s.
    The retry reads it: 10 s old + 3 s > 12 s, so no second send."""
    world.add_executing(sent_seconds_ago=10)
    h = harness(async_runtime_engine, configs=[WIDE, SHRUNK], sends=[NO_RESPONSE])
    claimed = await h.claim()
    assert (await execute_run(h, claimed)).step is ExecutingStep.RECONCILING
    assert len(h.client.sent) == 1


async def test_negative_control_reusing_the_first_read_sends_the_retry(
    world: World, async_runtime_engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    run = world.add_executing(sent_seconds_ago=10)
    original = gate.read_config
    first: list[ConfigRead] = []

    async def cached(client: RefundClient, settings: RefundClientSettings) -> ConfigRead:
        if not first:
            first.append(await original(client, settings))
        return first[0]

    monkeypatch.setattr(execute, "read_config", cached)
    payload = payload_of(world, run)
    h = harness(
        async_runtime_engine, configs=[WIDE, SHRUNK], sends=[NO_RESPONSE, created_result(payload)]
    )
    claimed = await h.claim()
    await execute_run(h, claimed)
    assert len(h.client.sent) == 2


async def test_the_attempt_count_survives_a_restart(
    world: World, async_runtime_engine: AsyncEngine
) -> None:
    """Two sends already happened before a crash: only one more is allowed."""
    run = world.add_executing(send_attempts=2)
    h = harness(async_runtime_engine, sends=[UNAVAILABLE])
    claimed = await h.claim()
    assert (await execute_run(h, claimed)).step is ExecutingStep.RECONCILING
    assert (len(h.client.sent), world.row(run).send_attempts) == (1, 3)


# --- the attempt budget and the lease (ADR-014 Part 3) -----------------------------------------


class SlowBeforeSend(WorkerHooks):
    def __init__(self, seconds: float) -> None:
        self.seconds = seconds

    async def before_refund_call(self, run_id: uuid.UUID) -> None:
        await asyncio.sleep(self.seconds)


async def test_time_spent_after_the_budget_starts_comes_off_the_send(
    world: World, async_runtime_engine: AsyncEngine
) -> None:
    run = world.add_executing()
    h = harness(
        async_runtime_engine,
        sends=[created_result(payload_of(world, run))],
        hooks=SlowBeforeSend(1.0),
    )
    claimed = await h.claim()
    await execute_run(h, claimed)
    [budget] = h.client.send_budgets
    assert budget is not None and 0 < budget <= TIMEOUT - 1.0


async def test_with_no_budget_left_nothing_is_sent_but_the_attempt_counts(
    world: World, async_runtime_engine: AsyncEngine
) -> None:
    run = world.add_executing(send_attempts=RETRY.attempts - 1)
    h = harness(async_runtime_engine, hooks=SlowBeforeSend(TIMEOUT + 0.2))
    claimed = await h.claim()
    assert (await execute_run(h, claimed)).step is ExecutingStep.RECONCILING
    assert (h.client.sent, world.row(run).send_attempts) == ([], RETRY.attempts)


class HeldSend(ScriptedRefundClient):
    """Holds the send open until the test has looked at the run."""

    def __init__(self, *, configs: Iterable[HttpResult], sends: Iterable[HttpResult]) -> None:
        super().__init__(configs=configs, sends=sends)
        self.on_the_wire = asyncio.Event()
        self.finish = asyncio.Event()

    async def send(
        self, refund: RefundPayload, idempotency_key: str, *, timeout: float | None = None
    ) -> HttpResult:
        self.on_the_wire.set()
        await self.finish.wait()
        return await super().send(refund, idempotency_key, timeout=timeout)


async def test_the_lease_outlives_an_attempt_on_the_wire(
    world: World, async_runtime_engine: AsyncEngine
) -> None:
    run = world.add_executing()
    client = HeldSend(configs=[WIDE], sends=[created_result(payload_of(world, run))])
    h = harness(async_runtime_engine, client=client)
    claimed = await h.claim(lease=timedelta(seconds=1))
    async with asyncio.timeout(BOUND):
        task = asyncio.create_task(execute_run(h, claimed))
        await client.on_the_wire.wait()
        await asyncio.sleep(1.2)  # past the 1 s claim lease
        lease_left = await asyncio.to_thread(world.seconds_until, run, "lease_expires_at")
        other = await h.claim_or_none("B")
        client.finish.set()
        await task
    assert lease_left > 0.5, "the attempt transaction extended the lease to cover the send"
    assert other is None, "no other worker can claim the run mid-attempt"


# --- a lost claim ------------------------------------------------------------------------------


async def test_a_claim_taken_over_sends_nothing(
    world: World, async_runtime_engine: AsyncEngine
) -> None:
    run = world.add_executing()
    h = harness(async_runtime_engine)
    claimed = await h.claim()
    world.steal(run, "B", TIMINGS.lease)
    assert (await execute_run(h, claimed)).step is ExecutingStep.LOST
    assert (h.client.sent, world.row(run).send_attempts) == ([], 0)


async def test_an_expired_lease_sends_nothing_even_with_the_version_intact(
    world: World, async_runtime_engine: AsyncEngine
) -> None:
    """ADR-014 S8: a worker that couldn't renew can't send."""
    run = world.add_executing()
    h = harness(async_runtime_engine)
    claimed = await h.claim()
    world.expire_lease(run)
    assert (await execute_run(h, claimed)).step is ExecutingStep.LOST
    assert h.client.sent == []


# --- failed per-send reads (ADR-014 Part 4) ----------------------------------------------------


async def test_reads_failing_past_the_snapshot_go_to_reconciliation(
    world: World, async_runtime_engine: AsyncEngine
) -> None:
    run = world.add_executing(sent_seconds_ago=20, window_ms=22_000)
    h = harness(async_runtime_engine, configs=[UNAVAILABLE])
    claimed = await h.claim()
    assert (await execute_run(h, claimed)).step is ExecutingStep.RECONCILING
    assert (h.client.sent, world.row(run).send_attempts) == ([], 0)
    assert "send_window_passed" in h.alerter.kinds(Severity.ALERT)


async def test_reads_failing_inside_the_window_release_after_the_limit(
    world: World, async_runtime_engine: AsyncEngine
) -> None:
    run = world.add_executing()
    h = harness(async_runtime_engine, configs=[UNAVAILABLE] * FAST.max_failed_reads)
    claimed = await h.claim()
    assert (await execute_run(h, claimed)).step is ExecutingStep.RELEASED
    row = world.row(run)
    assert (row.status, row.worker_id, row.send_attempts) == (RunStatus.EXECUTING, None, 0)
    assert len(h.sleeps) == FAST.max_failed_reads - 1
    assert h.ctx.gate.closed_reason() == "read_failed", "no new claims until a read succeeds"


async def test_a_401_on_the_per_send_read_releases_and_pages(
    world: World, async_runtime_engine: AsyncEngine
) -> None:
    run = world.add_executing()
    h = harness(async_runtime_engine, configs=[HttpResult(status=401)])
    claimed = await h.claim()
    assert (await execute_run(h, claimed)).step is ExecutingStep.RELEASED
    assert (h.client.sent, world.row(run).worker_id) == ([], None)
    assert h.alerter.kinds(Severity.PAGE) == ["refund_api_auth_failed"]
