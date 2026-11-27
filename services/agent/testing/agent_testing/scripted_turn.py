"""``python -m agent_testing.scripted_turn``: one real customer turn on a scripted model, for the
end-to-end path from a message to an issued refund (turn slice T6; ADR-015 Part 3's pattern).

The turn-side counterpart of ``make_approved_run``: instead of writing an APPROVED run directly,
it creates a customer, an order and a conversation, then runs the production ``run_turn``
(start, loop, ADR-009's proposal path, call records) with a model scripted to look the order up
and propose the refund. Policy, not the script, decides the outcome: a shipped order escalates.

    python -m agent_testing.scripted_turn --order ord_x --amount 2000 --charged 4000 \
        [--status shipped]

Connects as the runtime role (``AGENT_DB_RUNTIME_URL``), reads the turn and policy settings from
the environment like production, refuses ``APP_ENV=production``, and prints one JSON object.
"""

import argparse
import asyncio
import json
import os
import sys
import uuid
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta

from sqlalchemy import insert, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import create_async_engine

from agent.alerts import LogAlerter
from agent.core.policy import FulfillmentStatus
from agent.db.llm_calls import DbCallRecorder
from agent.db.schema import conversations, customers, orders, runs
from agent.llm.pool import PoolMember, ProviderPool
from agent.llm.prices import PriceTable
from agent.llm.types import Purpose
from agent.policy_settings import policy_config_from_env
from agent.turn.run import TurnDeps, run_turn
from agent.turn.settings import TurnSettings
from agent_testing.scripted_llm import ScriptedLLM, Turn, tool_call, warrant_turn

CURRENCY = "USD"
DELIVERED_DAYS_AGO = 3
MESSAGE = "My order arrived damaged, I'd like a refund please."


class ScriptedTurnRefused(RuntimeError):
    pass


def script(order_id: str, amount_minor: int) -> ScriptedLLM:
    """Look the order up, then propose. Strict: a model call after the proposal would fail."""
    return ScriptedLLM(
        [
            Turn(tool_calls=(tool_call("lookup_order", {"order_id": order_id}, "c_lookup"),)),
            Turn(
                tool_calls=(
                    tool_call(
                        "propose_refund",
                        {"order_id": order_id, "amount_minor": amount_minor, "currency": CURRENCY},
                        "c_propose",
                    ),
                ),
                match=lambda view: view.has_tool_results,
            ),
            warrant_turn(order_id, "arrived damaged"),
        ]
    )


async def scripted_turn(
    environ: Mapping[str, str],
    *,
    order_id: str,
    amount_minor: int,
    charged_minor: int,
    status: FulfillmentStatus,
    user_id: str | None = None,
) -> dict[str, str | int | None]:
    if environ.get("APP_ENV") == "production":
        raise ScriptedTurnRefused("the scripted turn never runs with APP_ENV=production")
    settings = TurnSettings.from_env(environ)
    policy = policy_config_from_env(environ)
    engine = create_async_engine(environ["AGENT_DB_RUNTIME_URL"])
    user, conversation = user_id or f"e2e_user_{uuid.uuid4().hex[:10]}", uuid.uuid4()
    try:
        async with engine.begin() as conn:
            await conn.execute(
                pg_insert(customers).values(id=user).on_conflict_do_nothing(index_elements=["id"])
            )
            delivered_at = datetime.now(UTC) - timedelta(days=DELIVERED_DAYS_AGO)
            await conn.execute(
                insert(orders).values(
                    id=order_id,
                    owner_user_id=user,
                    charged_minor=charged_minor,
                    currency=CURRENCY,
                    status=status,
                    delivered_at=delivered_at if status is FulfillmentStatus.DELIVERED else None,
                )
            )
            await conn.execute(insert(conversations).values(id=conversation, user_id=user))
        model = script(order_id, amount_minor)
        alerter = LogAlerter(timedelta(seconds=60))
        deps = TurnDeps(
            engine=engine,
            settings=settings,
            pool=ProviderPool(
                {purpose: [PoolMember("scripted", "scripted-model", model)] for purpose in Purpose},
                cooldown=timedelta(seconds=900),
                alerter=alerter,
            ),
            record=DbCallRecorder(
                engine,
                statement_timeout=settings.statement_timeout,
                lock_timeout=settings.lock_timeout,
            ),
            prices=PriceTable.parse({"version": "e2e", "currency": "USD", "models": {}}),
            alerter=alerter,
            policy=policy,
        )
        result = await run_turn(deps, conversation_id=conversation, user=user, message=MESSAGE)
        await deps.background.drain(settings.summary_deadline.total_seconds())
        async with engine.connect() as conn:
            action_id = (
                await conn.execute(select(runs.c.action_id).where(runs.c.id == result.ids.run_id))
            ).scalar_one()
    finally:
        await engine.dispose()
    return {
        "outcome": result.outcome.value,
        "run_id": str(result.ids.run_id),
        "conversation_id": str(conversation),
        "user_id": user,
        "order_id": order_id,
        "action_id": action_id,
        "amount_minor": amount_minor,
        "currency": CURRENCY,
        "model_calls": len(model.calls),
    }


def main(argv: Sequence[str], environ: Mapping[str, str]) -> int:
    parser = argparse.ArgumentParser(prog="python -m agent_testing.scripted_turn")
    parser.add_argument("--order", required=True, help="the new order's id")
    parser.add_argument("--amount", type=int, required=True, help="refund, minor units")
    parser.add_argument("--charged", type=int, required=True, help="the order's charge")
    parser.add_argument(
        "--status",
        choices=[s.value for s in FulfillmentStatus],
        default=FulfillmentStatus.DELIVERED.value,
    )
    args = parser.parse_args(argv)
    if not environ.get("AGENT_DB_RUNTIME_URL"):
        print("AGENT_DB_RUNTIME_URL is not set", file=sys.stderr)
        return 2
    try:
        made = asyncio.run(
            scripted_turn(
                environ,
                order_id=args.order,
                amount_minor=args.amount,
                charged_minor=args.charged,
                status=FulfillmentStatus(args.status),
            )
        )
    except ScriptedTurnRefused as refused:
        print(str(refused), file=sys.stderr)
        return 2
    print(json.dumps(made))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:], os.environ))
