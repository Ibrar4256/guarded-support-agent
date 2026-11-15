"""One customer message, start to finish (ADR-022 S1): start, loop under the lease, finish.

A proposal finishes through ADR-009's proposal path (``turn.propose``) after the loop, with no
further model call.

The turn never holds a database connection while waiting on the model (S4): the window is read
in its own short transaction, each tool call checks out its own connection, and the finish is
one fenced transaction. A turn that loses its lease or runs past its deadline writes nothing
more (``LOST``); the next message resumes the run.
"""

import logging
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum

from sqlalchemy.ext.asyncio import AsyncEngine

from agent.alerts import Alerter
from agent.core.policy import PolicyConfig
from agent.db import turns
from agent.db.claims import worker_transaction
from agent.llm.pool import ProviderPool
from agent.llm.prices import PriceTable
from agent.llm.recording import CallContext, Recorder, RecordingModel
from agent.tools.contract import LostTurn
from agent.turn.finish import TryAgainCause, escalate_and_reply, reply_and_complete, reply_and_wait
from agent.turn.lease import TurnOverrun, hold_lease
from agent.turn.loop import Escalate, LoopContext, LoopOutcome, Propose, Reply, Wait, run_loop
from agent.turn.prompt import MessageEvent, build_messages, new_nonce, window
from agent.turn.propose import ProposalContext, ProposalStep, run_proposal
from agent.turn.settings import TurnSettings
from agent.turn.start import TurnIds, start_turn

logger = logging.getLogger(__name__)

PROVIDER_FAILURES_ALERT = "conversation_provider_failures"


class TurnOutcome(StrEnum):
    ANSWERED_AT_START = "answered_at_start"  # a pending proposal or C01: no model call
    REPLIED = "replied"
    APPROVED = "approved"  # a proposal policy approved; the worker takes it from here
    AWAITING_REVIEW = "awaiting_review"  # a proposal waiting for a reviewer
    WAITING = "waiting"  # try_again (provider, deadline, proposal lock) or rephrase; AGENT_STEP
    ESCALATED = "escalated"
    LOST = "lost"  # the lease was lost or the turn overran; nothing more was written


@dataclass(frozen=True)
class TurnDeps:
    engine: AsyncEngine
    settings: TurnSettings
    pool: ProviderPool
    record: Recorder
    prices: PriceTable
    alerter: Alerter
    policy: PolicyConfig
    monotonic: Callable[[], float] = field(default=time.monotonic)


@dataclass(frozen=True)
class TurnResult:
    ids: TurnIds
    outcome: TurnOutcome


async def run_turn(
    deps: TurnDeps, *, conversation_id: uuid.UUID, user: str, message: str
) -> TurnResult:
    started = await start_turn(
        deps.engine, deps.settings, conversation_id=conversation_id, user=user, message=message
    )
    ids = started.ids
    if started.answered is not None:
        return TurnResult(ids, TurnOutcome.ANSWERED_AT_START)
    deadline = deps.monotonic() + deps.settings.deadline.total_seconds()
    try:
        outcome = await hold_lease(
            deps.engine,
            deps.settings,
            ids,
            deadline,
            lambda: _work(deps, ids, user, deadline),
            monotonic=deps.monotonic,
        )
        return TurnResult(ids, await _finish(deps, ids, user, outcome))
    except (LostTurn, TurnOverrun) as lost:
        logger.warning("turn %s gave up its conversation: %s", ids.turn_id, lost)
        return TurnResult(ids, TurnOutcome.LOST)


async def _work(deps: TurnDeps, ids: TurnIds, user: str, deadline: float) -> LoopOutcome:
    settings = deps.settings
    async with worker_transaction(
        deps.engine,
        statement_timeout=settings.statement_timeout,
        lock_timeout=settings.lock_timeout,
    ) as conn:
        events = await turns.window_events(conn, ids.conversation_id, settings.window_messages)
    history = window(
        [MessageEvent(kind, payload) for kind, payload in events],
        max_chars=settings.window_max_chars,
    )
    pooled = deps.pool.for_turn()
    model = RecordingModel(
        pooled,
        provider=pooled.provider,
        model=pooled.model_name,
        record=deps.record,
        context=CallContext(ids.conversation_id, ids.run_id, ids.turn_id),
        prices=deps.prices,
        monotonic=deps.monotonic,
    )
    context = LoopContext(deps.engine, settings, ids, user, model, deadline, deps.monotonic)
    return await run_loop(context, build_messages(history, nonce=new_nonce()))


PROPOSAL_OUTCOMES = {
    ProposalStep.APPROVED: TurnOutcome.APPROVED,
    ProposalStep.AWAITING_REVIEW: TurnOutcome.AWAITING_REVIEW,
    ProposalStep.ESCALATED: TurnOutcome.ESCALATED,
    ProposalStep.TRY_AGAIN: TurnOutcome.WAITING,
}


async def _finish(deps: TurnDeps, ids: TurnIds, user: str, outcome: LoopOutcome) -> TurnOutcome:
    if isinstance(outcome, Propose):
        context = ProposalContext(deps.engine, deps.settings, deps.policy, deps.alerter, ids, user)
        return PROPOSAL_OUTCOMES[await run_proposal(context, outcome.args)]
    if isinstance(outcome, Reply):
        await reply_and_complete(deps.engine, deps.settings, ids, outcome.text)
        return TurnOutcome.REPLIED
    if isinstance(outcome, Escalate):
        await escalate_and_reply(deps.engine, deps.settings, ids, outcome.reason)
        return TurnOutcome.ESCALATED
    await reply_and_wait(deps.engine, deps.settings, ids, outcome.template, outcome.cause)
    _alert(deps, ids, outcome)
    if outcome.cause is TryAgainCause.PROVIDER:
        await _alert_on_repeated_provider_failures(deps, ids)
    return TurnOutcome.WAITING


def _alert(deps: TurnDeps, ids: TurnIds, outcome: Wait) -> None:
    if outcome.alert is not None:
        deps.alerter.alert(
            outcome.alert, run_id=ids.run_id, conversation_id=str(ids.conversation_id)
        )


async def _alert_on_repeated_provider_failures(deps: TurnDeps, ids: TurnIds) -> None:
    """ADR-022 Part 2: "this customer can't get an answer" is visible before anything escalates."""
    threshold = deps.settings.provider_failure_alert_after
    async with worker_transaction(
        deps.engine,
        statement_timeout=deps.settings.statement_timeout,
        lock_timeout=deps.settings.lock_timeout,
    ) as conn:
        streak = await turns.trailing_provider_failures(conn, ids.conversation_id, threshold)
    if streak >= threshold:
        deps.alerter.alert(
            PROVIDER_FAILURES_ALERT,
            run_id=ids.run_id,
            conversation_id=str(ids.conversation_id),
            in_a_row=streak,
        )
