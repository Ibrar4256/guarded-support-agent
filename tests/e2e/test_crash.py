"""ADR-004's crash tests, end to end (ADR-015): real processes, refund-api's fault build, the
agent's crash-test worker. Each test counts the refunds refund-api actually holds.

ADR-004's test-data rule: every charge is at least twice its refund, so a ``dedup_disabled``
control's second refund isn't refused as ``refund_exceeds_charge``. Without that, those
controls could never fail.
"""

import asyncio
from profile import HANG_SECONDS, LEASE_SECONDS
from typing import Any

from conftest import E2E

CRASH_EXIT = 1
# Long enough for any lease left by a crashed worker to expire, and a resumed run to finish.
SETTLE = LEASE_SECONDS + 3


async def approved(
    e2e: E2E, amount: int, *, charged: int | None = None, charge: int | None = None
) -> dict[str, Any]:
    """An approved run, and refund-api's charge for its order (at least twice the refund)."""
    made = await e2e.make_run(amount, charged_minor=charged if charged is not None else amount * 2)
    await e2e.refund_api.create_charge(
        made["order_id"], charge if charge is not None else amount * 2
    )
    return made


async def refunds(e2e: E2E, made: dict[str, Any]) -> int:
    return len(await e2e.refund_api.refunds(made["order_id"], made["action_id"]))


async def crash(e2e: E2E, point: str) -> None:
    worker = await e2e.start_worker(crash_point=point)
    assert await worker.wait(30) == CRASH_EXIT, f"the worker crashed at {point}"


async def resume_to(e2e: E2E, made: dict[str, Any], statuses: set[str]) -> dict[str, Any]:
    worker = await e2e.start_worker()
    row = await e2e.until_status(made["run_id"], statuses)
    assert await worker.terminate() == 0
    return row


# --- before_refund_call: the attempt is recorded, nothing sent ----------------------------------


async def test_a_crash_before_the_call_is_resumed_into_exactly_one_refund(e2e: E2E) -> None:
    made = await approved(e2e, 2_000)
    await crash(e2e, "before_refund_call")
    row = await e2e.run_row(made["run_id"])
    assert (row["status"], row["send_attempts"]) == ("EXECUTING", 1), "recorded, then died"
    assert await refunds(e2e, made) == 0, "nothing reached refund-api"
    row = await resume_to(e2e, made, {"COMPLETED"})
    assert row["outcome"] == "confirmed"
    assert await refunds(e2e, made) == 1


async def test_negative_control_without_a_resumed_worker_there_is_no_refund(e2e: E2E) -> None:
    """The refund in the test above comes from the resume, not from the crashed worker."""
    made = await approved(e2e, 2_000)
    await crash(e2e, "before_refund_call")
    await asyncio.sleep(SETTLE)
    row = await e2e.run_row(made["run_id"])
    assert row["status"] == "EXECUTING"
    assert await refunds(e2e, made) == 0


# --- after_refund_response: refund-api committed, the agent never recorded it --------------------


async def test_a_crash_after_the_response_is_replayed_into_exactly_one_refund(e2e: E2E) -> None:
    made = await approved(e2e, 2_000)
    await crash(e2e, "after_refund_response")
    assert (await e2e.run_row(made["run_id"]))["status"] == "EXECUTING", "never recorded"
    assert await refunds(e2e, made) == 1, "refund-api committed before the crash"
    row = await resume_to(e2e, made, {"COMPLETED"})
    assert (row["outcome"], row["send_attempts"]) == ("confirmed", 2)
    assert await refunds(e2e, made) == 1, "the resumed send replayed the same key"


async def test_negative_control_without_deduplication_the_resume_refunds_twice(e2e: E2E) -> None:
    made = await approved(e2e, 2_000)
    await e2e.refund_api.plan(made["action_id"], "dedup_disabled")
    await crash(e2e, "after_refund_response")
    await resume_to(e2e, made, {"COMPLETED"})
    assert await refunds(e2e, made) == 2
    assert "dedup_disabled" in await e2e.refund_api.applied(made["action_id"])


