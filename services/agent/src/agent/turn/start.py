"""The turn's start transaction (ADR-022 S1 step 1, S9; ADR-008 turn lease; ADR-009).

One short transaction: take the lease, find or create the run, save the customer's message,
and run C01 on it. C01 is deterministic, which is the only reason it may run inside this
transaction (ADR-022 S1). A C01 hit in ``AGENT_STEP`` escalates the run with ``needs_person``
and no model call; a hit after a proposal flags the run for reviewers and bumps its
``review_revision`` (A03), without touching the refund. C02's ids are stored with the message
for a later proposal to read (ADR-029 S1); they change nothing here.
A message while a proposal is pending gets a status template and the lease back at once: it
never reaches the model (ADR-009). An oversized or empty message is refused before anything is
written. A missing conversation and another customer's get the same ``ConversationNotFound``,
so the start can't be used to learn which conversation ids exist.
"""

import logging
import uuid
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncEngine

from agent.core import c02
from agent.core.c01 import classify_customer_message
from agent.core.lifecycle import RunStatus
from agent.core.replies import ReplyTemplate
from agent.db import turns
from agent.db.claims import worker_transaction
from agent.db.clock import Clock, IdSource
from agent.turn.settings import TurnSettings

logger = logging.getLogger(__name__)

C01_ESCALATED = "ESCALATED"
C01_FLAGGED = "FLAGGED"


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
    clock: Clock,
    new_id: IdSource,
) -> TurnStart:
    check_message(message, settings)
    c01 = classify_customer_message(message)
    signals = c02.classify_customer_message(message)
    turn_id = uuid.uuid4()
    async with worker_transaction(
        engine, statement_timeout=settings.statement_timeout, lock_timeout=settings.lock_timeout
    ) as conn:
        if not await turns.take_lease(conn, conversation_id, user, turn_id, settings.lease):
            if await turns.owns_conversation(conn, conversation_id, user):
                raise TurnInProgress(str(conversation_id))
            raise ConversationNotFound(str(conversation_id))
        at = clock.stamp()
        found = await turns.unfinished_run(conn, conversation_id)
        if found is None:
            run_id = await turns.create_run(conn, conversation_id, user, run_id=new_id(), at=at)
            status = RunStatus.AGENT_STEP
        else:
            run_id, status = found.run_id, found.status
        payload = {"text": message, "c02": list(signals.patterns), "c02_version": signals.version}
        await turns.append_event(
            conn, conversation_id, run_id, turns.CUSTOMER_MESSAGE, payload, at=at
        )
        if signals.flagged:
            logger.info("C02 on run %s: %s", run_id, ", ".join(signals.patterns))
        ids = TurnIds(conversation_id, run_id, turn_id)
        if c01.flagged:
            logger.info("C01 on run %s: %s", run_id, ", ".join(c01.patterns))
            if status is RunStatus.AGENT_STEP:
                await turns.escalate_for_c01(conn, run_id, at=at)
                await turns.record_c01_decision(
                    conn, run_id, None, C01_ESCALATED, c01.patterns, at=at
                )
                await turns.append_event(
                    conn,
                    conversation_id,
                    run_id,
                    turns.REPLY,
                    {"template": ReplyTemplate.NEEDS_PERSON.value},
                    at=at,
                )
                await turns.release_lease(conn, conversation_id, turn_id)
                return TurnStart(ids, user, RunStatus.ESCALATED, ReplyTemplate.NEEDS_PERSON)
            action_id = await turns.flag_pending_for_c01(conn, run_id, at=at)
            await turns.record_c01_decision(
                conn, run_id, action_id, C01_FLAGGED, c01.patterns, at=at
            )
        template = PENDING_TEMPLATES.get(status)
        if template is not None:
            await turns.append_event(
                conn, conversation_id, run_id, turns.REPLY, {"template": template.value}, at=at
            )
            await turns.release_lease(conn, conversation_id, turn_id)
        return TurnStart(ids, user, status, template)
