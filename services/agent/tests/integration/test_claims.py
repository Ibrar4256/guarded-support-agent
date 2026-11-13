"""The claim statement, fencing and releases (ADR-004 step 7, ADR-009 R14, ADR-014 Part 2, S9,
S12). Each named claim condition has a negative control that drops only that condition."""

from collections.abc import Callable
from datetime import timedelta
from typing import Any

import pytest
from conftest import T0
from sqlalchemy import ColumnElement, FromClause, func, update
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine
from worker_world import World

from agent.core.capacity import ApprovalSource
from agent.core.lifecycle import RunStatus
from agent.db import claims
from agent.db.claims import Claim, Controls, Release
from agent.db.schema import runs

LEASE = timedelta(seconds=30)
COOLDOWN = timedelta(seconds=60)
STATEMENT_TIMEOUT = timedelta(seconds=2)
LOCK_TIMEOUT = timedelta(milliseconds=200)
EARLY, LATE = T0, T0 + timedelta(minutes=5)

Conditions = Callable[[FromClause, Controls], dict[str, ColumnElement[bool]]]


def without(condition: str) -> Conditions:
    """The production claim conditions minus one: the planted bug for a negative control."""
    original = claims.claim_conditions

    def planted(r: FromClause, controls: Controls) -> dict[str, ColumnElement[bool]]:
        kept = {k: v for k, v in original(r, controls).items() if k != condition}
        assert len(kept) == len(original(r, controls)) - 1, f"no condition named {condition}"
        return kept

    return planted


async def claim(engine: AsyncEngine, worker: str = "w1") -> Claim | None:
    async with claims.worker_transaction(
        engine, statement_timeout=STATEMENT_TIMEOUT, lock_timeout=LOCK_TIMEOUT
    ) as conn:
        return await claims.claim_run(conn, worker, LEASE)


async def renew(engine: AsyncEngine, claimed: Claim) -> bool:
    async with claims.worker_transaction(
        engine, statement_timeout=STATEMENT_TIMEOUT, lock_timeout=LOCK_TIMEOUT
    ) as conn:
        return await claims.renew_lease(conn, claimed, LEASE)


async def release(engine: AsyncEngine, claimed: Claim, **kwargs: Any) -> Release:
    async with claims.worker_transaction(
        engine, statement_timeout=STATEMENT_TIMEOUT, lock_timeout=LOCK_TIMEOUT
    ) as conn:
        return await claims.release_claim(conn, claimed, **kwargs)


async def fenced_write(engine: AsyncEngine, claimed: Claim) -> int:
    """Any write fenced by claim_version alone, as ADR-005's pre-send move is."""
    async with claims.worker_transaction(
        engine, statement_timeout=STATEMENT_TIMEOUT, lock_timeout=LOCK_TIMEOUT
    ) as conn:
        result = await conn.execute(
            update(runs)
            .where(claims.fenced(claimed))
            .values(presend_attempts=runs.c.presend_attempts + 1)
        )
        return result.rowcount


# --- the claim statement -------------------------------------------------------------------


async def test_claim_takes_an_approved_run_and_bumps_the_version(
    world: World, async_runtime_engine: AsyncEngine
) -> None:
    run = world.add_run()
    claimed = await claim(async_runtime_engine)
    assert claimed == Claim(run, RunStatus.APPROVED, claim_version=1, worker_id="w1")
    row = world.row(run)
    assert (row.worker_id, row.claimable_after) == ("w1", None)
    assert LEASE.total_seconds() - 5 < world.seconds_until(run, "lease_expires_at") <= 30


async def test_claim_returns_none_when_nothing_is_claimable(
    world: World, async_runtime_engine: AsyncEngine
) -> None:
    assert await claim(async_runtime_engine) is None


UNCLAIMABLE = [
    RunStatus.AGENT_STEP,
    RunStatus.AWAIT_APPROVAL,
    RunStatus.NEEDS_RECONCILIATION,
    RunStatus.COMPLETED,
    RunStatus.ESCALATED,
]


@pytest.mark.parametrize("status", UNCLAIMABLE)
async def test_unclaimable_statuses_are_never_claimed(
    world: World, async_runtime_engine: AsyncEngine, status: RunStatus
) -> None:
    world.add_run(status)
    assert await claim(async_runtime_engine) is None


