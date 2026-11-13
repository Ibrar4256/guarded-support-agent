"""Provider breakers in the loops (ADR-018), built as production builds them: half-open at start,
on a clock the test moves. Every check has a negative control; every wait is bounded."""

import asyncio
import uuid
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from agent_testing.fakes import (
    RecordingAlerter,
    ScriptedRefundClient,
    created_result,
    lookup_result,
)
from sqlalchemy import ColumnElement, FromClause, text
from sqlalchemy.ext.asyncio import AsyncEngine
from worker_harness import FITS, TIMINGS, Harness, Lookups, never_opening
from worker_world import World

from agent.alerts import Severity
from agent.core.lifecycle import RefundOutcome, RunStatus
from agent.core.refund_outcome import (
    ConfigRead,
    ConfigReadKind,
    Health,
    HttpResult,
    ProviderConfig,
)
from agent.db import claims
from agent.db.claims import ProbeOrder
from agent.refund_client.models import RefundPayload
from agent.worker import execute, presend
from agent.worker import gate as gate_module
from agent.worker.gate import GateState
from agent.worker.loop import Ended, claim_next, process_claim, run_worker
from agent.worker.provider_health import BreakerState, ProviderBreaker
from agent.worker.reconcile import LookupEvidence, Reconciled, reconcile_once

BOUND = 5.0
UNAVAILABLE = HttpResult(status=503)
MISROUTED = HttpResult(status=404, body=b'{"detail": "Not Found"}')
OPEN = TIMINGS.provider_open.total_seconds()
PAGE_AFTER = TIMINGS.stale_page_after.total_seconds()


def payload_of(world: World, run: uuid.UUID) -> RefundPayload:
    return RefundPayload.model_validate_json(world.row(run).canonical_payload)


def approved(world: World, hours_ago: float, amount: int = 1_000) -> uuid.UUID:
    return world.add_refund_run(
        world.add_order(), amount, approved_at=datetime.now(UTC) - timedelta(hours=hours_ago)
    )


def ago(seconds: float) -> Any:
    return text(f"now() - interval '{seconds} seconds'")


def earlier_approved(world: World) -> uuid.UUID:
    """A healthy EXECUTING run (window wide open) approved before the others."""
    run = world.add_executing(sent_seconds_ago=5, send_attempts=1)
    world.set(run, approved_at=datetime.now(UTC) - timedelta(hours=5))
    return run


def ready(h: Harness) -> Harness:
    """The gate open, as ``run_worker``'s startup read leaves it, so claims test the breakers."""
    window, retention, budget = (timedelta(seconds=n) for n in (300, 3_600, 5))
    h.ctx.gate.record(
        ConfigRead(ConfigReadKind.OK, "valid", ProviderConfig(window, retention, budget))
    )
    return h


def state(breaker: ProviderBreaker) -> BreakerState:
    return breaker.state


async def cycle(h: Harness) -> Ended | None:
    """One turn of the send loop, as ``run_worker`` takes it (claim, process, free the slot)."""
    async with asyncio.timeout(BOUND):
        claimed = await claim_next(h.ctx, "W")
        if claimed is None:
            return None
        try:
            return await process_claim(h.ctx, claimed)
        finally:
            h.ctx.write_breaker.release_probe()


def evidence_of(h: Harness) -> LookupEvidence:
    return LookupEvidence(lambda: h.ctx.read_breaker.successes)


async def reconcile_pass(h: Harness, evidence: LookupEvidence) -> Reconciled | None:
    async with asyncio.timeout(BOUND):
        return await reconcile_once(h.ctx, "R", evidence)


def statuses(world: World, *runs: uuid.UUID) -> list[RunStatus]:
    return [world.row(run).status for run in runs]


# --- a write outage (S1, S2) ----------------------------------------------------------------


