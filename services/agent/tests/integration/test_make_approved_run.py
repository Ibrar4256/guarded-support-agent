"""The e2e suite's agent data comes from ``agent_testing.make_approved_run`` (ADR-015 Part 3):
a run made there must be exactly what approval saves, so the worker can take it all the way."""

import uuid

from agent_testing.fakes import created_result
from agent_testing.make_approved_run import make_approved_run
from conftest import env
from sqlalchemy.ext.asyncio import AsyncEngine
from worker_harness import Harness
from worker_world import World

from agent.core.lifecycle import RefundOutcome, RunStatus
from agent.refund_client.models import RefundPayload
from agent.refund_client.payload import canonical_payload
from agent.worker.loop import Ended, process_claim


async def test_a_made_run_carries_its_key_and_canonical_payload_and_completes(
    world: World, async_runtime_engine: AsyncEngine
) -> None:
    made = make_approved_run(
        env("AGENT_DB_RUNTIME_URL"), amount_minor=2_000, charged_minor=4_000, user_id=world.user
    )
    run = uuid.UUID(str(made["run_id"]))
    row = world.row(run)
    assert (row.status, row.idempotency_key) == (RunStatus.APPROVED, made["idempotency_key"])
    assert row.canonical_payload == canonical_payload(
        order_id=str(made["order_id"]),
        amount_minor=2_000,
        currency="USD",
        action_id=str(made["action_id"]),
    )
    payload = RefundPayload.model_validate_json(row.canonical_payload)
    h = Harness(async_runtime_engine, sends=[created_result(payload)])
    claimed = await h.claim()
    assert claimed.run_id == run
    assert await process_claim(h.ctx, claimed) is Ended.COMPLETED, "passes the real policy check"
    assert world.row(run).refund_outcome is RefundOutcome.CONFIRMED


async def test_a_second_run_can_share_an_order(world: World) -> None:
    db_url = env("AGENT_DB_RUNTIME_URL")
    first = make_approved_run(db_url, amount_minor=1_000, charged_minor=8_000, user_id=world.user)
    second = make_approved_run(
        db_url,
        amount_minor=2_000,
        charged_minor=0,
        order_id=str(first["order_id"]),
        user_id=world.user,
    )
    assert second["order_id"] == first["order_id"] and second["run_id"] != first["run_id"]
