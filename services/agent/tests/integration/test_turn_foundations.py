"""The turn's foundations against Postgres (ADR-022 S1 step 1, S3, S5, S9; ADR-008 turn lease).

Start, finish, the heartbeat and the tool connection, without a model: the loop (T3) builds on
these. Every wait is bounded.
"""

import asyncio
import time
import uuid
from collections.abc import AsyncIterator

import pytest
from sqlalchemy import Engine, text, update
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, create_async_engine
from turn_helpers import FAST, SETTINGS
from worker_world import World

from agent.core.lifecycle import RunStatus
from agent.core.replies import ReplyTemplate
from agent.db import turns
from agent.db.clock import DATABASE_CLOCK
from agent.db.schema import conversations
from agent.tools.contract import LostTurn
from agent.turn.finish import TryAgainCause, reply_and_complete, reply_and_wait
from agent.turn.lease import TurnOverrun, hold_lease
from agent.turn.start import (
    ConversationNotFound,
    MessageRejected,
    TurnIds,
    TurnInProgress,
    TurnStart,
    start_turn,
)
from agent.turn.toolconn import tool_connection

BOUND = 10
CLEAN = {"c02": [], "c02_version": "c02-v1"}  # a message with no C02 match (ADR-029 S1)


async def start(
    engine: AsyncEngine, world: World, conversation: uuid.UUID, message: str = "hi"
) -> TurnStart:
    return await start_turn(
        engine,
        SETTINGS,
        conversation_id=conversation,
        user=world.user,
        message=message,
        clock=DATABASE_CLOCK,
        new_id=uuid.uuid4,
    )


def kinds(world: World, conversation: uuid.UUID) -> list[tuple[str, dict[str, object]]]:
    return [(e.kind, e.payload) for e in world.conversation_events(conversation)]


# --- Start ---------------------------------------------------------------------------------------


async def test_a_first_message_creates_a_run_and_holds_the_lease(
    world: World, async_runtime_engine: AsyncEngine
) -> None:
    conversation = world.add_conversation()

    started = await start(async_runtime_engine, world, conversation, "where is my order?")

    assert started.status is RunStatus.AGENT_STEP and started.answered is None
    assert world.row(started.ids.run_id).status == RunStatus.AGENT_STEP
    assert world.conversation(conversation).active_turn_id == started.ids.turn_id
    assert kinds(world, conversation) == [
        ("customer_message", {"text": "where is my order?", **CLEAN})
    ]


async def test_two_messages_at_once_get_one_turn_and_one_conflict(
    world: World, async_runtime_engine: AsyncEngine
) -> None:
    conversation = world.add_conversation()

    async with asyncio.timeout(BOUND):
        results = await asyncio.gather(
            start(async_runtime_engine, world, conversation, "one"),
            start(async_runtime_engine, world, conversation, "two"),
            return_exceptions=True,
        )

    assert sum(isinstance(r, TurnStart) for r in results) == 1
    assert sum(isinstance(r, TurnInProgress) for r in results) == 1
    assert len(world.runs_of(conversation)) == 1
    assert len(kinds(world, conversation)) == 1  # the refused message wasn't saved


async def test_an_expired_lease_can_be_taken_over(
    world: World, async_runtime_engine: AsyncEngine, owner_engine: Engine
) -> None:
    conversation = world.add_conversation()
    first = await start(async_runtime_engine, world, conversation)
    with owner_engine.begin() as owner:
        owner.execute(
            update(conversations)
            .where(conversations.c.id == conversation)
            .values(turn_expires_at=text("now() - interval '1 second'"))
        )

    second = await start(async_runtime_engine, world, conversation)

    assert second.ids.turn_id != first.ids.turn_id
    assert second.ids.run_id == first.ids.run_id  # the AGENT_STEP run is resumed


async def test_a_missing_and_another_customers_conversation_look_the_same(
    owner_engine: Engine, world: World, async_runtime_engine: AsyncEngine
) -> None:
    stranger = World(owner_engine)
    try:
        theirs = stranger.add_conversation()

        for conversation in (uuid.uuid4(), theirs):
            with pytest.raises(ConversationNotFound):
                await start(async_runtime_engine, world, conversation)

        assert stranger.conversation(theirs).active_turn_id is None
    finally:
        stranger.teardown()


