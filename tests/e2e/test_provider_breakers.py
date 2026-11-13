"""Provider breakers end to end (ADR-018): refund-api answers 500 to every refund and lookup for
the test's own actions (``fail_all``), as with its database down, while ``/config`` keeps
answering. The worker's write breaker must pause sending instead of spending each run's attempts
and sending it to reconciliation, then finish every refund once refund-api recovers.
"""

import asyncio
from typing import Any

from conftest import E2E

UNSETTLED = {"NEEDS_RECONCILIATION", "ESCALATED", "COMPLETED"}
# refund-api is "down" for this long: long enough for both runs to be tried and paused.
OUTAGE_SECONDS = 30.0
# Recovery waits out the runs' claim cooldown (30 s in the profile), then one probe each.
RECOVERY_SECONDS = 60.0


async def approved(e2e: E2E, amount: int) -> dict[str, Any]:
    made = await e2e.make_run(amount, charged_minor=amount * 2)
    await e2e.refund_api.create_charge(made["order_id"], amount * 2)
    return made


async def test_a_refund_api_outage_pauses_sending_and_every_refund_completes_after_it(
    e2e: E2E,
) -> None:
    runs = [await approved(e2e, 2_000), await approved(e2e, 1_500)]
    for made in runs:
        await e2e.refund_api.plan(made["action_id"], "fail_all")
    worker = await e2e.start_worker()

    async with asyncio.timeout(OUTAGE_SECONDS):
        while not all([await e2e.refund_api.applied(m["action_id"]) for m in runs]):
            await asyncio.sleep(0.2)
    for made in runs:
        row = await e2e.run_row(made["run_id"])
        assert (row["status"], row["worker_id"]) == ("EXECUTING", None), "paused, not reconciled"
        assert row["send_attempts"] < 3, "released with an attempt left"
        await e2e.refund_api.remove(made["action_id"])

    for made in runs:
        row = await e2e.until_status(made["run_id"], UNSETTLED, seconds=RECOVERY_SECONDS)
        assert (row["status"], row["outcome"]) == ("COMPLETED", "confirmed"), "none escalated"
        assert len(await e2e.refund_api.refunds(made["order_id"], made["action_id"])) == 1
    assert await worker.terminate() == 0
