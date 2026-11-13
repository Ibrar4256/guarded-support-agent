"""The reconcile loop (ADR-008 Part 2, ADR-010, ADR-016, ADR-017). Every check has a negative
control; every wait is bounded; the reconciler never sends (checked after every pass)."""

import asyncio
import logging
import uuid
from collections.abc import Callable
from dataclasses import replace
from datetime import timedelta
from typing import Any

import pytest
from agent_testing.fakes import config_result, created_result, lookup_result
from sqlalchemy import ColumnElement, FromClause, func, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine
from worker_harness import READ_WINDOW_MS, RETRY, TIMINGS, Harness, Lookups
from worker_world import World

from agent.alerts import LogAlerter, Severity
from agent.core.capacity import RefundRecord, used_on_order
from agent.core.lifecycle import WORKER_CLAIMABLE, RefundOutcome, RunStatus
from agent.core.refund_outcome import HttpResult, LookupKind, LookupOutcome
from agent.core.replies import ReplyTemplate
from agent.db import claims, execution
from agent.db import reconcile as reconcile_db
from agent.db.claims import Claim
from agent.db.schema import runs
from agent.refund_client.models import RefundPayload
from agent.worker import gate, reconcile
from agent.worker.gate import GateState
from agent.worker.hooks import NO_FAULTS, WorkerHooks
from agent.worker.loop import Ended, process_claim, run_worker
from agent.worker.reconcile import LookupEvidence, Reconciled, reconcile_once
from agent.worker.timings import WorkerTimings

BOUND = 5.0
BUDGET = timedelta(seconds=5)  # FITS reports 5 s
MARGIN = TIMINGS.quiet_margin  # 2 s
NOTHING = lookup_result()
UNAVAILABLE = HttpResult(status=500)


class Reconciler:
    def __init__(
        self,
        engine: AsyncEngine,
        client: Lookups,
        *,
        timings: WorkerTimings = TIMINGS,
        hooks: WorkerHooks = NO_FAULTS,
    ) -> None:
        self.client = client
        self.h = Harness(engine, client=client, timings=timings, hooks=hooks)
        self.evidence = LookupEvidence(lambda: self.h.ctx.read_breaker.successes)

    async def run_pass(self) -> Reconciled | None:
        sent_before = len(self.client.sent)
        async with asyncio.timeout(BOUND):
            step = await reconcile_once(self.h.ctx, "R", self.evidence)
        assert len(self.client.sent) == sent_before, "the reconciler never sends (ADR-008 Part 2)"
        return step

    def raised(self, severity: Severity, kind: str) -> list[Any]:
        return [r for r in self.h.alerter.raised if r.severity is severity and r.kind == kind]

    def logging_alerts(self) -> None:
        """Alerts through the real, rate-limited alerter (one line per key per interval)."""
        self.h.ctx = replace(self.h.ctx, alerter=LogAlerter(timedelta(seconds=300)))


@pytest.fixture
def make(async_runtime_engine: AsyncEngine) -> Callable[..., Reconciler]:
    def factory(client: Lookups | None = None, **kwargs: Any) -> Reconciler:
        return Reconciler(async_runtime_engine, client or Lookups(), **kwargs)

    return factory


def payload_of(world: World, run: uuid.UUID) -> RefundPayload:
    return RefundPayload.model_validate_json(world.row(run).canonical_payload)


def action_of(world: World, run: uuid.UUID) -> str:
    return str(world.row(run).action_id)


def found(world: World, run: uuid.UUID) -> HttpResult:
    return lookup_result(payload_of(world, run))


def ago(seconds: float) -> Any:
    return text(f"now() - interval '{seconds} seconds'")


async def claim(
    engine: AsyncEngine, *, budget: timedelta = BUDGET, margin: timedelta = MARGIN
) -> Claim | None:
    async with claims.worker_transaction(
        engine, statement_timeout=TIMINGS.statement_timeout, lock_timeout=TIMINGS.lock_timeout
    ) as conn:
        return await reconcile_db.claim_for_reconcile(
            conn, "R", TIMINGS.lease, budget_now=budget, margin=margin
        )


Conditions = Callable[[FromClause, timedelta, timedelta], dict[str, ColumnElement[bool]]]


def without(condition: str) -> Conditions:
    original = reconcile_db.reconcile_conditions

    def planted(r: FromClause, budget: timedelta, margin: timedelta) -> dict[str, Any]:
        kept = {k: v for k, v in original(r, budget, margin).items() if k != condition}
        assert len(kept) == len(original(r, budget, margin)) - 1, f"no condition {condition}"
        return kept

    return planted


# --- the claim (S1) and its quiet period (Part 2) ------------------------------------------


def test_reconcile_conditions_are_the_four_adr016_conditions() -> None:
    conditions = reconcile_db.reconcile_conditions(runs, BUDGET, MARGIN)
    assert set(conditions) == {"status", "lease_free", "cooldown_passed", "quiet_passed"}


def only_failing(world: World, condition: str) -> uuid.UUID:
    """A run that fails exactly one reconcile condition."""
    if condition == "status":
        return world.add_executing(sent_seconds_ago=120, send_attempts=1)
    if condition == "quiet_passed":
        return world.add_uncertain(last_attempt_ago=1)
    run = world.add_uncertain()
    if condition == "lease_free":
        world.set(run, worker_id="other", lease_expires_at=text("now() + interval '30 seconds'"))
    else:
        world.set(run, claimable_after=text("now() + interval '60 seconds'"))
    return run


