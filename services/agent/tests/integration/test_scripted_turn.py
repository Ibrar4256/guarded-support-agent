"""The e2e suite's conversation comes from ``agent_testing.scripted_turn`` (turn slice T6): one
production turn on a scripted model, so the run it leaves is exactly what a real turn leaves."""

import uuid

import pytest
from agent_testing.env import example_turn_env
from agent_testing.scripted_turn import ScriptedTurnRefused, main, scripted_turn
from conftest import env
from sqlalchemy import select
from worker_world import World

from agent.core.capacity import ApprovalSource
from agent.core.lifecycle import RunStatus
from agent.core.policy import FulfillmentStatus
from agent.db.schema import llm_calls


def turn_env() -> dict[str, str]:
    return example_turn_env(db_url=env("AGENT_DB_RUNTIME_URL"))


async def test_a_scripted_turn_proposes_and_policy_approves_with_every_call_recorded(
    world: World,
) -> None:
    order = f"ord_{uuid.uuid4().hex[:12]}"

    made = await scripted_turn(
        turn_env(),
        order_id=order,
        amount_minor=2_000,
        charged_minor=4_000,
        status=FulfillmentStatus.DELIVERED,
        user_id=world.user,
    )

    assert (made["outcome"], made["model_calls"], made["order_id"]) == ("approved", 3, order)
    run = uuid.UUID(str(made["run_id"]))
    row = world.row(run)
    assert row.status == RunStatus.APPROVED and row.approval_source == ApprovalSource.POLICY
    assert row.action_id == made["action_id"] and row.idempotency_key
    with world.owner.connect() as connection:
        purposes = connection.execute(
            select(llm_calls.c.purpose).where(llm_calls.c.run_id == run).order_by(llm_calls.c.id)
        ).scalars()
        assert list(purposes) == ["turn", "turn", "warrant"]  # the warrant check (ADR-029)


async def test_a_scripted_turn_on_a_shipped_order_is_escalated_by_policy(world: World) -> None:
    made = await scripted_turn(
        turn_env(),
        order_id=f"ord_{uuid.uuid4().hex[:12]}",
        amount_minor=2_000,
        charged_minor=4_000,
        status=FulfillmentStatus.SHIPPED,
        user_id=world.user,
    )

    assert made["outcome"] == "escalated"
    assert world.row(uuid.UUID(str(made["run_id"]))).status == RunStatus.ESCALATED


async def test_the_scripted_turn_refuses_production() -> None:
    with pytest.raises(ScriptedTurnRefused, match="APP_ENV=production"):
        await scripted_turn(
            {**turn_env(), "APP_ENV": "production"},
            order_id="ord_never",
            amount_minor=1,
            charged_minor=2,
            status=FulfillmentStatus.DELIVERED,
        )


def test_the_command_needs_a_database_url(capsys: pytest.CaptureFixture[str]) -> None:
    code = main(["--order", "o", "--amount", "1", "--charged", "2"], {})

    assert code == 2 and "AGENT_DB_RUNTIME_URL" in capsys.readouterr().err
