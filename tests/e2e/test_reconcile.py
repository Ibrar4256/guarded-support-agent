"""The reconciler end to end (ADR-016, ADR-010): real processes, refund-api's fault build, the
agent's worker running both loops. Each test counts the refunds refund-api actually holds.

The in-process tests (services/agent/tests/integration/test_reconcile.py) plant the quiet-period
bugs; a real process has no seam for them. Here the zero-quiet control is the lookup itself: the
moment the run needs reconciliation, refund-api holds no refund, so a reconciler that didn't wait
would find nothing and reject a refund that then commits.
"""

from profile import SLOW_COMMIT_SECONDS
from typing import Any

from conftest import E2E

UNSETTLED = {"NEEDS_RECONCILIATION", "COMPLETED", "ESCALATED"}
SETTLED = {"COMPLETED", "ESCALATED"}


async def approved(e2e: E2E, amount: int) -> dict[str, Any]:
    """An approved run, and refund-api's charge for its order (twice the refund, ADR-004)."""
    made = await e2e.make_run(amount, charged_minor=amount * 2)
    await e2e.refund_api.create_charge(made["order_id"], amount * 2)
    return made


async def refunds(e2e: E2E, made: dict[str, Any]) -> int:
    return len(await e2e.refund_api.refunds(made["order_id"], made["action_id"]))


async def test_a_slow_commit_is_waited_out_and_confirmed(e2e: E2E) -> None:
    """The agent's attempts end (a timeout, then 409s) before refund-api commits; the reconciler
    waits out the quiet period, finds the refund and confirms it.

    A warm-up refund goes first, so the write breaker is closed (ADR-018): there the timeout
    counts once toward its threshold and the 409s are neutral, so the attempts run out as they
    would in a running worker. In a fresh worker the timeout would be the half-open probe, and
    the run would be paused instead of reconciled."""
    warm_up = await approved(e2e, 1_000)
    made = await approved(e2e, 2_000)
    await e2e.refund_api.plan(made["action_id"], "slow_commit", seconds=SLOW_COMMIT_SECONDS)
    worker = await e2e.start_worker()
    row = await e2e.until_status(made["run_id"], UNSETTLED)
    assert (row["status"], row["send_attempts"]) == ("NEEDS_RECONCILIATION", 3)
    assert await refunds(e2e, made) == 0, "a zero quiet period would find nothing here"
    assert (await e2e.run_row(warm_up["run_id"]))["status"] == "COMPLETED"
    row = await e2e.until_status(made["run_id"], SETTLED)
    assert (row["status"], row["outcome"]) == ("COMPLETED", "confirmed")
    assert "slow_commit" in await e2e.refund_api.applied(made["action_id"])
    assert await refunds(e2e, made) == 1
    assert await worker.terminate() == 0


async def test_an_invalid_request_after_a_commit_is_confirmed_never_rejected(e2e: E2E) -> None:
    """refund-api commits, then answers 400 invalid_request. That says nothing about whether
    money moved (ADR-010), so the run is reconciled, finds the refund, and is never resent."""
    made = await approved(e2e, 2_000)
    await e2e.refund_api.plan(made["action_id"], "invalid_request_after_commit")
    worker = await e2e.start_worker()
    row = await e2e.until_status(made["run_id"], UNSETTLED)
    assert (row["status"], row["send_attempts"]) == ("NEEDS_RECONCILIATION", 1)
    assert await refunds(e2e, made) == 1, "refund-api committed before answering"
    row = await e2e.until_status(made["run_id"], SETTLED)
    assert (row["status"], row["outcome"], row["send_attempts"]) == ("COMPLETED", "confirmed", 1)
    assert "invalid_request_after_commit" in await e2e.refund_api.applied(made["action_id"])
    assert await refunds(e2e, made) == 1
    assert await worker.terminate() == 0