@pytest.mark.parametrize(
    "status", [RunStatus.AGENT_STEP, RunStatus.AWAIT_APPROVAL, RunStatus.NEEDS_RECONCILIATION]
)
async def test_negative_control_without_the_status_condition_they_are_claimed(
    world: World,
    async_runtime_engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
    status: RunStatus,
) -> None:
    """For these, the status condition is the only guard."""
    run = world.add_run(status)
    monkeypatch.setattr(claims, "claim_conditions", without("status"))
    claimed = await claim(async_runtime_engine)
    assert claimed is not None and claimed.run_id == run


@pytest.mark.parametrize("status", [RunStatus.COMPLETED, RunStatus.ESCALATED])
async def test_negative_control_terminal_runs_are_also_guarded_by_the_database(
    world: World,
    async_runtime_engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
    status: RunStatus,
) -> None:
    """Without the status condition, the claim tries to take a finished run, and ADR-008's
    CHECK (a finished run holds no claim) refuses it: a second layer, not the first."""
    world.add_run(status)
    monkeypatch.setattr(claims, "claim_conditions", without("status"))
    with pytest.raises(IntegrityError) as caught:
        await claim(async_runtime_engine)
    assert caught.value.orig.diag.constraint_name == "ck_runs_terminal_no_claim"  # type: ignore[union-attr]


async def test_a_live_lease_is_not_taken_and_an_expired_one_is(
    world: World, async_runtime_engine: AsyncEngine
) -> None:
    run = world.add_run()
    assert await claim(async_runtime_engine, "A") is not None
    assert await claim(async_runtime_engine, "B") is None
    world.expire_lease(run)
    taken = await claim(async_runtime_engine, "B")
    assert taken == Claim(run, RunStatus.APPROVED, claim_version=2, worker_id="B")


