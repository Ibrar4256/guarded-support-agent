"""``python -m agent_testing.make_approved_run``: an APPROVED refund run, created the way
approval saves it (ADR-004 step 1: the idempotency key and canonical payload written with the
approval), for the end-to-end crash tests (ADR-015 Part 3). The agent-side counterpart of
refund-api's ``/_test/charges``: the e2e suite never writes agent rows itself.

    python -m agent_testing.make_approved_run --amount 2000 [--charged 8000] [--order ord_x]

Connects as the runtime role (``AGENT_DB_RUNTIME_URL``), creates a customer, a delivered order
(unless ``--order`` names an existing one), a conversation, the run and its order lookup, and
prints them as one JSON object.
"""

import argparse
import json
import os
import secrets
import sys
import uuid
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta

from sqlalchemy import create_engine, insert, select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from agent.core.capacity import ApprovalSource
from agent.core.lifecycle import RefundOutcome, RunStatus
from agent.core.policy import FulfillmentStatus
from agent.db.schema import conversations, customers, orders, run_order_lookups, runs
from agent.refund_client.payload import canonical_payload

CURRENCY = "USD"
DELIVERED_DAYS_AGO = 3


def make_approved_run(
    db_url: str,
    *,
    amount_minor: int,
    charged_minor: int,
    order_id: str | None = None,
    user_id: str | None = None,
) -> dict[str, str | int]:
    user = user_id or f"e2e_user_{uuid.uuid4().hex[:10]}"
    order = order_id or f"e2e_ord_{uuid.uuid4().hex[:12]}"
    action = f"e2e_act_{uuid.uuid4().hex[:12]}"
    key = secrets.token_urlsafe(32)
    run, conversation = uuid.uuid4(), uuid.uuid4()
    engine = create_engine(db_url)
    try:
        with engine.begin() as connection:
            connection.execute(
                pg_insert(customers).values(id=user).on_conflict_do_nothing(index_elements=["id"])
            )
            if order_id is None:
                connection.execute(
                    insert(orders).values(
                        id=order,
                        owner_user_id=user,
                        charged_minor=charged_minor,
                        currency=CURRENCY,
                        status=FulfillmentStatus.DELIVERED,
                        delivered_at=datetime.now(UTC) - timedelta(days=DELIVERED_DAYS_AGO),
                    )
                )
            else:
                owner = connection.execute(
                    select(orders.c.owner_user_id).where(orders.c.id == order)
                ).scalar_one()
                if owner != user:
                    raise SystemExit(f"--order {order} belongs to {owner}; pass --user {owner}")
            connection.execute(insert(conversations).values(id=conversation, user_id=user))
            connection.execute(
                insert(runs).values(
                    id=run,
                    conversation_id=conversation,
                    user_id=user,
                    status=RunStatus.APPROVED,
                    refund_outcome=RefundOutcome.NONE,
                    action_id=action,
                    order_id=order,
                    amount_minor=amount_minor,
                    currency=CURRENCY,
                    approval_source=ApprovalSource.POLICY,
                    approved_at=datetime.now(UTC),
                    idempotency_key=key,
                    canonical_payload=canonical_payload(
                        order_id=order,
                        amount_minor=amount_minor,
                        currency=CURRENCY,
                        action_id=action,
                    ),
                )
            )
            connection.execute(insert(run_order_lookups).values(run_id=run, order_id=order))
    finally:
        engine.dispose()
    return {
        "run_id": str(run),
        "action_id": action,
        "order_id": order,
        "user_id": user,
        "amount_minor": amount_minor,
        "currency": CURRENCY,
        "idempotency_key": key,
    }


def main(argv: Sequence[str], environ: Mapping[str, str]) -> int:
    parser = argparse.ArgumentParser(prog="python -m agent_testing.make_approved_run")
    parser.add_argument("--amount", type=int, required=True, help="refund, minor units")
    parser.add_argument("--charged", type=int, default=None, help="new order's charge")
    parser.add_argument("--order", default=None, help="reuse an existing order")
    parser.add_argument("--user", default=None, help="the order's customer, with --order")
    args = parser.parse_args(argv)
    db_url = environ.get("AGENT_DB_RUNTIME_URL", "")
    if not db_url:
        print("AGENT_DB_RUNTIME_URL is not set", file=sys.stderr)
        return 2
    created = make_approved_run(
        db_url,
        amount_minor=args.amount,
        charged_minor=args.charged if args.charged is not None else args.amount * 2,
        order_id=args.order,
        user_id=args.user,
    )
    print(json.dumps(created))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:], os.environ))
