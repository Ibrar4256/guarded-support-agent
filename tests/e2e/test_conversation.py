"""A customer message to an issued refund, end to end (turn slice T6, ADR-015's harness).

The agent's own entry point runs one real turn on a scripted model (look the order up, propose
the refund); policy decides; the crash-test worker, unfaulted, sends to the real refund-api. The
suite only reads, to assert. The control: the same script on an order policy refuses must leave
refund-api untouched even with a worker running.
"""

import asyncio
import uuid
from profile import LEASE_SECONDS

from conftest import E2E

AMOUNT = 2_000
CHARGED = AMOUNT * 2  # ADR-004's test-data rule: every charge at least twice its refund
# Long enough for a running worker to have claimed and sent anything claimable.
SETTLE = LEASE_SECONDS + 3


def new_order() -> str:
    return f"e2e_ord_{uuid.uuid4().hex[:12]}"


async def test_a_customer_message_ends_in_exactly_one_issued_refund(e2e: E2E) -> None:
    order = new_order()
    await e2e.refund_api.create_charge(order, CHARGED)

    made = await e2e.scripted_turn(order, AMOUNT, charged_minor=CHARGED)

    assert (made["outcome"], made["model_calls"]) == ("approved", 2)
    assert (await e2e.run_row(made["run_id"]))["status"] == "APPROVED"
    worker = await e2e.start_worker()
    row = await e2e.until_status(made["run_id"], {"COMPLETED", "ESCALATED"})
    assert await worker.terminate() == 0
    assert (row["status"], row["outcome"]) == ("COMPLETED", "confirmed")
    sent = await e2e.refund_api.refunds(order, made["action_id"])
    assert len(sent) == 1 and sent[0]["amount_minor"] == AMOUNT
    replies = [payload for kind, payload in await e2e.events(made["run_id"]) if kind == "reply"]
    assert replies == [
        {"template": "processing"},
        {
            "template": "refund_issued",
            "order_id": order,
            "amount_minor": AMOUNT,
            "currency": "USD",
        },
    ]


async def test_negative_control_a_refused_proposal_never_reaches_refund_api(e2e: E2E) -> None:
    """Same script, a shipped order: policy escalates, and a running worker sends nothing."""
    order = new_order()
    await e2e.refund_api.create_charge(order, CHARGED)

    made = await e2e.scripted_turn(order, AMOUNT, charged_minor=CHARGED, status="shipped")

    assert made["outcome"] == "escalated"
    # Refused by the proposal itself, before any worker: the pre-send re-check would also refuse
    # it (R09), so checking only the end state couldn't tell which layer did.
    assert (await e2e.run_row(made["run_id"]))["status"] == "ESCALATED"
    worker = await e2e.start_worker()
    await asyncio.sleep(SETTLE)
    assert await worker.terminate() == 0
    assert (await e2e.run_row(made["run_id"]))["status"] == "ESCALATED"
    assert await e2e.refund_api.refunds(order, made["action_id"]) == []
