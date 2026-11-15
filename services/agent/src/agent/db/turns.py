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

from agent.core.c01 import C01_VERSION
from agent.core.c01 import RULE_ID as C01_RULE_ID
from agent.core.lifecycle import TERMINAL_STATUSES, RunStatus, require_transition
from agent.core.policy import POLICY_VERSION
from agent.db.presend import DECIDED_BY_POLICY
from agent.db.schema import conversations, decisions, events, runs

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


async def escalate_for_c01(conn: AsyncConnection, run_id: uuid.UUID) -> None:
    """C01 during ``AGENT_STEP``: ``→ ESCALATED`` with ``c01_flagged_at`` (ADR-009)."""
    require_transition(RunStatus.AGENT_STEP, RunStatus.ESCALATED)
    result = await conn.execute(
        update(runs)
        .where(runs.c.id == run_id, runs.c.status == RunStatus.AGENT_STEP)
        .values(status=RunStatus.ESCALATED, escalated_at=func.now(), c01_flagged_at=func.now())
        .returning(runs.c.id)
    )
    if result.one_or_none() is None:
        raise RuntimeError(f"run {run_id} left AGENT_STEP while this turn held its conversation")


async def flag_pending_for_c01(conn: AsyncConnection, run_id: uuid.UUID) -> str | None:
    """C01 after a proposal: flag the run for reviewers and bump ``review_revision`` (A03), so
    an approval from a page that predates the message fails. The status is untouched: an
    in-flight or queued refund isn't cancelled (ADR-009). Returns the run's action id."""
    result = await conn.execute(
        update(runs)
        .where(runs.c.id == run_id)
        .values(
            c01_flagged_at=func.coalesce(runs.c.c01_flagged_at, func.now()),
            escalated_at=func.coalesce(runs.c.escalated_at, func.now()),
            review_revision=runs.c.review_revision + 1,
        )
        .returning(runs.c.action_id)
    )
    action_id: str | None = result.scalar_one()
    return action_id


async def record_c01_decision(
    conn: AsyncConnection,
    run_id: uuid.UUID,
    action_id: str | None,
    outcome: str,
    patterns: tuple[str, ...],
) -> None:
    """The decision log keeps the matched pattern IDs only, never the message (ADR-009)."""
    await conn.execute(
        insert(decisions).values(
            run_id=run_id,
            action_id=action_id,
            stage="conversation_escalation",
            outcome=outcome,
            rule_ids=[C01_RULE_ID],
            policy_version=POLICY_VERSION,
            config_snapshot={"c01_version": C01_VERSION},
            input_snapshot={"patterns": list(patterns)},
            decided_by=DECIDED_BY_POLICY,
        )
    )


async def window_events(
    conn: AsyncConnection, conversation_id: uuid.UUID, limit: int
) -> list[tuple[str, dict[str, Any]]]:
    """The last ``limit`` message events of the conversation, across runs, oldest first.
    Tool calls and results are never stored here, so they can't reach a later prompt."""
    result = await conn.execute(
        select(events.c.kind, events.c.payload)
        .where(
            events.c.conversation_id == conversation_id,
            events.c.kind.in_((CUSTOMER_MESSAGE, REPLY)),
        )
        .order_by(events.c.id.desc())
        .limit(limit)
    )
    return [(row.kind, row.payload) for row in reversed(result.all())]


async def trailing_provider_failures(
    conn: AsyncConnection, conversation_id: uuid.UUID, limit: int
) -> int:
    """How many of the conversation's most recent replies, in an unbroken run, were
    ``try_again`` caused by the provider (ADR-022 Part 2). Lock-timeout ones break the run."""
    result = await conn.execute(
        select(events.c.payload)
        .where(events.c.conversation_id == conversation_id, events.c.kind == REPLY)
        .order_by(events.c.id.desc())
        .limit(limit)
    )
    count = 0
    for payload in result.scalars():
        if payload.get("template") != "try_again" or payload.get("cause") != "provider":
            break
        count += 1
    return count


async def escalate_run(conn: AsyncConnection, run_id: uuid.UUID) -> None:
    """``AGENT_STEP → ESCALATED`` when the turn's step or malformed budget runs out (ADR-008)."""
    require_transition(RunStatus.AGENT_STEP, RunStatus.ESCALATED)
    result = await conn.execute(
        update(runs)
        .where(runs.c.id == run_id, runs.c.status == RunStatus.AGENT_STEP)
        .values(status=RunStatus.ESCALATED, escalated_at=func.now())
        .returning(runs.c.id)
    )
    if result.one_or_none() is None:
        raise RuntimeError(f"run {run_id} left AGENT_STEP while this turn held its conversation")