CONDITIONS = ["status", "lease_free", "cooldown_passed", "quiet_passed"]


@pytest.mark.parametrize("condition", CONDITIONS)
async def test_a_run_failing_one_condition_is_not_claimed(
    world: World, async_runtime_engine: AsyncEngine, condition: str
) -> None:
    only_failing(world, condition)
    assert await claim(async_runtime_engine) is None


@pytest.mark.parametrize("condition", CONDITIONS)
async def test_negative_control_without_that_condition_it_is_claimed(
    world: World,
    async_runtime_engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
    condition: str,
) -> None:
    run = only_failing(world, condition)
    monkeypatch.setattr(reconcile_db, "reconcile_conditions", without(condition))
    claimed = await claim(async_runtime_engine)
    assert claimed is not None and claimed.run_id == run


async def test_a_run_inside_its_quiet_period_waits_and_after_it_is_claimed(
    world: World, async_runtime_engine: AsyncEngine
) -> None:
    run = world.add_uncertain(last_attempt_ago=12)  # bound 13 s + margin 2 s
    assert await claim(async_runtime_engine) is None
    world.set(run, last_attempt_at=ago(16))
    claimed = await claim(async_runtime_engine)
    assert claimed is not None
    assert (claimed.run_id, claimed.status) == (run, RunStatus.NEEDS_RECONCILIATION)
    row = world.row(run)
    assert (row.worker_id, row.claim_version) == ("R", 1)


async def test_the_oldest_last_attempt_is_claimed_first(
    world: World, async_runtime_engine: AsyncEngine
) -> None:
    world.add_uncertain(last_attempt_ago=100)
    oldest = world.add_uncertain(last_attempt_ago=300)
    claimed = await claim(async_runtime_engine)
    assert claimed is not None and claimed.run_id == oldest


async def test_the_recorded_bound_is_the_largest_over_the_sends(
    world: World, async_runtime_engine: AsyncEngine
) -> None:
    """Two sends: the first under a 20 s budget, the last under 5 s. The run waits for the
    first's bound (8 + 20 s), not the last's (8 + 5 s)."""
    run = world.add_executing(sent_seconds_ago=60)
    h = Harness(async_runtime_engine)
    claimed = await h.claim()
    timeout = RETRY.client_timeout
    async with claims.worker_transaction(
        async_runtime_engine,
        statement_timeout=TIMINGS.statement_timeout,
        lock_timeout=TIMINGS.lock_timeout,
    ) as conn:
        await execution.record_attempt(conn, claimed, timeout, timedelta(seconds=20))
        await execution.record_attempt(conn, claimed, timeout, timedelta(seconds=5))
        assert await execution.to_reconciliation(conn, claimed)
    assert (world.row(run).in_flight_bound_ms, world.row(run).max_client_timeout_ms) == (
        28_000,
        8_000,
    )
    world.set(run, last_attempt_at=ago(20))  # past the last send's 13 + 2 s
    assert await claim(async_runtime_engine) is None
    world.set(run, last_attempt_at=ago(31))  # past 28 + 2 s
    reclaimed = await claim(async_runtime_engine)
    assert reclaimed is not None and reclaimed.run_id == run


class Redeploy(WorkerHooks):
    """refund-api is redeployed with a larger budget between the worker's config read and its
    send: the request runs under a budget the worker never read."""

    def __init__(self, client: Lookups, raised: HttpResult) -> None:
        self.client, self.raised = client, raised

    async def before_refund_call(self, run_id: uuid.UUID) -> None:
        self.client.then_config = self.raised


# Two attempts so ADR-018 S1 holds; the one send is answered 422, which reconciles at once.
ONE_SHOT = replace(TIMINGS, policy=replace(RETRY, attempts=2), provider_failure_threshold=1)
UNKNOWN_OUTCOME = HttpResult(status=422)
RAISED = config_result(window_ms=READ_WINDOW_MS, retention_ms=3_600_000, budget_ms=20_000)


async def send_once_then_wait(world: World, r: Reconciler) -> uuid.UUID:
    """One send under the 5 s budget read before it, answered 422 (outcome unknown), so the run
    needs reconciliation; then 16 s pass: the recorded bound (8 + 5 + 2 s) is over, the new budget's
    (8 + 20 + 2 s) isn't."""
    run = world.add_executing(sent_seconds_ago=1)
    claimed = await r.h.claim()
    async with asyncio.timeout(BOUND):
        assert await process_claim(r.h.ctx, claimed) is Ended.RECONCILING
    row = world.row(run)
    assert (row.status, row.in_flight_bound_ms) == (RunStatus.NEEDS_RECONCILIATION, 13_000)
    world.set(run, last_attempt_at=ago(16))
    return run


async def test_a_budget_raised_between_the_read_and_the_send_is_waited_for(
    world: World, make: Callable[..., Reconciler]
) -> None:
    client = Lookups(sends=[UNKNOWN_OUTCOME])
    r = make(client, timings=ONE_SHOT, hooks=Redeploy(client, RAISED))
    run = await send_once_then_wait(world, r)
    assert await r.run_pass() is None, "the fresh read's 20 s budget keeps it waiting"
    assert world.row(run).status is RunStatus.NEEDS_RECONCILIATION
    assert client.lookups == []


