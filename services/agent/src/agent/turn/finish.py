"""Ending a turn (ADR-022 S1, Part 2, S5): every finisher is fenced on the lease.

- ``reply_and_complete``: the model's text is the reply; ``AGENT_STEP → COMPLETED``, the reply
  event and the lease release in one transaction.
- ``escalate_and_reply``: a budget ran out; ``AGENT_STEP → ESCALATED`` with ``needs_person``.
  The reason goes to the server log only, never the customer's stream (ADR-009 non-disclosure).
- ``reply_and_wait``: a template (``try_again`` with its cause, ``rephrase``); the run stays in
  ``AGENT_STEP`` and the next message resumes it.

A turn that no longer holds the conversation writes nothing and raises ``LostTurn``.
"""

import logging
from enum import StrEnum

from sqlalchemy.ext.asyncio import AsyncEngine

from agent.core.replies import ReplyTemplate
from agent.db import turns
from agent.db.claims import worker_transaction
from agent.db.clock import Clock
from agent.tools.contract import LostTurn
from agent.turn.settings import TurnSettings
from agent.turn.start import TurnIds

logger = logging.getLogger(__name__)


class TryAgainCause(StrEnum):
    PROVIDER = "provider"  # counted toward the provider-failure alert (ADR-022 Part 2)
    LOCK = "lock"  # ADR-009's proposal-time lock timeout; not counted


async def reply_and_complete(
    engine: AsyncEngine, settings: TurnSettings, ids: TurnIds, text: str, *, clock: Clock
) -> None:
    async with worker_transaction(
        engine, statement_timeout=settings.statement_timeout, lock_timeout=settings.lock_timeout
    ) as conn:
        if not await turns.hold_turn(conn, ids.conversation_id, ids.turn_id):
            raise LostTurn(f"turn {ids.turn_id} lost conversation {ids.conversation_id}")
        at = clock.stamp()
        await turns.append_event(
            conn, ids.conversation_id, ids.run_id, turns.REPLY, {"text": text}, at=at
        )
        await turns.complete_run(conn, ids.run_id)
        await turns.release_lease(conn, ids.conversation_id, ids.turn_id)


async def reply_and_wait(
    engine: AsyncEngine,
    settings: TurnSettings,
    ids: TurnIds,
    template: ReplyTemplate,
    cause: TryAgainCause | None = None,
    *,
    clock: Clock,
) -> None:
    if (template is ReplyTemplate.TRY_AGAIN) != (cause is not None):
        raise ValueError("a try_again reply needs its cause, and only it has one")
    payload = {"template": template.value}
    if cause is not None:
        payload["cause"] = cause.value
    async with worker_transaction(
        engine, statement_timeout=settings.statement_timeout, lock_timeout=settings.lock_timeout
    ) as conn:
        if not await turns.hold_turn(conn, ids.conversation_id, ids.turn_id):
            raise LostTurn(f"turn {ids.turn_id} lost conversation {ids.conversation_id}")
        await turns.append_event(
            conn, ids.conversation_id, ids.run_id, turns.REPLY, payload, at=clock.stamp()
        )
        await turns.release_lease(conn, ids.conversation_id, ids.turn_id)


async def escalate_and_reply(
    engine: AsyncEngine, settings: TurnSettings, ids: TurnIds, reason: str, *, clock: Clock
) -> None:
    async with worker_transaction(
        engine, statement_timeout=settings.statement_timeout, lock_timeout=settings.lock_timeout
    ) as conn:
        if not await turns.hold_turn(conn, ids.conversation_id, ids.turn_id):
            raise LostTurn(f"turn {ids.turn_id} lost conversation {ids.conversation_id}")
        at = clock.stamp()
        await turns.escalate_run(conn, ids.run_id, reason=reason, at=at)
        await turns.append_event(
            conn,
            ids.conversation_id,
            ids.run_id,
            turns.REPLY,
            {"template": ReplyTemplate.NEEDS_PERSON.value},
            at=at,
        )
        await turns.release_lease(conn, ids.conversation_id, ids.turn_id)
    logger.info("run %s escalated: %s", ids.run_id, reason)