@pytest.mark.parametrize(
    ("status", "template"),
    [
        (RunStatus.AWAIT_APPROVAL, ReplyTemplate.AWAITING_REVIEW),
        (RunStatus.APPROVED, ReplyTemplate.PROCESSING),
        (RunStatus.EXECUTING, ReplyTemplate.PROCESSING),
        (RunStatus.NEEDS_RECONCILIATION, ReplyTemplate.PROCESSING),
    ],
)
async def test_a_message_while_a_proposal_is_pending_gets_a_status_template(
    world: World, async_runtime_engine: AsyncEngine, status: RunStatus, template: ReplyTemplate
) -> None:
    if status is RunStatus.AWAIT_APPROVAL:
        run = world.add_run(status)
    else:
        run = world.add_refund_run(world.add_order(), 2_000, status)
    conversation = world.row(run).conversation_id

    started = await start(async_runtime_engine, world, conversation, "any news?")

    assert started.answered is template and started.ids.run_id == run
    assert kinds(world, conversation)[-2:] == [
        ("customer_message", {"text": "any news?", **CLEAN}),
        ("reply", {"template": template.value}),
    ]
    assert world.conversation(conversation).active_turn_id is None  # handed straight back
    assert world.row(run).status == status


@pytest.mark.parametrize(("message", "reason"), [("x" * 51, "too_long"), ("   ", "empty")])
async def test_an_oversized_or_empty_message_is_refused_and_nothing_is_saved(
    world: World, async_runtime_engine: AsyncEngine, message: str, reason: str
) -> None:
    conversation = world.add_conversation()

    with pytest.raises(MessageRejected, match=reason):
        await start(async_runtime_engine, world, conversation, message)

    assert kinds(world, conversation) == []
    assert world.conversation(conversation).active_turn_id is None
    assert world.runs_of(conversation) == []