async def test_negative_control_without_the_lease_condition_a_live_lease_is_taken(
    world: World, async_runtime_engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    run = world.add_run()
    assert await claim(async_runtime_engine, "A") is not None
    monkeypatch.setattr(claims, "claim_conditions", without("lease_free"))
    stolen = await claim(async_runtime_engine, "B")
    assert stolen is not None and stolen.run_id == run


async def test_a_run_in_its_cooldown_is_skipped_until_it_passes(
    world: World, async_runtime_engine: AsyncEngine
) -> None:
    cooling = world.add_run(approved_at=EARLY)
    world.set(cooling, claimable_after=T0 + timedelta(days=36500))
    other = world.add_run(approved_at=LATE)
    first = await claim(async_runtime_engine, "A")
    assert first is not None and first.run_id == other, "another run is claimed meanwhile"
    assert await claim(async_runtime_engine, "B") is None
    world.set(cooling, claimable_after=T0)
    after = await claim(async_runtime_engine, "B")
    assert after is not None and after.run_id == cooling
    assert world.row(cooling).claimable_after is None, "a claim clears the cooldown"


async def test_negative_control_without_the_cooldown_condition_it_is_claimed_first(
    world: World, async_runtime_engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    cooling = world.add_run(approved_at=EARLY)
    world.set(cooling, claimable_after=T0 + timedelta(days=36500))
    world.add_run(approved_at=LATE)
    monkeypatch.setattr(claims, "claim_conditions", without("cooldown_passed"))
    first = await claim(async_runtime_engine)
    assert first is not None and first.run_id == cooling


# R14 (ADR-009): which runs each control holds.
HOLDS = [
    pytest.param("breaker", RunStatus.APPROVED, ApprovalSource.POLICY, False, id="breaker-policy"),
    pytest.param(
        "breaker", RunStatus.APPROVED, ApprovalSource.REVIEWER, True, id="breaker-reviewer"
    ),
    pytest.param("breaker", RunStatus.EXECUTING, ApprovalSource.POLICY, True, id="breaker-exec"),
    pytest.param("kill", RunStatus.APPROVED, ApprovalSource.POLICY, False, id="kill-policy"),
    pytest.param("kill", RunStatus.APPROVED, ApprovalSource.REVIEWER, False, id="kill-reviewer"),
    pytest.param("kill", RunStatus.EXECUTING, ApprovalSource.REVIEWER, True, id="kill-exec"),
]


def apply_control(world: World, control: str) -> None:
    if control == "breaker":
        world.controls(breaker_tripped=True)
    else:
        world.controls(sends_enabled=False)


@pytest.mark.parametrize(("control", "status", "source", "claimable"), HOLDS)
async def test_holds_follow_r14(
    world: World,
    async_runtime_engine: AsyncEngine,
    control: str,
    status: RunStatus,
    source: ApprovalSource,
    claimable: bool,
) -> None:
    world.add_run(status, source=source)
    apply_control(world, control)
    assert (await claim(async_runtime_engine) is not None) is claimable


@pytest.mark.parametrize(
    ("control", "source"),
    [("breaker", ApprovalSource.POLICY), ("kill", ApprovalSource.REVIEWER)],
)
async def test_negative_control_without_the_hold_condition_held_runs_are_claimed(
    world: World,
    async_runtime_engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
    control: str,
    source: ApprovalSource,
) -> None:
    world.add_run(source=source)
    apply_control(world, control)
    monkeypatch.setattr(claims, "claim_conditions", without("not_held"))
    assert await claim(async_runtime_engine) is not None


async def test_executing_runs_come_first_then_the_oldest_approval(
    world: World, async_runtime_engine: AsyncEngine
) -> None:
    old_approved = world.add_run(approved_at=EARLY)
    new_approved = world.add_run(approved_at=LATE)
    executing = world.add_run(RunStatus.EXECUTING, approved_at=LATE + timedelta(minutes=1))
    order = [await claim(async_runtime_engine, f"w{i}") for i in range(3)]
    assert [c.run_id if c else None for c in order] == [executing, old_approved, new_approved]


async def test_a_locked_row_is_skipped_not_waited_for(
    world: World, async_runtime_engine: AsyncEngine
) -> None:
    locked = world.add_run(approved_at=EARLY)
    free = world.add_run(approved_at=LATE)
    with world.owner.connect() as holder, holder.begin():
        holder.execute(runs.select().where(runs.c.id == locked).with_for_update())
        claimed = await claim(async_runtime_engine)
    assert claimed is not None and claimed.run_id == free


async def test_negative_control_without_skip_locked_the_claim_waits_on_the_lock(
    world: World, async_runtime_engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    locked = world.add_run(approved_at=EARLY)
    world.add_run(approved_at=LATE)
    monkeypatch.setattr(claims, "SKIP_LOCKED", False)
    with world.owner.connect() as holder, holder.begin():
        holder.execute(runs.select().where(runs.c.id == locked).with_for_update())
        with pytest.raises(DBAPIError) as caught:
            await claim(async_runtime_engine)  # bounded by LOCK_TIMEOUT
    assert caught.value.orig.sqlstate == "55P03"  # type: ignore[union-attr]


# --- fencing and the heartbeat's write -------------------------------------------------------


async def test_renewal_extends_the_lease_and_fails_once_another_worker_claims(
    world: World, async_runtime_engine: AsyncEngine
) -> None:
    run = world.add_run()
    first = await claim(async_runtime_engine, "A")
    assert first is not None
    world.expire_lease(run)
    assert await renew(async_runtime_engine, first), "nobody else claimed: still A's version"
    assert world.seconds_until(run, "lease_expires_at") > LEASE.total_seconds() - 5
    world.expire_lease(run)
    assert await claim(async_runtime_engine, "B") is not None
    assert not await renew(async_runtime_engine, first), "A's claim is gone"
    assert world.row(run).worker_id == "B"


async def test_negative_control_fencing_without_the_version_renews_a_stolen_run(
    world: World, async_runtime_engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    run = world.add_run()
    first = await claim(async_runtime_engine, "A")
    assert first is not None
    world.expire_lease(run)
    assert await claim(async_runtime_engine, "B") is not None
    monkeypatch.setattr(claims, "fenced", lambda c: runs.c.id == c.run_id)
    assert await renew(async_runtime_engine, first)


# --- releases (S9, S12) ----------------------------------------------------------------------


async def test_release_clears_the_claim_and_kills_the_releasers_version(
    world: World, async_runtime_engine: AsyncEngine
) -> None:
    run = world.add_run()
    claimed = await claim(async_runtime_engine)
    assert claimed is not None
    assert await release(async_runtime_engine, claimed) == Release(True, release_streak=0)
    row = world.row(run)
    assert (row.worker_id, row.lease_expires_at, row.claimable_after) == (None, None, None)
    assert row.claim_version == claimed.claim_version + 1
    assert await fenced_write(async_runtime_engine, claimed) == 0, "a stray write after release"
    again = await claim(async_runtime_engine)
    assert again is not None and again.run_id == run, "a plain release is claimable at once"


async def test_negative_control_release_without_the_bump_lets_a_stray_write_commit(
    world: World, async_runtime_engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    world.add_run()
    claimed = await claim(async_runtime_engine)
    assert claimed is not None
    original = claims.release_values

    def no_bump(cooldown: timedelta | None, **kwargs: Any) -> dict[str, Any]:
        values = original(cooldown, **kwargs)
        del values["claim_version"]
        return values

    monkeypatch.setattr(claims, "release_values", no_bump)
    assert (await release(async_runtime_engine, claimed)).released
    assert await fenced_write(async_runtime_engine, claimed) == 1


async def test_a_stale_claim_releases_nothing(
    world: World, async_runtime_engine: AsyncEngine
) -> None:
    run = world.add_run()
    first = await claim(async_runtime_engine, "A")
    assert first is not None
    world.expire_lease(run)
    assert await claim(async_runtime_engine, "B") is not None
    assert await release(async_runtime_engine, first) == Release(False)
    assert world.row(run).worker_id == "B"


async def test_cooldown_release_waits_and_counts_toward_the_streak(
    world: World, async_runtime_engine: AsyncEngine
) -> None:
    run = world.add_run(approved_at=EARLY)
    other = world.add_run(approved_at=LATE)
    claimed = await claim(async_runtime_engine, "A")
    assert claimed is not None and claimed.run_id == run
    assert await release(async_runtime_engine, claimed, cooldown=COOLDOWN) == Release(True, 1)
    cooldown_left = world.seconds_until(run, "claimable_after")
    assert COOLDOWN.total_seconds() - 5 < cooldown_left <= COOLDOWN.total_seconds()
    next_claim = await claim(async_runtime_engine, "B")
    assert next_claim is not None and next_claim.run_id == other, "not reclaimed in its cooldown"
    world.set(run, claimable_after=T0)
    again = await claim(async_runtime_engine, "C")
    assert again is not None and again.run_id == run
    assert await release(async_runtime_engine, again, cooldown=COOLDOWN) == Release(True, 2)


async def test_a_cooldown_release_can_leave_the_streak_alone(
    world: World, async_runtime_engine: AsyncEngine
) -> None:
    """ADR-016 S5: the cooldown and the streak are separate choices."""
    run = world.add_run(approved_at=EARLY)
    world.set(run, release_streak=2)
    claimed = await claim(async_runtime_engine)
    assert claimed is not None
    released = await release(async_runtime_engine, claimed, cooldown=COOLDOWN, count_streak=False)
    assert released == Release(True, 2)
    assert world.seconds_until(run, "claimable_after") > COOLDOWN.total_seconds() - 5


async def test_negative_control_a_plain_release_is_reclaimed_on_the_next_poll(
    world: World, async_runtime_engine: AsyncEngine
) -> None:
    run = world.add_run(approved_at=EARLY)
    world.add_run(approved_at=LATE)
    claimed = await claim(async_runtime_engine, "A")
    assert claimed is not None and claimed.run_id == run
    await release(async_runtime_engine, claimed)
    next_claim = await claim(async_runtime_engine, "B")
    assert next_claim is not None and next_claim.run_id == run


def test_claim_conditions_are_the_four_adr_conditions() -> None:
    """The negative controls above drop each of these by name."""
    assert set(claims.claim_conditions(runs, claims.current_controls())) == {
        "status",
        "lease_free",
        "cooldown_passed",
        "not_held",
    }


async def test_renewal_never_shortens_a_lease_extended_for_an_attempt(
    world: World, async_runtime_engine: AsyncEngine
) -> None:
    """ADR-014 Part 3: the attempt transaction extends the lease past the client timeout; a
    heartbeat landing mid-attempt with a shorter lease must not cut it back."""
    run = world.add_run()
    claimed = await claim(async_runtime_engine)
    assert claimed is not None
    with world.transaction() as connection:
        connection.execute(
            update(runs)
            .where(runs.c.id == run)
            .values(lease_expires_at=func.now() + timedelta(hours=1))
        )
    assert await renew(async_runtime_engine, claimed)
    assert world.seconds_until(run, "lease_expires_at") > 3000, "still about an hour"
