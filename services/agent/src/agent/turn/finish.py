"""Ending a turn (ADR-022 S1, Part 2, S5): every finisher is fenced on the lease.

- ``reply_and_complete``: the model's text is the reply; ``AGENT_STEP → COMPLETED``, the reply
  event and the lease release in one transaction.
- ``reply_and_wait``: a template (``try_again`` with its cause, ``rephrase``); the run stays in
  ``AGENT_STEP`` and the next message resumes it.

A turn that no longer holds the conversation writes nothing and raises ``LostTurn``.
"""

from enum import StrEnum

from sqlalchemy.ext.asyncio import AsyncEngine

from agent.core.replies import ReplyTemplate
from agent.db import turns
from agent.db.claims import worker_transaction
from agent.tools.contract import LostTurn
from agent.turn.settings import TurnSettings
from agent.turn.start import TurnIds


class TryAgainCause(StrEnum):
    PROVIDER = "provider"  # counted toward the provider-failure alert (ADR-022 Part 2)
    LOCK = "lock"  # ADR-009's proposal-time lock timeout; not counted


async def reply_and_complete(
    engine: AsyncEngine, settings: TurnSettings, ids: TurnIds, text: str
) -> None:
    async with worker_transaction(
        engine, statement_timeout=settings.statement_timeout, lock_timeout=settings.lock_timeout
    ) as conn:
        if not await turns.hold_turn(conn, ids.conversation_id, ids.turn_id):
            raise LostTurn(f"turn {ids.turn_id} lost conversation {ids.conversation_id}")
        await turns.append_event(conn, ids.conversation_id, ids.run_id, turns.REPLY, {"text": text})
        await turns.complete_run(conn, ids.run_id)
        await turns.release_lease(conn, ids.conversation_id, ids.turn_id)


async def reply_and_wait(
    engine: AsyncEngine,
    settings: TurnSettings,
    ids: TurnIds,
    template: ReplyTemplate,
    cause: TryAgainCause | None = None,
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
        await turns.append_event(conn, ids.conversation_id, ids.run_id, turns.REPLY, payload)
        await turns.release_lease(conn, ids.conversation_id, ids.turn_id)
