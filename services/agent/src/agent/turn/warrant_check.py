"""The warrant step (ADR-029 S2, W01): between the loop's proposal and the proposal transaction.

A short read with no customer lock, then one call to the ``warrant`` purpose with its own
deadline, inside the turn's. Every failure (too little time left, a provider error or timeout,
a malformed answer, anything unexpected) is ``unavailable``, which sends an automatic refund to
review: the check can only raise severity, never approve (ADR-009 asymmetry).
"""

import logging
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncEngine

from agent.core.policy import RefundProposal, WarrantStatus, WarrantVerdict
from agent.core.warrant import judge
from agent.db import warrant as warrant_db
from agent.db.claims import worker_transaction
from agent.llm.pool import ProviderPool
from agent.llm.prices import PriceTable
from agent.llm.recording import CallContext, Recorder, RecordingModel
from agent.llm.types import Purpose
from agent.turn.settings import TurnSettings
from agent.turn.warrant import (
    WARRANT_MAX_ORDERS,
    WARRANT_TOOL,
    parse_verdict,
    warrant_messages,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class WarrantEnv:
    engine: AsyncEngine
    settings: TurnSettings
    pool: ProviderPool
    record: Recorder
    prices: PriceTable
    monotonic: Callable[[], float]
    nonce: Callable[[], str]


@dataclass(frozen=True)
class WarrantRequest:
    conversation_id: uuid.UUID
    run_id: uuid.UUID
    turn_id: uuid.UUID
    user: str
    proposal: RefundProposal
    deadline: float  # the turn's, on ``monotonic``


WarrantCheck = Callable[[WarrantEnv, WarrantRequest], Awaitable[WarrantVerdict]]


def bound_verdict(
    proposal: RefundProposal,
    status: WarrantStatus,
    *,
    problem: str | None = None,
    model: str | None = None,
) -> WarrantVerdict:
    return WarrantVerdict(
        status, problem, proposal.action_id, proposal.order_id, proposal.amount_minor, model
    )


async def check_warrant(env: WarrantEnv, request: WarrantRequest) -> WarrantVerdict:
    proposal, run = request.proposal, request.run_id
    budget = env.settings.warrant_deadline.total_seconds()
    left = request.deadline - env.monotonic()
    if left < budget:
        logger.warning("run %s: warrant check skipped, %.1f s of the turn left", run, left)
        return bound_verdict(proposal, WarrantStatus.UNAVAILABLE)
    model_label: str | None = None
    try:
        async with worker_transaction(
            env.engine,
            statement_timeout=env.settings.statement_timeout,
            lock_timeout=env.settings.lock_timeout,
        ) as conn:
            facts = await warrant_db.load_warrant_facts(
                conn,
                conversation_id=request.conversation_id,
                user=request.user,
                refund_order_id=proposal.order_id,
                max_orders=WARRANT_MAX_ORDERS,
            )
        messages = warrant_messages(
            order_id=proposal.order_id,
            orders=facts.orders,
            customer_messages=facts.customer_messages,
            nonce=env.nonce(),
            max_chars=env.settings.window_max_chars,
        )
        pooled = env.pool.model_for(Purpose.WARRANT)
        model_label = f"{pooled.provider}:{pooled.model_name}"
        model = RecordingModel(
            pooled,
            provider=pooled.provider,
            model=pooled.model_name,
            record=env.record,
            context=CallContext(request.conversation_id, run, request.turn_id),
            prices=env.prices,
            monotonic=env.monotonic,
        )
        response = await model.complete(
            messages,
            [WARRANT_TOOL],
            deadline=min(request.deadline, env.monotonic() + budget),
            purpose=Purpose.WARRANT,
        )
        answer = parse_verdict(response)
    except Exception as failure:  # every failure is a review, never an approval
        logger.warning(
            "run %s: warrant check unavailable: %s: %s", run, type(failure).__name__, failure
        )
        return bound_verdict(proposal, WarrantStatus.UNAVAILABLE, model=model_label)
    status = judge(
        warranted=answer.warranted,
        order_id=answer.order_id,
        problem=answer.problem,
        quote=answer.complaint_quote,
        proposal_order_id=proposal.order_id,
        customer_messages=facts.customer_messages,
    )
    logger.info("run %s: warrant %s (%s)", run, status.value, answer.problem)
    return bound_verdict(proposal, status, problem=answer.problem, model=model_label)
