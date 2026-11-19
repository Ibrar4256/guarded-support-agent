"""What the proposal path reads under the customer lock (ADR-009 Definitions, R11-R19).

Each test arranges rows that must count and rows that must not, then reads the inputs as the
runtime role. System-wide totals are compared before and after, since other customers' rows
may exist in the shared test database.
"""

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import Engine, insert
from sqlalchemy.ext.asyncio import AsyncEngine
from worker_world import World

from agent.core.capacity import ApprovalSource
from agent.core.lifecycle import RefundOutcome, RunStatus
from agent.core.policy import ProposalInputs, RefundProposal
from agent.db import proposal as proposal_db
from agent.db.claims import worker_transaction
from agent.db.schema import decisions

BREAKER_WINDOW = timedelta(hours=1)


@pytest.fixture
def other(owner_engine: Engine) -> Iterator[World]:
    """A second customer."""
    world = World(owner_engine)
    try:
        yield world
    finally:
        world.teardown()


def now() -> datetime:
    return datetime.now(UTC)


def action() -> str:
    return f"act_{uuid.uuid4().hex}"


def refund_run(world: World, status: RunStatus, order: str, amount: int, **extra: Any) -> uuid.UUID:
    proposal: dict[str, Any] = {
        "action_id": action(),
        "order_id": order,
        "amount_minor": amount,
        "currency": "USD",
    }
    return world.add_run(status, **proposal | extra)


def add_decision(world: World, run: uuid.UUID, stage: str, decided_at: datetime) -> None:
    with world.transaction() as connection:
        connection.execute(
            insert(decisions).values(
                run_id=run,
                stage=stage,
                outcome="X",
                policy_version="1.0.0",
                config_snapshot={},
                input_snapshot={},
                decided_by="policy",
                decided_at=decided_at,
            )
        )


async def read(engine: AsyncEngine, world: World, order: str) -> ProposalInputs:
    run = world.add_run(RunStatus.AGENT_STEP)
    async with worker_transaction(
        engine, statement_timeout=timedelta(seconds=5), lock_timeout=timedelta(seconds=2)
    ) as conn:
        return await proposal_db.load_proposal_inputs(
            conn,
            run_id=run,
            user=world.user,
            proposal=RefundProposal(action(), order, 1_000, "USD"),
            breaker_window=BREAKER_WINDOW,
            at=None,
        )


async def test_history_holds_the_customers_actions_and_other_actions_on_this_order(
    world: World, other: World, async_runtime_engine: AsyncEngine
) -> None:
    order, elsewhere = world.add_order(), world.add_order()
    own_elsewhere = refund_run(world, RunStatus.AWAIT_APPROVAL, elsewhere, 500)
    theirs_here = refund_run(other, RunStatus.APPROVED, order, 700)
    refund_run(other, RunStatus.APPROVED, other.add_order(), 900)  # neither mine nor this order
    world.add_run(RunStatus.COMPLETED)  # no action: not a refund

    inputs = await read(async_runtime_engine, world, order)

    ids = {world.row(own_elsewhere).action_id, other.row(theirs_here).action_id}
    assert {r.action_id for r in inputs.history} == ids


async def test_declines_are_read_for_this_order_only(
    world: World, async_runtime_engine: AsyncEngine
) -> None:
    order, elsewhere = world.add_order(), world.add_order()
    declined_at = now() - timedelta(days=2)
    add_decision(
        world, refund_run(world, RunStatus.COMPLETED, order, 500), "reviewer_decline", declined_at
    )
    add_decision(
        world, refund_run(world, RunStatus.COMPLETED, elsewhere, 500), "reviewer_decline", now()
    )
    add_decision(
        world, refund_run(world, RunStatus.COMPLETED, order, 500), "reviewer_approve", now()
    )

    inputs = await read(async_runtime_engine, world, order)

    assert [(d.order_id, d.declined_at) for d in inputs.declines] == [(order, declined_at)]