async def test_negative_control_the_recorded_bound_alone_claims_it_early(
    world: World, make: Callable[..., Reconciler], monkeypatch: pytest.MonkeyPatch
) -> None:
    client = Lookups(sends=[UNKNOWN_OUTCOME])
    r = make(client, timings=ONE_SHOT, hooks=Redeploy(client, RAISED))
    run = await send_once_then_wait(world, r)
    monkeypatch.setattr(
        reconcile_db, "quiet_millis", lambda r, budget: func.coalesce(r.c.in_flight_bound_ms, 0)
    )
    client.answers[action_of(world, run)] = NOTHING  # nothing has committed... yet
    assert await r.run_pass() is Reconciled.REJECTED, "rejected while the send may still commit"


async def test_a_budget_lowered_after_the_send_still_waits_for_the_recorded_bound(
    world: World, async_runtime_engine: AsyncEngine
) -> None:
    world.add_uncertain(last_attempt_ago=20, in_flight_bound_ms=28_000)  # sent under 20 s
    assert await claim(async_runtime_engine) is None  # 28 + 2 s, though budget_now is 5 s


async def test_negative_control_the_fresh_read_alone_claims_it_early(
    world: World, async_runtime_engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    run = world.add_uncertain(last_attempt_ago=20, in_flight_bound_ms=28_000)
    monkeypatch.setattr(
        reconcile_db,
        "quiet_millis",
        lambda r, budget: r.c.max_client_timeout_ms + budget // timedelta(milliseconds=1),
    )
    claimed = await claim(async_runtime_engine)
    assert claimed is not None and claimed.run_id == run


async def test_a_client_timeout_lowered_in_a_later_deploy_is_still_covered(
    world: World, async_runtime_engine: AsyncEngine
) -> None:
    """Sent under an 8 s timeout and a 5 s budget; refund-api now reports 10 s. The recorded
    timeout gives 8 + 10 + 2 = 20 s; today's 2 s timeout would give 15 s."""
    world.add_uncertain(last_attempt_ago=16)
    assert await claim(async_runtime_engine, budget=timedelta(seconds=10)) is None


async def test_negative_control_the_current_timeout_claims_it_early(
    world: World, async_runtime_engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    run = world.add_uncertain(last_attempt_ago=16)
    current_timeout_ms = 2_000
    monkeypatch.setattr(
        reconcile_db,
        "quiet_millis",
        lambda r, budget: func.greatest(
            r.c.in_flight_bound_ms, current_timeout_ms + budget // timedelta(milliseconds=1)
        ),
    )
    claimed = await claim(async_runtime_engine, budget=timedelta(seconds=10))
    assert claimed is not None and claimed.run_id == run


async def test_a_run_with_no_recorded_send_waits_only_the_margin(
    world: World, async_runtime_engine: AsyncEngine
) -> None:
    world.add_uncertain(send_attempts=0, last_attempt_ago=1)
    assert await claim(async_runtime_engine) is None
    unsent = world.add_uncertain(send_attempts=0, last_attempt_ago=3)
    claimed = await claim(async_runtime_engine)
    assert claimed is not None and claimed.run_id == unsent


async def test_negative_control_adding_the_budget_to_an_unsent_run_delays_it(
    world: World, async_runtime_engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    world.add_uncertain(send_attempts=0, last_attempt_ago=3)
    monkeypatch.setattr(
        reconcile_db,
        "quiet_millis",
        lambda r, budget: func.coalesce(
            r.c.in_flight_bound_ms, budget // timedelta(milliseconds=1)
        ),
    )
    assert await claim(async_runtime_engine) is None


# --- outcomes (S3) --------------------------------------------------------------------------


async def test_found_confirms_with_the_issued_reply(
    world: World, make: Callable[..., Reconciler]
) -> None:
    run = world.add_uncertain()
    payload = payload_of(world, run)
    r = make(Lookups({payload.action_id: found(world, run)}))
    assert await r.run_pass() is Reconciled.CONFIRMED
    row = world.row(run)
    assert (row.status, row.refund_outcome) == (RunStatus.COMPLETED, RefundOutcome.CONFIRMED)
    assert (row.worker_id, row.lease_expires_at, row.claim_version) == (None, None, 2)
    [event] = world.events(run)
    assert (event.kind, event.payload) == (
        "reply",
        {
            "template": ReplyTemplate.REFUND_ISSUED.value,
            "order_id": payload.order_id,
            "amount_minor": payload.amount_minor,
            "currency": payload.currency,
        },
    )
    assert r.raised(Severity.ALERT, "duplicate_refunds") == []


async def test_two_refunds_confirm_and_alert(world: World, make: Callable[..., Reconciler]) -> None:
    run = world.add_uncertain()
    payload = payload_of(world, run)
    twice = lookup_result(payload, payload, ids=("re_1", "re_2"))
    r = make(Lookups({payload.action_id: twice}))
    assert await r.run_pass() is Reconciled.CONFIRMED
    [duplicate] = r.raised(Severity.ALERT, "duplicate_refunds")
    assert (duplicate.run_id, duplicate.fields["refund_ids"]) == (run, ["re_1", "re_2"])


async def test_negative_control_found_read_as_not_found_rejects(
    world: World, make: Callable[..., Reconciler], monkeypatch: pytest.MonkeyPatch
) -> None:
    run = world.add_uncertain()
    r = make(Lookups({action_of(world, run): found(world, run)}))
    monkeypatch.setattr(
        reconcile,
        "classify_lookup",
        lambda result, sent: LookupOutcome(LookupKind.NOT_FOUND, "planted"),
    )
    assert await r.run_pass() is Reconciled.REJECTED
    assert world.row(run).refund_outcome is RefundOutcome.REJECTED


def capacity_used(world: World, run: uuid.UUID) -> int:
    row = world.row(run)
    record = RefundRecord(
        action_id=row.action_id,
        order_id=row.order_id,
        amount_minor=row.amount_minor,
        currency=row.currency,
        status=row.status,
        outcome=row.refund_outcome,
        first_send_started_at=row.first_send_started_at,
        approval_source=row.approval_source,
        approved_at=row.approved_at,
    )
    return used_on_order(
        [record], order_id=row.order_id, exclude_action_id="", include_pending=True
    )


async def test_not_found_after_the_quiet_period_rejects_and_frees_the_capacity(
    world: World, make: Callable[..., Reconciler]
) -> None:
    run = world.add_uncertain(2_500)
    escalated_at = world.row(run).escalated_at
    assert capacity_used(world, run) == 2_500
    r = make(Lookups({action_of(world, run): NOTHING}))
    assert await r.run_pass() is Reconciled.REJECTED
    row = world.row(run)
    assert (row.status, row.refund_outcome) == (RunStatus.ESCALATED, RefundOutcome.REJECTED)
    assert row.escalated_at == escalated_at, "kept from when it first needed attention"
    assert (row.worker_id, row.claim_version, row.release_streak) == (None, 2, 0)
    [event] = world.events(run)
    assert (event.kind, event.payload) == (
        "escalated",
        {"template": ReplyTemplate.NOT_CONFIRMED.value},
    )
    assert capacity_used(world, run) == 0


async def test_a_claim_stolen_during_the_lookup_writes_nothing(
    world: World, make: Callable[..., Reconciler]
) -> None:
    run = world.add_uncertain()

    async def stolen_meanwhile() -> HttpResult:
        await asyncio.to_thread(world.steal, run, "other", timedelta(seconds=30))
        return NOTHING

    r = make(Lookups({action_of(world, run): stolen_meanwhile}))
    assert await r.run_pass() is Reconciled.LOST
    row = world.row(run)
    assert (row.status, row.worker_id, row.refund_outcome) == (
        RunStatus.NEEDS_RECONCILIATION,
        "other",
        RefundOutcome.UNKNOWN,
    )
    assert world.events(run) == []


FAST_BEAT = replace(TIMINGS, lease=timedelta(seconds=0.6), heartbeat=timedelta(seconds=0.1))


async def slow_answer(result: HttpResult) -> HttpResult:
    await asyncio.sleep(1.0)  # longer than the lease: only renewal keeps the claim
    return result


async def test_the_heartbeat_holds_a_reconcile_claim_through_a_slow_lookup(
    world: World, make: Callable[..., Reconciler]
) -> None:
    run = world.add_uncertain()
    r = make(
        Lookups({action_of(world, run): lambda: slow_answer(found(world, run))}), timings=FAST_BEAT
    )
    assert await r.run_pass() is Reconciled.CONFIRMED


async def test_negative_control_renewing_only_worker_statuses_loses_the_claim(
    world: World, make: Callable[..., Reconciler], monkeypatch: pytest.MonkeyPatch
) -> None:
    run = world.add_uncertain()
    monkeypatch.setattr(claims, "holdable", lambda claim: WORKER_CLAIMABLE)
    r = make(
        Lookups({action_of(world, run): lambda: slow_answer(found(world, run))}), timings=FAST_BEAT
    )
    assert await r.run_pass() is Reconciled.LOST
    assert world.row(run).status is RunStatus.NEEDS_RECONCILIATION


# --- the kill switch (Part 4) ---------------------------------------------------------------


async def test_with_the_kill_switch_on_not_found_is_held_until_it_is_off(
    world: World, make: Callable[..., Reconciler]
) -> None:
    world.controls(sends_enabled=False)
    run = world.add_uncertain()
    r = make(Lookups({action_of(world, run): NOTHING}))
    assert await r.run_pass() is Reconciled.HELD
    row = world.row(run)
    assert (row.status, row.refund_outcome, row.release_streak) == (
        RunStatus.NEEDS_RECONCILIATION,
        RefundOutcome.UNKNOWN,
        0,
    )
    assert (row.worker_id, row.claim_version) == (None, 2)
    assert world.seconds_until(run, "claimable_after") > TIMINGS.claim_cooldown.total_seconds() - 5
    assert world.events(run) == []
    assert r.raised(Severity.ALERT, "reconcile_held_by_kill_switch")

    world.controls(sends_enabled=True)
    world.end_cooldown(run)
    assert await r.run_pass() is Reconciled.REJECTED


async def test_negative_control_ignoring_the_switch_rejects_at_once(
    world: World, make: Callable[..., Reconciler], monkeypatch: pytest.MonkeyPatch
) -> None:
    world.controls(sends_enabled=False)
    run = world.add_uncertain()
    original = reconcile_db.lock_state

    async def switch_ignored(conn: Any, claim: Claim) -> Any:
        state = await original(conn, claim)
        return None if state is None else replace(state, sends_enabled=True)

    monkeypatch.setattr(reconcile_db, "lock_state", switch_ignored)
    r = make(Lookups({action_of(world, run): NOTHING}))
    assert await r.run_pass() is Reconciled.REJECTED


async def test_with_the_kill_switch_on_found_still_confirms(
    world: World, make: Callable[..., Reconciler]
) -> None:
    world.controls(sends_enabled=False)
    run = world.add_uncertain()
    r = make(Lookups({action_of(world, run): found(world, run)}))
    assert await r.run_pass() is Reconciled.CONFIRMED


# --- refund-api's failures and the run's (Part 3, ADR-017) ----------------------------------


async def test_an_outage_of_refund_apis_lookups_counts_toward_no_streak_and_pages_nobody(
    world: World, make: Callable[..., Reconciler]
) -> None:
    """refund-api's database is down: config reads succeed, every lookup fails. Ten uncertain
    runs, each failing twice: no streak, no page, one alert key (ADR-017)."""
    uncertain = [world.add_uncertain(last_attempt_ago=60 + n) for n in range(10)]
    r = make(Lookups({action_of(world, run): UNAVAILABLE for run in uncertain}))
    for _ in range(2):
        for _ in uncertain:
            assert await r.run_pass() is Reconciled.PROVIDER_FAILED
            assert r.h.ctx.gate.is_open(), "a lookup failure never stops the send loop (ADR-018)"
        assert await r.run_pass() is None, "every run is in its cooldown"
        for run in uncertain:
            world.end_cooldown(run)
    assert len(r.client.lookups) == 20
    assert [world.row(run).release_streak for run in uncertain] == [0] * 10
    assert r.h.alerter.kinds(Severity.PAGE) == []
    failed = r.raised(Severity.ALERT, "reconcile_lookup_failed")
    assert len(failed) == 20 and {a.run_id for a in failed} == {None}, "one key for all runs"


async def test_negative_control_adr016s_config_read_evidence_counts_the_outage(
    world: World, make: Callable[..., Reconciler], monkeypatch: pytest.MonkeyPatch
) -> None:
    def config_read_in_between(self: LookupEvidence, run_id: uuid.UUID) -> bool:
        # Every claim follows a successful config read, so under ADR-016 any repeat counts.
        before = run_id in self._failed_at
        self._failed_at[run_id] = self._answered()
        return before

    monkeypatch.setattr(LookupEvidence, "provider_failure_counts", config_read_in_between)
    uncertain = [world.add_uncertain(last_attempt_ago=60 + n) for n in range(3)]
    r = make(Lookups({action_of(world, run): UNAVAILABLE for run in uncertain}))
    for _ in range(2):
        for _ in uncertain:
            assert await r.run_pass() is Reconciled.PROVIDER_FAILED
        for run in uncertain:
            world.end_cooldown(run)
    assert [world.row(run).release_streak for run in uncertain] == [1, 1, 1]


async def test_one_failing_action_pages_once_the_others_are_answered_in_between(
    world: World, make: Callable[..., Reconciler]
) -> None:
    """refund-api fails one action's lookup only. Each repeat after another run's lookup was
    answered counts; at the limit (3) the run pages, keeping its status."""
    failing = world.add_uncertain(last_attempt_ago=600)
    r = make(Lookups({action_of(world, failing): UNAVAILABLE}))
    assert await r.run_pass() is Reconciled.PROVIDER_FAILED  # a first failure never counts
    for streak in (1, 2, 3):
        other = world.add_uncertain()
        r.client.answers[action_of(world, other)] = found(world, other)
        assert await r.run_pass() is Reconciled.CONFIRMED, "the cooldown let the others through"
        world.end_cooldown(failing)
        assert await r.run_pass() is Reconciled.PROVIDER_FAILED
        assert world.row(failing).release_streak == streak
    row = world.row(failing)
    assert (row.status, row.refund_outcome) == (
        RunStatus.NEEDS_RECONCILIATION,
        RefundOutcome.UNKNOWN,
    )
    [page] = r.raised(Severity.PAGE, "release_streak_limit")
    assert (page.run_id, page.fields["status"]) == (failing, "NEEDS_RECONCILIATION")


async def test_negative_control_a_release_without_the_cooldown_reclaims_it_in_a_loop(
    world: World, make: Callable[..., Reconciler], monkeypatch: pytest.MonkeyPatch
) -> None:
    original = claims.release_values

    def no_cooldown(cooldown: timedelta | None, **kwargs: Any) -> dict[str, Any]:
        values = original(cooldown, **kwargs)
        values["claimable_after"] = None
        return values

    monkeypatch.setattr(claims, "release_values", no_cooldown)
    failing = world.add_uncertain(last_attempt_ago=600)
    other = world.add_uncertain()
    r = make(
        Lookups(
            {action_of(world, failing): UNAVAILABLE, action_of(world, other): found(world, other)}
        )
    )
    for _ in range(5):
        await r.run_pass()
    assert world.row(other).status is RunStatus.NEEDS_RECONCILIATION, "starved"
    assert {action for _, action in r.client.lookups} == {action_of(world, failing)}


async def test_a_run_failing_alone_never_streaks_and_the_stale_alert_reaches_a_person(
    world: World, make: Callable[..., Reconciler]
) -> None:
    """ADR-017's cost: with no other lookup answered in between, a lone failing run never
    reaches the limit; the stale-unknown alert is its route to a person."""
    alone = world.add_uncertain(last_attempt_ago=600)
    r = make(Lookups({action_of(world, alone): UNAVAILABLE}))
    for _ in range(4):
        assert await r.run_pass() is Reconciled.PROVIDER_FAILED
        world.end_cooldown(alone)
    assert world.row(alone).release_streak == 0
    assert r.raised(Severity.ALERT, "reconcile_stale_unknown") == []
    world.set(alone, escalated_at=ago(TIMINGS.stale_unknown_after.total_seconds() + 60))
    await r.run_pass()
    [stale] = r.raised(Severity.ALERT, "reconcile_stale_unknown")
    assert stale.run_id == alone


async def test_unauthorized_lookups_page_and_close_the_gate(
    world: World, make: Callable[..., Reconciler]
) -> None:
    run = world.add_uncertain()
    r = make(Lookups({action_of(world, run): HttpResult(status=401)}))
    assert await r.run_pass() is Reconciled.PROVIDER_FAILED
    assert r.h.alerter.kinds(Severity.PAGE) == ["refund_api_auth_failed"]
    assert r.h.ctx.gate.is_open(), "the read breaker's concern, not the send loop's (ADR-018)"
    row = world.row(run)
    assert (row.status, row.release_streak, row.worker_id) == (
        RunStatus.NEEDS_RECONCILIATION,
        0,
        None,
    )
    assert world.seconds_until(run, "claimable_after") > 0


async def test_a_mismatched_refund_streaks_and_pages_once_per_interval_keeping_its_status(
    world: World, make: Callable[..., Reconciler], caplog: pytest.LogCaptureFixture
) -> None:
    run = world.add_uncertain()
    payload = payload_of(world, run)
    foreign = payload.model_copy(update={"amount_minor": payload.amount_minor + 1})
    r = make(Lookups({payload.action_id: lookup_result(foreign)}))
    r.logging_alerts()
    with caplog.at_level(logging.WARNING, logger="agent.alerts"):
        for _ in range(5):
            assert await r.run_pass() is Reconciled.RUN_FAILED
            world.end_cooldown(run)
    row = world.row(run)
    assert (row.status, row.refund_outcome, row.release_streak) == (
        RunStatus.NEEDS_RECONCILIATION,
        RefundOutcome.UNKNOWN,
        5,
    )
    pages = [rec for rec in caplog.records if rec.levelno == logging.CRITICAL]
    assert len(pages) == 1 and '"release_streak_limit"' in pages[0].getMessage()


async def test_negative_control_a_streak_that_moved_the_run_breaks_adr008s_check(
    world: World, async_runtime_engine: AsyncEngine
) -> None:
    """Why the limit pages instead of escalating, as it does for an APPROVED run: a run whose
    outcome is unknown can't be terminal."""
    world.add_uncertain()
    claimed = await claim(async_runtime_engine)
    assert claimed is not None
    with pytest.raises(IntegrityError) as raised:
        async with claims.worker_transaction(
            async_runtime_engine,
            statement_timeout=TIMINGS.statement_timeout,
            lock_timeout=TIMINGS.lock_timeout,
        ) as conn:
            await conn.execute(
                update(runs)
                .where(claims.fenced(claimed))
                .values(status=RunStatus.ESCALATED, worker_id=None, lease_expires_at=None)
            )
    assert raised.value.orig.diag.constraint_name == "ck_runs_terminal_not_unknown"  # type: ignore[union-attr]


async def test_a_run_with_no_payload_to_look_up_is_the_runs_failure(
    world: World, make: Callable[..., Reconciler]
) -> None:
    run = world.add_uncertain(canonical_payload="not a payload")
    r = make()
    assert await r.run_pass() is Reconciled.RUN_FAILED
    [alert] = r.raised(Severity.ALERT, "reconcile_unreadable_run")
    assert alert.run_id == run
    assert world.row(run).release_streak == 1
    assert r.client.lookups == []


async def test_an_unexpected_error_is_the_runs_failure_with_the_cooldown(
    world: World, make: Callable[..., Reconciler]
) -> None:
    run = world.add_uncertain()
    r = make(Lookups({action_of(world, run): RuntimeError("bug")}))
    assert await r.run_pass() is Reconciled.RUN_FAILED
    [alert] = r.raised(Severity.ALERT, "reconcile_error")
    assert (alert.run_id, alert.fields["reason"]) == (run, "RuntimeError")
    row = world.row(run)
    assert (row.status, row.release_streak, row.worker_id) == (
        RunStatus.NEEDS_RECONCILIATION,
        1,
        None,
    )
    assert world.seconds_until(run, "claimable_after") > 0


# --- the config read each pass (Part 2, S2) -------------------------------------------------


async def test_a_budget_below_the_agents_minimum_is_an_invalid_read_and_nothing_is_claimed(
    world: World, make: Callable[..., Reconciler]
) -> None:
    run = world.add_uncertain()
    low = config_result(window_ms=READ_WINDOW_MS, retention_ms=3_600_000, budget_ms=500)
    r = make(Lookups({action_of(world, run): NOTHING}, then_config=low))
    assert await r.run_pass() is None
    assert world.row(run).claim_version == 0
    assert r.h.ctx.gate.state is GateState.READ_FAILED
    assert r.raised(Severity.ALERT, "provider_config_read_failed")


async def test_negative_control_accepting_a_low_budget_acts_on_the_run(
    world: World, make: Callable[..., Reconciler], monkeypatch: pytest.MonkeyPatch
) -> None:
    run = world.add_uncertain()
    low = config_result(window_ms=READ_WINDOW_MS, retention_ms=3_600_000, budget_ms=500)
    r = make(Lookups({action_of(world, run): NOTHING}, then_config=low))
    original = gate.read_config

    async def no_minimum(client: Any, settings: Any) -> Any:
        return await original(
            client, replace(settings, min_provider_budget=timedelta(milliseconds=1))
        )

    monkeypatch.setattr(reconcile, "read_config", no_minimum)
    assert await r.run_pass() is Reconciled.REJECTED


async def test_each_pass_reads_the_config_afresh_even_with_the_gate_open(
    world: World, make: Callable[..., Reconciler]
) -> None:
    r = make()
    for passes in (1, 2, 3):
        assert await r.run_pass() is None
        assert r.client.config_reads == passes
    assert r.h.ctx.gate.is_open()


# --- the stale-unknown alert (ADR-004) ------------------------------------------------------


async def test_the_stale_unknown_alert_fires_past_the_threshold_once_per_run_per_interval(
    world: World, make: Callable[..., Reconciler], caplog: pytest.LogCaptureFixture
) -> None:
    threshold = TIMINGS.stale_unknown_after.total_seconds()
    recent = world.add_uncertain()
    old = world.add_uncertain()
    for run in (recent, old):
        world.set(run, claimable_after=text("now() + interval '60 seconds'"))
    world.set(recent, escalated_at=ago(threshold - 60))
    world.set(old, escalated_at=ago(threshold + 60))
    r = make()
    r.logging_alerts()
    with caplog.at_level(logging.WARNING, logger="agent.alerts"):
        for _ in range(3):
            assert await r.run_pass() is None
    stale = [
        rec.getMessage() for rec in caplog.records if "reconcile_stale_unknown" in rec.getMessage()
    ]
    assert len(stale) == 1 and str(old) in stale[0] and str(recent) not in stale[0]


async def test_a_failing_stale_scan_alerts_and_never_blocks_reconciling(
    world: World, make: Callable[..., Reconciler], monkeypatch: pytest.MonkeyPatch
) -> None:
    run = world.add_uncertain()

    async def broken(conn: Any, after: timedelta) -> Any:
        raise RuntimeError("planted")

    monkeypatch.setattr(reconcile_db, "stale_unknown", broken)
    r = make(Lookups({action_of(world, run): found(world, run)}))
    assert await r.run_pass() is Reconciled.CONFIRMED
    assert r.raised(Severity.ALERT, "reconcile_stale_scan_failed")


# --- the stale-unknown page (ADR-017 S3, S4) ------------------------------------------------

PAGE_AFTER = TIMINGS.stale_page_after.total_seconds()
ALERT_AFTER = TIMINGS.stale_unknown_after.total_seconds()


def stale_pages(r: Reconciler) -> list[uuid.UUID | None]:
    return [page.run_id for page in r.raised(Severity.PAGE, "reconcile_stale_unknown")]


async def test_a_run_failing_alone_pages_past_the_page_threshold_once_per_interval(
    world: World, make: Callable[..., Reconciler], caplog: pytest.LogCaptureFixture
) -> None:
    """Its streak never moves (ADR-017), so the stale page is its route to a person. A run only
    past the alert threshold alerts and doesn't page."""
    alone = world.add_uncertain(last_attempt_ago=600)
    alerted_only = world.add_uncertain(last_attempt_ago=600)
    world.set(alone, escalated_at=ago(PAGE_AFTER + 60))
    world.set(alerted_only, escalated_at=ago(ALERT_AFTER + 60))
    world.set(alerted_only, claimable_after=text("now() + interval '60 seconds'"))
    r = make(Lookups({action_of(world, alone): UNAVAILABLE}))
    r.logging_alerts()
    with caplog.at_level(logging.WARNING, logger="agent.alerts"):
        for _ in range(3):
            await r.run_pass()
            world.end_cooldown(alone)
    pages = [rec.getMessage() for rec in caplog.records if rec.levelno == logging.CRITICAL]
    assert len(pages) == 1 and '"reconcile_stale_unknown"' in pages[0] and str(alone) in pages[0]
    assert world.row(alone).release_streak == 0, "it pages without a streak"
    alerts = [rec.getMessage() for rec in caplog.records if rec.levelno == logging.WARNING]
    assert any('"reconcile_stale_unknown"' in a and str(alerted_only) in a for a in alerts)


async def hold_one_fail_one(world: World, r: Reconciler) -> tuple[uuid.UUID, uuid.UUID]:
    """Kill switch on: one run's "not found" is held, another's lookups fail on their own; both
    then sit past the page threshold, in their cooldowns."""
    world.controls(sends_enabled=False)
    held = world.add_uncertain(last_attempt_ago=600)
    failing = world.add_uncertain(last_attempt_ago=300)
    r.client.answers.update(
        {action_of(world, held): NOTHING, action_of(world, failing): UNAVAILABLE}
    )
    assert await r.run_pass() is Reconciled.HELD
    assert await r.run_pass() is Reconciled.PROVIDER_FAILED
    for run in (held, failing):
        world.set(run, escalated_at=ago(PAGE_AFTER + 60))
    return held, failing


async def test_with_the_switch_on_only_runs_it_held_skip_their_own_page(
    world: World, make: Callable[..., Reconciler]
) -> None:
    r = make()
    held, failing = await hold_one_fail_one(world, r)
    assert await r.run_pass() is None
    assert stale_pages(r) == [failing], "a run stuck for its own reason keeps its page"
    [together] = r.raised(Severity.PAGE, "reconcile_stale_held_by_kill_switch")
    assert (together.run_id, together.fields["held_runs"]) == (None, 1)
    alerted = {a.run_id for a in r.raised(Severity.ALERT, "reconcile_stale_unknown")}
    assert alerted == {held, failing}, "the per-run alert still fires for both"


async def test_negative_control_suppressing_every_run_while_the_switch_is_on_silences_it(
    world: World, make: Callable[..., Reconciler], monkeypatch: pytest.MonkeyPatch
) -> None:
    r = make()
    _, failing = await hold_one_fail_one(world, r)
    monkeypatch.setattr(LookupEvidence, "is_held", lambda self, run_id: True)
    assert await r.run_pass() is None
    assert failing not in stale_pages(r)


async def test_negative_control_paging_held_runs_pages_once_per_held_run(
    world: World, make: Callable[..., Reconciler], monkeypatch: pytest.MonkeyPatch
) -> None:
    r = make()
    held, failing = await hold_one_fail_one(world, r)
    monkeypatch.setattr(LookupEvidence, "is_held", lambda self, run_id: False)
    assert await r.run_pass() is None
    assert set(stale_pages(r)) == {held, failing}


async def test_any_other_outcome_clears_the_hold(
    world: World, make: Callable[..., Reconciler]
) -> None:
    """Held, then its lookups start failing on their own: it is no longer the switch's, so it
    pages on its own again, from the next pass's scan (each pass scans before it claims)."""
    r = make()
    held, _ = await hold_one_fail_one(world, r)
    r.client.answers[action_of(world, held)] = UNAVAILABLE
    world.end_cooldown(held)
    assert await r.run_pass() is Reconciled.PROVIDER_FAILED
    assert held not in stale_pages(r), "this pass scanned while it was still held"
    assert await r.run_pass() is None
    assert held in stale_pages(r)


async def test_a_restart_forgets_holds_so_held_runs_page_on_their_own(
    world: World, make: Callable[..., Reconciler]
) -> None:
    r = make()
    held, _ = await hold_one_fail_one(world, r)
    r.evidence = LookupEvidence(lambda: r.h.ctx.read_breaker.successes)  # a new process
    assert await r.run_pass() is None
    assert held in stale_pages(r)


# --- both loops in one process (Part 1) -----------------------------------------------------


async def test_the_worker_runs_both_loops_and_names_them_when_ready(
    world: World, async_runtime_engine: AsyncEngine, caplog: pytest.LogCaptureFixture
) -> None:
    approved = world.add_refund_run(world.add_order(), 1_000)
    uncertain = world.add_uncertain()
    client = Lookups(
        {action_of(world, uncertain): found(world, uncertain)},
        sends=[created_result(payload_of(world, approved))],
    )
    h = Harness(async_runtime_engine, client=client)
    with caplog.at_level(logging.INFO, logger="agent.worker.loop"):
        task = asyncio.create_task(run_worker(h.ctx, "W"))
        try:
            async with asyncio.timeout(BOUND):
                while True:
                    rows = await asyncio.gather(
                        asyncio.to_thread(world.row, approved),
                        asyncio.to_thread(world.row, uncertain),
                    )
                    if {row.status for row in rows} == {RunStatus.COMPLETED}:
                        break
                    await asyncio.sleep(0.05)
        finally:
            h.ctx.stopping.set()
            async with asyncio.timeout(BOUND):
                await task
    assert any("send loop and reconcile loop" in rec.getMessage() for rec in caplog.records), (
        "the readiness log names both loops"
    )
    assert len(client.sent) == 1, "only the send loop sent"


async def test_stopping_lets_a_lookup_on_the_wire_finish_and_be_recorded(
    world: World, async_runtime_engine: AsyncEngine
) -> None:
    uncertain = world.add_uncertain()
    on_the_wire = asyncio.Event()

    async def slow_found() -> HttpResult:
        on_the_wire.set()
        await asyncio.sleep(0.5)
        return lookup_result(payload)

    payload = payload_of(world, uncertain)
    h = Harness(async_runtime_engine, client=Lookups({payload.action_id: slow_found}))
    task = asyncio.create_task(run_worker(h.ctx, "W"))
    async with asyncio.timeout(BOUND):
        await on_the_wire.wait()
        h.ctx.stopping.set()
        await task
    assert (await asyncio.to_thread(world.row, uncertain)).status is RunStatus.COMPLETED
