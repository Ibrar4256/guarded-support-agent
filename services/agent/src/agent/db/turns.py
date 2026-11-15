"""Database side of the turn (ADR-008 turn lease, ADR-022 S1, S5).

Nothing here commits on its own; the turn owns its transactions. Lock order (ADR-009):
``customers`` → ``conversations`` → ``runs``. Every write a turn makes after its start is
conditional on ``conversations.active_turn_id = :turn`` under ``FOR SHARE`` (``hold_turn``), so a
takeover waits for the write's transaction instead of committing under it.
"""

import uuid
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from sqlalchemy import func, insert, select, update
from sqlalchemy.ext.asyncio import AsyncConnection

from agent.core.lifecycle import TERMINAL_STATUSES, RunStatus, require_transition
from agent.db.schema import conversations, events, runs

CUSTOMER_MESSAGE = "customer_message"
REPLY = "reply"


@dataclass(frozen=True)
class UnfinishedRun:
    run_id: uuid.UUID
    status: RunStatus


async def take_lease(
    conn: AsyncConnection,
    conversation_id: uuid.UUID,
    user: str,
    turn_id: uuid.UUID,
    lease: timedelta,
) -> bool:
    """ADR-008's turn start: free, or held by an expired turn. False when another turn holds it."""
    result = await conn.execute(
        update(conversations)
        .where(
            conversations.c.id == conversation_id,
            conversations.c.user_id == user,
            (conversations.c.active_turn_id.is_(None))
            | (conversations.c.turn_expires_at < func.now()),
        )
        .values(active_turn_id=turn_id, turn_expires_at=func.now() + lease)
        .returning(conversations.c.id)
    )
    return result.one_or_none() is not None


async def owns_conversation(conn: AsyncConnection, conversation_id: uuid.UUID, user: str) -> bool:
    result = await conn.execute(
        select(conversations.c.id).where(
            conversations.c.id == conversation_id, conversations.c.user_id == user
        )
    )
    return result.one_or_none() is not None


async def renew_lease(
    conn: AsyncConnection, conversation_id: uuid.UUID, turn_id: uuid.UUID, lease: timedelta
) -> bool:
    result = await conn.execute(
        update(conversations)
        .where(conversations.c.id == conversation_id, conversations.c.active_turn_id == turn_id)
        .values(turn_expires_at=func.now() + lease)
        .returning(conversations.c.id)
    )
    return result.one_or_none() is not None


async def hold_turn(conn: AsyncConnection, conversation_id: uuid.UUID, turn_id: uuid.UUID) -> bool:
    """The write fence (S5): this turn still holds the conversation, and keeps holding it
    until this transaction ends."""
    result = await conn.execute(
        select(conversations.c.id)
        .where(conversations.c.id == conversation_id, conversations.c.active_turn_id == turn_id)
        .with_for_update(read=True)
    )
    return result.one_or_none() is not None


async def release_lease(
    conn: AsyncConnection, conversation_id: uuid.UUID, turn_id: uuid.UUID
) -> None:
    await conn.execute(
        update(conversations)
        .where(conversations.c.id == conversation_id, conversations.c.active_turn_id == turn_id)
        .values(active_turn_id=None, turn_expires_at=None)
    )


async def unfinished_run(conn: AsyncConnection, conversation_id: uuid.UUID) -> UnfinishedRun | None:
    """At most one exists (the partial unique index, ADR-009). Read without a row lock: a
    pending run may be held by a worker mid-send, and a status read must never wait on that."""
    result = await conn.execute(
        select(runs.c.id, runs.c.status).where(
            runs.c.conversation_id == conversation_id,
            runs.c.status.not_in(list(TERMINAL_STATUSES)),
        )
    )
    row = result.one_or_none()
    return None if row is None else UnfinishedRun(row.id, RunStatus(row.status))


async def create_run(conn: AsyncConnection, conversation_id: uuid.UUID, user: str) -> uuid.UUID:
    require_transition(None, RunStatus.AGENT_STEP)
    run_id = uuid.uuid4()
    await conn.execute(
        insert(runs).values(
            id=run_id, conversation_id=conversation_id, user_id=user, status=RunStatus.AGENT_STEP
        )
    )
    return run_id


async def append_event(
    conn: AsyncConnection,
    conversation_id: uuid.UUID,
    run_id: uuid.UUID | None,
    kind: str,
    payload: dict[str, Any],
) -> None:
    await conn.execute(
        insert(events).values(
            conversation_id=conversation_id, run_id=run_id, kind=kind, payload=payload
        )
    )


async def complete_run(conn: AsyncConnection, run_id: uuid.UUID) -> None:
    """``AGENT_STEP → COMPLETED`` on a reply with no proposal (ADR-008)."""
    require_transition(RunStatus.AGENT_STEP, RunStatus.COMPLETED)
    result = await conn.execute(
        update(runs)
        .where(runs.c.id == run_id, runs.c.status == RunStatus.AGENT_STEP)
        .values(status=RunStatus.COMPLETED)
        .returning(runs.c.id)
    )
    if result.one_or_none() is None:
        raise RuntimeError(f"run {run_id} left AGENT_STEP while this turn held its conversation")