async def test_a_write_outage_stops_claiming_and_the_tripping_run_resumes_after_recovery(
    world: World, async_runtime_engine: AsyncEngine
) -> None:
    first, tripping, waiting = approved(world, 3), approved(world, 2), approved(world, 1)
    sends = [created_result(payload_of(world, first)), *[UNAVAILABLE] * 3]
    sends += [
        created_result(payload_of(world, waiting)),
        created_result(payload_of(world, tripping)),
    ]
    h = ready(
        Harness(
            async_runtime_engine,
            client=ScriptedRefundClient(sends=sends, then_config=FITS),
            production_breakers=True,
        )
    )

    assert await cycle(h) is Ended.COMPLETED, "the start-up probe succeeds and closes it"
    assert await cycle(h) is Ended.RELEASED, "three 503s open it; the run stops between attempts"
    row = world.row(tripping)
    assert (row.status, row.send_attempts, row.worker_id, row.release_streak) == (
        RunStatus.EXECUTING,
        3,
        None,
        0,
    )
    assert world.seconds_until(tripping, "claimable_after") > 0, "released with the cooldown"
    assert await cycle(h) is None, "nothing is claimed while it is open"
    assert world.row(waiting).claim_version == 0 and world.row(waiting).status is RunStatus.APPROVED

    h.clock.advance(OPEN)
    assert await cycle(h) is Ended.COMPLETED, "the probe: the waiting approval, not the cooled run"
    world.end_cooldown(tripping)
    assert await cycle(h) is Ended.COMPLETED
    row = world.row(tripping)
    assert (row.refund_outcome, row.send_attempts) == (RefundOutcome.CONFIRMED, 4)
    assert len(h.client.sent) == 6


async def test_negative_control_without_the_breaker_the_outage_reconciles_the_run(
    world: World, async_runtime_engine: AsyncEngine
) -> None:
    first, tripping = approved(world, 3), approved(world, 2)
    sends = [created_result(payload_of(world, first)), *[UNAVAILABLE] * 5]
    h = ready(
        Harness(async_runtime_engine, client=ScriptedRefundClient(sends=sends, then_config=FITS))
    )
    assert await cycle(h) is Ended.COMPLETED
    assert await cycle(h) is Ended.RECONCILING, "retries exhausted: on to rejected/ESCALATED"
    assert world.row(tripping).status is RunStatus.NEEDS_RECONCILIATION


# --- each path judged by its own traffic (S7) -----------------------------------------------


def open_write_breaker(h: Harness) -> None:
    h.ctx.write_breaker.take_probe()
    h.ctx.write_breaker.record(Health.FAILURE)


async def test_during_a_write_outage_lookups_keep_confirming_and_never_reopen_writes(
    world: World, async_runtime_engine: AsyncEngine
) -> None:
    uncertain = world.add_uncertain()
    client = Lookups(
        {payload_of(world, uncertain).action_id: lookup_result(payload_of(world, uncertain))}
    )
    h = ready(Harness(async_runtime_engine, client=client, production_breakers=True))
    open_write_breaker(h)
    before = h.ctx.write_breaker.next_open_for()
    assert await reconcile_pass(h, evidence_of(h)) is Reconciled.CONFIRMED
    assert state(h.ctx.write_breaker) is BreakerState.OPEN
    assert h.ctx.write_breaker.next_open_for() == before, "its backoff wasn't reset"


async def test_negative_control_one_shared_breaker_lets_a_lookup_close_the_write_path(
    world: World, async_runtime_engine: AsyncEngine
) -> None:
    uncertain = world.add_uncertain()
    client = Lookups(
        {payload_of(world, uncertain).action_id: lookup_result(payload_of(world, uncertain))}
    )
    h = ready(Harness(async_runtime_engine, client=client, production_breakers=True))
    h.ctx = replace(h.ctx, read_breaker=h.ctx.write_breaker)
    open_write_breaker(h)
    h.clock.advance(OPEN)  # half-open: the lookup is the probe
    assert await reconcile_pass(h, evidence_of(h)) is Reconciled.CONFIRMED
    assert state(h.ctx.write_breaker) is BreakerState.CLOSED


