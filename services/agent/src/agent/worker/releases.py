"""Releases that expect the failure to recur (ADR-005, ADR-014 S6, S12).

A lock-timeout streak or an unexpected error releases the claim with a cooldown, so the run
isn't reclaimed straight into the same failure, and counts toward ``release_streak`` (reset by
any progress). At the limit:
- an **APPROVED** run escalates: safe, nothing was sent;
- an **EXECUTING** run keeps its status (its outcome is unknown; only the age check or the
  reconciler may move it) and **pages**, with its ``run_id``, so a person looks.
"""

from enum import StrEnum

from agent.core.lifecycle import RunStatus
from agent.db import claims
from agent.db import presend as presend_db
from agent.db.claims import Claim
from agent.worker.context import WorkerContext


class Released(StrEnum):
    COOLDOWN = "cooldown"
    ESCALATED = "escalated"
    LOST = "lost"


async def release_with_cooldown(ctx: WorkerContext, claim: Claim, *, reason: str) -> Released:
    page_after_commit = False
    async with claims.worker_transaction(
        ctx.engine,
        statement_timeout=ctx.timings.statement_timeout,
        lock_timeout=ctx.timings.lock_timeout,
    ) as conn:
        held = await claims.lock_for_release(conn, claim)
        if held is None:
            return Released.LOST
        at_limit = held.release_streak + 1 >= ctx.timings.release_streak_limit
        if at_limit and held.status is RunStatus.APPROVED:
            if not await presend_db.escalate_unsent(conn, claim, held.conversation_id):
                return Released.LOST
            ctx.alerter.alert(
                "release_streak_limit", run_id=claim.run_id, status="APPROVED", reason=reason
            )
            return Released.ESCALATED
        released = await claims.release_claim(conn, claim, cooldown=ctx.timings.claim_cooldown)
        if not released.released:
            return Released.LOST
        page_after_commit = at_limit
    if page_after_commit:
        ctx.alerter.page(
            "release_streak_limit", run_id=claim.run_id, status=held.status.value, reason=reason
        )
    return Released.COOLDOWN