@pytest.mark.parametrize(
    ("status", "extra", "expected"),
    [
        (RunStatus.ESCALATED, {}, True),
        (RunStatus.ESCALATED, {"escalation_resolved_at": datetime(2026, 10, 9, tzinfo=UTC)}, False),
        # A reconciliation alert on a refund later confirmed isn't an escalation (ADR-009).
        (RunStatus.COMPLETED, {"escalated_at": datetime(2026, 10, 9, tzinfo=UTC)}, False),
        (RunStatus.AWAIT_APPROVAL, {"c01_flagged_at": datetime(2026, 10, 9, tzinfo=UTC)}, True),
    ],
    ids=["escalated", "resolved", "confirmed_after_alert", "c01_flagged_pending"],
)
async def test_an_unresolved_escalation_is_an_escalated_or_c01_flagged_run(
    world: World,
    other: World,
    async_runtime_engine: AsyncEngine,
    status: RunStatus,
    extra: dict[str, Any],
    expected: bool,
) -> None:
    order = world.add_order()
    world.add_run(status, **extra)
    other.add_run(RunStatus.ESCALATED)  # another customer's never counts

    inputs = await read(async_runtime_engine, world, order)

    assert inputs.has_unresolved_escalation is expected


async def test_review_counts_are_per_customer_and_system_wide(
    world: World, other: World, async_runtime_engine: AsyncEngine
) -> None:
    order = world.add_order()
    before = await read(async_runtime_engine, world, order)
    for _ in range(2):
        refund_run(world, RunStatus.AWAIT_APPROVAL, world.add_order(), 500)
    refund_run(other, RunStatus.AWAIT_APPROVAL, other.add_order(), 500)
    refund_run(world, RunStatus.APPROVED, world.add_order(), 500)

    after = await read(async_runtime_engine, world, order)

    assert (before.customer_pending_reviews, after.customer_pending_reviews) == (0, 2)
    assert after.global_pending_reviews - before.global_pending_reviews == 3


async def test_recent_proposals_count_this_customers_proposal_rows_in_the_last_day(
    world: World, other: World, async_runtime_engine: AsyncEngine
) -> None:
    order = world.add_order()
    mine = world.add_run(RunStatus.ESCALATED)
    add_decision(world, mine, "proposal", now() - timedelta(hours=1))
    add_decision(world, mine, "proposal", now() - timedelta(hours=25))
    add_decision(world, mine, "presend", now() - timedelta(hours=1))
    add_decision(other, other.add_run(RunStatus.ESCALATED), "proposal", now())

    inputs = await read(async_runtime_engine, world, order)

    assert inputs.customer_proposals_last_24h == 1


async def test_the_breaker_total_counts_live_policy_approved_amounts_in_its_window(
    world: World, other: World, async_runtime_engine: AsyncEngine
) -> None:
    order = world.add_order()
    before = await read(async_runtime_engine, world, order)
    recent, stale = now() - timedelta(minutes=5), now() - timedelta(hours=2)
    refund_run(other, RunStatus.APPROVED, other.add_order(), 1_000, approved_at=recent)
    refund_run(world, RunStatus.EXECUTING, world.add_order(), 2_000, approved_at=recent)
    refund_run(
        world,
        RunStatus.APPROVED,
        world.add_order(),
        4_000,
        approved_at=recent,
        source=ApprovalSource.REVIEWER,
    )
    refund_run(world, RunStatus.APPROVED, world.add_order(), 8_000, approved_at=stale)
    refused = refund_run(  # refused at pre-send: nothing moved, no longer live
        world,
        RunStatus.ESCALATED,
        world.add_order(),
        16_000,
        approval_source=ApprovalSource.POLICY,
        escalated_at=recent,
        refund_outcome=RefundOutcome.NONE,
    )
    world.set(refused, approved_at=recent)  # add_run sets approved_at only for live statuses

    after = await read(async_runtime_engine, world, order)

    delta = (
        after.global_policy_approved_in_breaker_window_minor
        - before.global_policy_approved_in_breaker_window_minor
    )
    assert delta == 3_000