async def read_outage_with_sends_succeeding(
    world: World, h: Harness, evidence: LookupEvidence
) -> uuid.UUID:
    """A lone uncertain run's lookups fail twice; between them a send succeeds."""
    lone = world.add_uncertain(last_attempt_ago=600)
    assert isinstance(h.client, Lookups)
    h.client.answers[payload_of(world, lone).action_id] = UNAVAILABLE
    assert await reconcile_pass(h, evidence) is Reconciled.PROVIDER_FAILED
    assert await cycle(h) is Ended.COMPLETED, "sends keep completing"
    h.clock.advance(OPEN)
    world.end_cooldown(lone)
    assert await reconcile_pass(h, evidence) is Reconciled.PROVIDER_FAILED
    return lone


def sending_harness(world: World, engine: AsyncEngine) -> Harness:
    sent = approved(world, 1)
    client = Lookups(sends=[created_result(payload_of(world, sent))])
    return ready(Harness(engine, client=client, production_breakers=True))


async def test_during_a_read_outage_a_201_is_no_evidence_against_a_lone_run(
    world: World, async_runtime_engine: AsyncEngine
) -> None:
    h = sending_harness(world, async_runtime_engine)
    lone = await read_outage_with_sends_succeeding(world, h, evidence_of(h))
    assert world.row(lone).release_streak == 0


async def test_negative_control_counting_201s_as_evidence_streaks_the_lone_run(
    world: World, async_runtime_engine: AsyncEngine
) -> None:
    h = sending_harness(world, async_runtime_engine)
    both = LookupEvidence(lambda: h.ctx.read_breaker.successes + h.ctx.write_breaker.successes)
    lone = await read_outage_with_sends_succeeding(world, h, both)
    assert world.row(lone).release_streak == 1


# --- which run probes (S3) ------------------------------------------------------------------


async def test_a_doomed_run_is_probed_before_a_fresh_approval(
    world: World, async_runtime_engine: AsyncEngine
) -> None:
    """10 s of its window left before its last send, under the 30 s a failed probe would cost:
    it can't be saved by waiting, while a fresh approval would start a window of its own. A
    healthy EXECUTING run approved earlier is what ADR-014's order would take."""
    earlier_approved(world)
    doomed = world.add_executing(sent_seconds_ago=50, window_ms=68_000, send_attempts=1)
    approved(world, 1)
    h = ready(
        Harness(
            async_runtime_engine,
            client=ScriptedRefundClient(then_config=FITS),
            production_breakers=True,
        )
    )
    claimed = await claim_next(h.ctx, "W")
    assert claimed is not None and claimed.run_id == doomed