async def test_the_start_transaction_runs_with_the_turn_timeouts(
    world: World, async_runtime_engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[tuple[str, str]] = []
    original = turns.unfinished_run

    async def reading_settings(
        conn: AsyncConnection, conversation_id: uuid.UUID
    ) -> turns.UnfinishedRun | None:
        row = (
            await conn.execute(
                text("SELECT current_setting('statement_timeout'), current_setting('lock_timeout')")
            )
        ).one()
        seen.append((row[0], row[1]))
        return await original(conn, conversation_id)

    monkeypatch.setattr(turns, "unfinished_run", reading_settings)

    await start(async_runtime_engine, world, world.add_conversation())

    assert seen == [("5s", "2s")]


# --- Finish -------------------------------------------------------------------------------------


async def test_a_reply_completes_the_run_and_frees_the_conversation(
    world: World, async_runtime_engine: AsyncEngine
) -> None:
    conversation = world.add_conversation()
    started = await start(async_runtime_engine, world, conversation)

    await reply_and_complete(
        async_runtime_engine, SETTINGS, started.ids, "It shipped today.", clock=DATABASE_CLOCK
    )

    assert world.row(started.ids.run_id).status == RunStatus.COMPLETED
    assert kinds(world, conversation)[-1] == ("reply", {"text": "It shipped today."})
    next_turn = await start(async_runtime_engine, world, conversation)  # no 409
    assert next_turn.ids.run_id != started.ids.run_id  # a new run for a new message


async def test_try_again_leaves_the_run_waiting_and_records_its_cause(
    world: World, async_runtime_engine: AsyncEngine
) -> None:
    conversation = world.add_conversation()
    started = await start(async_runtime_engine, world, conversation)

    await reply_and_wait(
        async_runtime_engine,
        SETTINGS,
        started.ids,
        ReplyTemplate.TRY_AGAIN,
        TryAgainCause.PROVIDER,
        clock=DATABASE_CLOCK,
    )

    assert world.row(started.ids.run_id).status == RunStatus.AGENT_STEP
    assert kinds(world, conversation)[-1] == (
        "reply",
        {"template": "try_again", "cause": "provider"},
    )
    resumed = await start(async_runtime_engine, world, conversation)
    assert resumed.ids.run_id == started.ids.run_id


async def test_a_turn_that_lost_its_lease_writes_nothing(
    world: World, async_runtime_engine: AsyncEngine
) -> None:
    conversation = world.add_conversation()
    started = await start(async_runtime_engine, world, conversation)
    world.hold_turn(started.ids.run_id)  # another turn takes the conversation over

    with pytest.raises(LostTurn):
        await reply_and_complete(
            async_runtime_engine, SETTINGS, started.ids, "stale", clock=DATABASE_CLOCK
        )
    with pytest.raises(LostTurn):
        await reply_and_wait(
            async_runtime_engine,
            SETTINGS,
            started.ids,
            ReplyTemplate.REPHRASE,
            clock=DATABASE_CLOCK,
        )

    assert world.row(started.ids.run_id).status == RunStatus.AGENT_STEP
    assert [k for k, _ in kinds(world, conversation)] == ["customer_message"]


async def test_try_again_needs_its_cause(async_runtime_engine: AsyncEngine) -> None:
    ids = TurnIds(uuid.uuid4(), uuid.uuid4(), uuid.uuid4())

    with pytest.raises(ValueError, match="cause"):
        await reply_and_wait(
            async_runtime_engine, SETTINGS, ids, ReplyTemplate.TRY_AGAIN, clock=DATABASE_CLOCK
        )


# --- The heartbeat --------------------------------------------------------------------------------


async def test_the_heartbeat_keeps_the_lease_while_the_turn_works(
    world: World, async_runtime_engine: AsyncEngine
) -> None:
    conversation = world.add_conversation()
    started = await start_turn(
        async_runtime_engine,
        FAST,
        conversation_id=conversation,
        user=world.user,
        message="hi",
        clock=DATABASE_CLOCK,
        new_id=uuid.uuid4,
    )

    async def slow_work() -> str:
        await asyncio.sleep(0.9)  # longer than the 0.6 s lease
        return "done"

    async with asyncio.timeout(BOUND):
        result = await hold_lease(
            async_runtime_engine, FAST, started.ids, time.monotonic() + 30, slow_work
        )

    assert result == "done"
    assert world.conversation(conversation).active_turn_id == started.ids.turn_id
    assert world.seconds_until_lease(conversation) > 0


async def test_a_takeover_cancels_the_work_with_a_lost_turn(
    world: World, async_runtime_engine: AsyncEngine
) -> None:
    conversation = world.add_conversation()
    started = await start_turn(
        async_runtime_engine,
        FAST,
        conversation_id=conversation,
        user=world.user,
        message="hi",
        clock=DATABASE_CLOCK,
        new_id=uuid.uuid4,
    )
    cancelled = asyncio.Event()

    async def work() -> None:
        try:
            await asyncio.to_thread(world.hold_turn, started.ids.run_id)
            await asyncio.sleep(BOUND)
        except asyncio.CancelledError:
            cancelled.set()
            raise

    async with asyncio.timeout(BOUND):
        with pytest.raises(LostTurn):
            await hold_lease(async_runtime_engine, FAST, started.ids, time.monotonic() + 30, work)

    assert cancelled.is_set()


async def test_a_turn_past_its_deadline_stops_renewing(
    world: World, async_runtime_engine: AsyncEngine
) -> None:
    conversation = world.add_conversation()
    started = await start_turn(
        async_runtime_engine,
        FAST,
        conversation_id=conversation,
        user=world.user,
        message="hi",
        clock=DATABASE_CLOCK,
        new_id=uuid.uuid4,
    )

    async def stuck() -> None:
        await asyncio.sleep(BOUND * 2)  # outlives the outer bound, so only the overrun can end it

    async with asyncio.timeout(BOUND):
        with pytest.raises(TurnOverrun):
            await hold_lease(async_runtime_engine, FAST, started.ids, time.monotonic() + 0.2, stuck)


# --- The tool connection ----------------------------------------------------------------


@pytest.fixture
async def single_connection_engine() -> AsyncIterator[AsyncEngine]:
    """One pooled connection, so the next checkout is provably the same one."""
    from conftest import env

    engine = create_async_engine(env("AGENT_DB_RUNTIME_URL"), pool_size=1, max_overflow=0)
    yield engine
    await engine.dispose()


async def test_a_tool_transaction_has_the_turn_timeouts_and_they_dont_leak(
    single_connection_engine: AsyncEngine,
) -> None:
    query = text("SELECT current_setting('statement_timeout'), current_setting('lock_timeout')")
    async with tool_connection(single_connection_engine, SETTINGS) as conn:
        async with conn.begin():  # the tool opens its own transaction (ADR-020)
            inside = tuple((await conn.execute(query)).one())
        pid = await conn.scalar(text("SELECT pg_backend_pid()"))

    async with single_connection_engine.connect() as again:
        after = tuple((await again.execute(query)).one())
        same_pid = await again.scalar(text("SELECT pg_backend_pid()"))

    assert inside == ("5s", "2s")
    assert same_pid == pid  # the same pooled connection came back
    assert after != inside  # reset to the server defaults before it went back