async def test_a_tight_limit_isnt_rechecked_on_resume(e2e: E2E) -> None:
    """40 of a 50 order: re-checking after the crash would count it twice and escalate a refund
    that already happened (ADR-004 step 6). refund-api's charge is 80, per the data rule."""
    made = await approved(e2e, 4_000, charged=5_000, charge=8_000)
    await crash(e2e, "after_refund_response")
    row = await resume_to(e2e, made, {"COMPLETED", "ESCALATED", "NEEDS_RECONCILIATION"})
    assert (row["status"], row["outcome"]) == ("COMPLETED", "confirmed")
    assert await refunds(e2e, made) == 1


async def test_negative_control_the_tight_case_under_dedup_disabled_refunds_twice(e2e: E2E) -> None:
    made = await approved(e2e, 4_000, charged=5_000, charge=8_000)
    await e2e.refund_api.plan(made["action_id"], "dedup_disabled")
    await crash(e2e, "after_refund_response")
    await resume_to(e2e, made, {"COMPLETED"})
    assert await refunds(e2e, made) == 2


# --- timeout_after_commit: committed, the answer never arrives in time ---------------------------


async def test_a_timeout_after_commit_is_retried_with_the_same_key(e2e: E2E) -> None:
    """refund-api holds one fault plan per action, so this can't also run under
    ``dedup_disabled``; its control is that the timeout fired and a second attempt really went
    out: one refund after two sends means the key was deduplicated."""
    made = await approved(e2e, 2_000)
    await e2e.refund_api.plan(made["action_id"], "timeout_after_commit", seconds=HANG_SECONDS)
    row = await resume_to(e2e, made, {"COMPLETED"})
    assert (row["outcome"], row["send_attempts"]) == ("confirmed", 2)
    assert "timeout_after_commit" in await e2e.refund_api.applied(made["action_id"])
    assert await refunds(e2e, made) == 1


# --- a business error frees capacity (needs real charges) ----------------------------------------


async def test_a_refund_above_the_real_charge_is_rejected_and_frees_capacity(e2e: E2E) -> None:
    """refund-api's charge is 50 while the agent's order says 80: a 60 refund passes the agent's
    policy but refund-api refuses it (refund_exceeds_charge), so the run ends rejected and its
    60 no longer counts: a 40 refund on the same order then goes through (60 + 40 > 80)."""
    first = await e2e.make_run(6_000, charged_minor=8_000)
    await e2e.refund_api.create_charge(first["order_id"], 5_000)
    worker = await e2e.start_worker()
    row = await e2e.until_status(
        first["run_id"], {"ESCALATED", "COMPLETED", "NEEDS_RECONCILIATION"}
    )
    assert (row["status"], row["outcome"]) == ("ESCALATED", "rejected")
    assert await refunds(e2e, first) == 0
    second = await e2e.make_run(4_000, order_id=first["order_id"], user_id=first["user_id"])
    row = await e2e.until_status(
        second["run_id"], {"ESCALATED", "COMPLETED", "NEEDS_RECONCILIATION"}
    )
    assert (row["status"], row["outcome"]) == ("COMPLETED", "confirmed")
    assert await worker.terminate() == 0


# --- a stale worker ------------------------------------------------------------------------------


async def test_a_stale_worker_writes_nothing_after_another_finishes_the_run(e2e: E2E) -> None:
    """A sends, refund-api commits and hangs; A is stopped (state checked) past its lease; B
    claims, replays the key and completes; A resumes and must change nothing. Assertion-only
    here: the fencing control lives in-process (test_execute.py), with no production seam."""
    made = await approved(e2e, 2_000)
    await e2e.refund_api.plan(made["action_id"], "timeout_after_commit", seconds=HANG_SECONDS)
    stale = await e2e.start_worker()
    async with asyncio.timeout(30):
        while "timeout_after_commit" not in await e2e.refund_api.applied(made["action_id"]):
            await asyncio.sleep(0.05)
    await stale.stop()  # raises unless /proc shows it stopped

    fresh = await e2e.start_worker()
    finished = await e2e.until_status(made["run_id"], {"COMPLETED"})
    events_then = await e2e.events(made["run_id"])

    await stale.resume()
    await asyncio.sleep(
        SETTLE
    )  # A's timeout fires, then its heartbeat or attempt write finds 0 rows
    assert await e2e.run_row(made["run_id"]) == finished, "the stale worker changed nothing"
    assert await e2e.events(made["run_id"]) == events_then
    assert finished["send_attempts"] == 2, "A's send and B's replay, nothing more"
    assert await refunds(e2e, made) == 1
    assert await stale.terminate() == 0
    assert await fresh.terminate() == 0
