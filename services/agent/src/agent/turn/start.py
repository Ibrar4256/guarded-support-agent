"""The turn's start transaction (ADR-022 S1 step 1, S9; ADR-008 turn lease; ADR-009).

One short transaction: take the lease, find or create the run, save the customer's message.
A message while a proposal is pending gets a status template and the lease back at once: it
never reaches the model (ADR-009). An oversized or empty message is refused before anything is
written. A missing conversation and another customer's get the same ``ConversationNotFound``,
so the start can't be used to learn which conversation ids exist.
"""

import uuid
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncEngine

from agent.core.lifecycle import RunStatus
from agent.core.replies import ReplyTemplate
from agent.db import turns
from agent.db.claims import worker_transaction
from agent.turn.settings import TurnSettings


class MessageRejected(Exception):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class ConversationNotFound(Exception):
    pass


class TurnInProgress(Exception):
    """Another turn holds the conversation (the chat API answers 409)."""


@dataclass(frozen=True)
class TurnIds:
    conversation_id: uuid.UUID
    run_id: uuid.UUID
    turn_id: uuid.UUID


@dataclass(frozen=True)
class TurnStart:
    ids: TurnIds
    user: str
    status: RunStatus
    # Set when the start already answered (a pending proposal): no loop, no model call.
    answered: ReplyTemplate | None


PENDING_TEMPLATES = {
    RunStatus.AWAIT_APPROVAL: ReplyTemplate.AWAITING_REVIEW,
    RunStatus.APPROVED: ReplyTemplate.PROCESSING,
    RunStatus.EXECUTING: ReplyTemplate.PROCESSING,
    RunStatus.NEEDS_RECONCILIATION: ReplyTemplate.PROCESSING,
}


def check_message(message: str, settings: TurnSettings) -> None:
    if not message.strip():
        raise MessageRejected("empty")
    if len(message) > settings.message_max_chars:
        raise MessageRejected("too_long")


async def start_turn(
    engine: AsyncEngine,
    settings: TurnSettings,
    *,
    conversation_id: uuid.UUID,
    user: str,
    message: str,
) -> TurnStart:
    check_message(message, settings)
    turn_id = uuid.uuid4()
    async with worker_transaction(
        engine, statement_timeout=settings.statement_timeout, lock_timeout=settings.lock_timeout
    ) as conn:
        if not await turns.take_lease(conn, conversation_id, user, turn_id, settings.lease):
            if await turns.owns_conversation(conn, conversation_id, user):
                raise TurnInProgress(str(conversation_id))
            raise ConversationNotFound(str(conversation_id))
        found = await turns.unfinished_run(conn, conversation_id)
        if found is None:
            run_id = await turns.create_run(conn, conversation_id, user)
            status = RunStatus.AGENT_STEP
        else:
            run_id, status = found.run_id, found.status
        await turns.append_event(
            conn, conversation_id, run_id, turns.CUSTOMER_MESSAGE, {"text": message}
        )
        ids = TurnIds(conversation_id, run_id, turn_id)
        template = PENDING_TEMPLATES.get(status)
        if template is not None:
            await turns.append_event(
                conn, conversation_id, run_id, turns.REPLY, {"template": template.value}
            )
            await turns.release_lease(conn, conversation_id, turn_id)
        return TurnStart(ids, user, status, template)