async def test_negative_control_the_closed_order_probes_the_healthy_executing_run(
    world: World, async_runtime_engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    healthy = earlier_approved(world)
    world.add_executing(sent_seconds_ago=50, window_ms=68_000, send_attempts=1)

    def closed_order(r: FromClause, probe: ProbeOrder) -> list[ColumnElement[Any]]:
        return [(r.c.status == RunStatus.EXECUTING).desc(), r.c.approved_at, r.c.id]

    monkeypatch.setattr(claims, "probe_order", closed_order)
    h = ready(
        Harness(
            async_runtime_engine,
            client=ScriptedRefundClient(then_config=FITS),
            production_breakers=True,
        )
    )
    claimed = await claim_next(h.ctx, "W")
    assert claimed is not None and claimed.run_id == healthy


async def test_negative_control_approved_first_probing_starts_a_fresh_window(
    world: World, async_runtime_engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    world.add_executing(sent_seconds_ago=50, window_ms=68_000, send_attempts=1)
    fresh = approved(world, 1)

    def approved_first(r: FromClause, probe: ProbeOrder) -> list[ColumnElement[Any]]:
        return [(r.c.status == RunStatus.APPROVED).desc(), r.c.send_attempts, r.c.id]

    monkeypatch.setattr(claims, "probe_order", approved_first)
    h = ready(
        Harness(
            async_runtime_engine,
            client=ScriptedRefundClient(then_config=FITS),
            production_breakers=True,
        )
    )
    claimed = await claim_next(h.ctx, "W")
    assert claimed is not None and claimed.run_id == fresh


SHORT_BACKOFF = replace(
    TIMINGS, provider_open=timedelta(seconds=1), provider_max_open=timedelta(seconds=4)
)


async def probe_through_a_long_outage(world: World, engine: AsyncEngine) -> list[uuid.UUID]:
    """Six failed probes, time passing between them (cooldowns ended, the breaker re-opening)."""
    released = world.add_executing(sent_seconds_ago=1, send_attempts=1)
    fresh = [approved(world, hours) for hours in (3, 2, 1)]
    h = ready(
        Harness(
            engine,
            client=ScriptedRefundClient(sends=[UNAVAILABLE] * 6, then_config=FITS),
            timings=SHORT_BACKOFF,
            production_breakers=True,
        )
    )
    for _ in range(6):
        await cycle(h)
        for run in (released, *fresh):
            world.end_cooldown(run)
        h.clock.advance(5)
    return [released, *fresh]


async def test_a_long_outage_never_drains_one_run_by_probing_alone(
    world: World, async_runtime_engine: AsyncEngine
) -> None:
    runs = await probe_through_a_long_outage(world, async_runtime_engine)
    assert statuses(world, *runs) == [RunStatus.EXECUTING] * 4, "none reconciled"
    assert max(world.row(run).send_attempts for run in runs) <= 2, "probes spread over the runs"


async def test_negative_control_executing_first_probing_reconciles_the_released_run(
    world: World, async_runtime_engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    def executing_first(r: FromClause, probe: ProbeOrder) -> list[ColumnElement[Any]]:
        return [(r.c.status == RunStatus.EXECUTING).desc(), r.c.approved_at, r.c.id]

    monkeypatch.setattr(claims, "probe_order", executing_first)
    released, *_ = await probe_through_a_long_outage(world, async_runtime_engine)
    assert world.row(released).status is RunStatus.NEEDS_RECONCILIATION


# --- a probe claim that sends nothing (S4) --------------------------------------------------


async def run_until(h: Harness, done: Any, seconds: float) -> bool:
    task = asyncio.create_task(run_worker(h.ctx, "W"))
    try:
        async with asyncio.timeout(seconds):
            while not await done():
                await asyncio.sleep(0.05)
        return True
    except TimeoutError:
        return False
    finally:
        h.ctx.stopping.set()
        async with asyncio.timeout(BOUND):
            await task


async def probe_with_a_run_that_never_sends(
    world: World, engine: AsyncEngine, seconds: float
) -> tuple[uuid.UUID, bool]:
    """The start-up probe claim goes to an approval refused by policy (nothing sent); a second
    approval is waiting."""
    world.add_refund_run(
        world.add_order(charged_minor=8_000),
        9_000,
        approved_at=datetime.now(UTC) - timedelta(hours=2),
    )
    waiting = approved(world, 1)
    h = ready(
        Harness(
            engine,
            client=ScriptedRefundClient(
                sends=[created_result(payload_of(world, waiting))], then_config=FITS
            ),
            production_breakers=True,
        )
    )

    async def completed() -> bool:
        return (await asyncio.to_thread(world.row, waiting)).status is RunStatus.COMPLETED

    return waiting, await run_until(h, completed, seconds)


async def test_a_probe_claim_that_sends_nothing_frees_the_slot(
    world: World, async_runtime_engine: AsyncEngine
) -> None:
    _, completed = await probe_with_a_run_that_never_sends(world, async_runtime_engine, BOUND)
    assert completed, "the next claim probed"


async def test_negative_control_keeping_the_slot_leaves_it_half_open_with_no_probe(
    world: World, async_runtime_engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(ProviderBreaker, "release_probe", lambda self: None)
    waiting, completed = await probe_with_a_run_that_never_sends(world, async_runtime_engine, 1.5)
    assert not completed
    assert world.row(waiting).status is RunStatus.APPROVED


# --- infrastructure errors count (S6) -------------------------------------------------------


async def misrouted(
    world: World, engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch | None
) -> list[uuid.UUID]:
    """After a good send, refund-api's URL starts answering 404 for everything."""
    first = approved(world, 9)
    later = [approved(world, hours) for hours in (5, 4, 3, 2)]
    h = ready(
        Harness(
            engine,
            client=ScriptedRefundClient(
                sends=[created_result(payload_of(world, first)), *[MISROUTED] * 4], then_config=FITS
            ),
            production_breakers=True,
        )
    )
    if monkeypatch is not None:
        monkeypatch.setattr(execute, "send_health", lambda result: Health.NEUTRAL)
    for _ in range(6):
        await cycle(h)
    return later


async def test_a_404_for_everything_opens_the_breaker_after_the_threshold(
    world: World, async_runtime_engine: AsyncEngine
) -> None:
    later = await misrouted(world, async_runtime_engine, None)
    assert statuses(world, *later) == [RunStatus.NEEDS_RECONCILIATION] * 3 + [RunStatus.APPROVED], (
        "up to a threshold's worth reconcile (S6's stated residual); then nothing more is sent"
    )


async def test_negative_control_not_counting_404s_reconciles_every_run(
    world: World, async_runtime_engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    later = await misrouted(world, async_runtime_engine, monkeypatch)
    assert statuses(world, *later) == [RunStatus.NEEDS_RECONCILIATION] * 4


# --- config reads feed no breaker -----------------------------------------------------------


async def test_a_failed_config_read_closes_the_gate_but_not_the_breaker(
    world: World, async_runtime_engine: AsyncEngine
) -> None:
    approved(world, 1)
    h = ready(
        Harness(async_runtime_engine, configs=[HttpResult(status=500)], production_breakers=True)
    )
    assert await cycle(h) is Ended.RELEASED
    assert h.ctx.gate.state is GateState.READ_FAILED
    assert state(h.ctx.write_breaker) is BreakerState.HALF_OPEN and h.ctx.write_breaker.probing()


async def test_negative_control_feeding_config_reads_to_the_breaker_opens_it(
    world: World, async_runtime_engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    approved(world, 1)
    h = ready(
        Harness(async_runtime_engine, configs=[HttpResult(status=500)], production_breakers=True)
    )
    original = gate_module.read_config

    async def fed(client: Any, settings: Any) -> Any:
        read = await original(client, settings)
        h.ctx.write_breaker.record(Health.FAILURE)
        return read

    monkeypatch.setattr(presend, "read_config", fed)
    await cycle(h)
    assert state(h.ctx.write_breaker) is BreakerState.OPEN


# --- stale pages during a read outage (S9) --------------------------------------------------


async def read_outage_past_the_page_threshold(
    world: World, engine: AsyncEngine
) -> tuple[Harness, uuid.UUID, list[uuid.UUID]]:
    """One run fails on its own (a mismatched row); then ten runs' lookups fail and open the read
    breaker; two more runs become uncertain during the outage and are never looked up. All of
    them are then past the stale page threshold, and the outage past the breaker's page."""
    own = world.add_uncertain(last_attempt_ago=900)
    ten = [world.add_uncertain(last_attempt_ago=600 - n) for n in range(10)]
    payload = payload_of(world, own)
    foreign = payload.model_copy(update={"amount_minor": payload.amount_minor + 1})
    client = Lookups({payload.action_id: lookup_result(foreign)})
    client.answers.update({payload_of(world, run).action_id: UNAVAILABLE for run in ten})
    h = ready(Harness(engine, client=client, production_breakers=True))
    evidence = evidence_of(h)
    assert await reconcile_pass(h, evidence) is Reconciled.RUN_FAILED
    for _ in range(3):
        assert await reconcile_pass(h, evidence) is Reconciled.PROVIDER_FAILED
    assert state(h.ctx.read_breaker) is BreakerState.OPEN
    never_looked_up = [world.add_uncertain(last_attempt_ago=60) for _ in range(2)]
    for run in (own, *ten, *never_looked_up):
        world.set(run, escalated_at=ago(PAGE_AFTER + 60))
    h.clock.advance(TIMINGS.provider_page_after.total_seconds() + 1)
    h.alerter.raised.clear()
    await reconcile_pass(h, evidence)
    return h, own, never_looked_up


def stale_pages(h: Harness) -> list[uuid.UUID | None]:
    return [
        r.run_id
        for r in h.alerter.raised
        if r.severity is Severity.PAGE and r.kind == "reconcile_stale_unknown"
    ]


async def test_a_read_outage_pages_once_for_the_breaker_and_per_run_only_for_own_failures(
    world: World, async_runtime_engine: AsyncEngine
) -> None:
    h, own, _ = await read_outage_past_the_page_threshold(world, async_runtime_engine)
    assert stale_pages(h) == [own]
    assert h.alerter.kinds(Severity.PAGE).count("provider_read_open") == 1
    alerted = [
        r
        for r in h.alerter.raised
        if r.severity is Severity.ALERT and r.kind == "reconcile_stale_unknown"
    ]
    assert len(alerted) == 13, "every run's alert still fires"


async def test_negative_control_skipping_only_marked_runs_pages_for_runs_never_looked_up(
    world: World, async_runtime_engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        LookupEvidence, "last_failed_on_its_own", lambda self, run_id: run_id not in self._failed_at
    )
    h, _, never_looked_up = await read_outage_past_the_page_threshold(world, async_runtime_engine)
    assert set(never_looked_up) <= set(stale_pages(h))


async def test_negative_control_skipping_every_run_silences_the_run_failing_on_its_own(
    world: World, async_runtime_engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(LookupEvidence, "last_failed_on_its_own", lambda self, run_id: False)
    h, own, _ = await read_outage_past_the_page_threshold(world, async_runtime_engine)
    assert own not in stale_pages(h)


def test_the_default_harness_breakers_never_open() -> None:
    """ADR-014's and ADR-016's tests rely on this; it is no production configuration."""
    breaker = never_opening("write", RecordingAlerter())
    for _ in range(100):
        breaker.record(Health.FAILURE)
    assert state(breaker) is BreakerState.CLOSED


async def test_a_read_probe_claim_that_makes_no_lookup_frees_the_slot(
    world: World, async_runtime_engine: AsyncEngine
) -> None:
    unreadable = world.add_uncertain(last_attempt_ago=900, canonical_payload="not a payload")
    readable = world.add_uncertain(last_attempt_ago=600)
    client = Lookups(
        {payload_of(world, readable).action_id: lookup_result(payload_of(world, readable))}
    )
    h = ready(Harness(async_runtime_engine, client=client, production_breakers=True))
    evidence = evidence_of(h)
    assert await reconcile_pass(h, evidence) is Reconciled.RUN_FAILED, "no lookup was made"
    assert world.row(unreadable).release_streak == 1
    assert await reconcile_pass(h, evidence) is Reconciled.CONFIRMED, "the next claim probed"
    assert state(h.ctx.read_breaker) is BreakerState.CLOSED


async def test_the_send_loop_pages_once_a_write_outage_outlasts_the_page_threshold(
    world: World, async_runtime_engine: AsyncEngine
) -> None:
    h = Harness(
        async_runtime_engine,
        client=ScriptedRefundClient(then_config=FITS),
        production_breakers=True,
    )
    open_write_breaker(h)
    h.clock.advance(TIMINGS.provider_page_after.total_seconds() + 1)

    async def paged() -> bool:
        return "provider_write_open" in h.alerter.kinds(Severity.PAGE)

    assert await run_until(h, paged, BOUND)
