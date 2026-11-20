"""A case's setup written into its own database (ADR-026 Part 1, S2a).

Every timestamp is set explicitly, relative to ``ANCHOR``; nothing relies on a column default,
which would be the real time and fail the case's timestamp invariant. Ids are fixed per case
(``uuid5`` of the case id), so the digest a prompt shows is the same on every run and day.
"""

import uuid
from typing import Any

from sqlalchemy import Engine, insert

from agent.core.capacity import ApprovalSource
from agent.core.lifecycle import RefundOutcome, RunStatus
from agent.db.schema import conversations, customers, orders, runs
from agent_testing.evals.cases import SEED_HORIZON, Case, PastTicket
from agent_testing.evals.clock import ANCHOR

NAMESPACE = uuid.UUID("5f0c2a8e-2d1b-4c55-9a43-6f1e0d7b9c21")
# Customers and orders exist from the start of the horizon; the case conversation at the anchor.
SEEDED_AT = ANCHOR - SEED_HORIZON
WINDOW_MS = 60_000  # a sent past ticket's window snapshot (ADR-014 S1)


def case_uuid(case: Case, name: str) -> uuid.UUID:
    return uuid.uuid5(NAMESPACE, f"{case.suite}/{case.id}/{name}")


def conversation_id(case: Case) -> uuid.UUID:
    return case_uuid(case, "conversation")


def seed(engine: Engine, case: Case) -> uuid.UUID:
    """Write the case's customers, orders, past tickets and conversation; returns the
    conversation the messages go to. ``engine`` connects as the owner role."""
    owners = sorted({case.customer, *(order.owner for order in case.orders)})
    with engine.begin() as conn:
        for customer in owners:
            conn.execute(insert(customers).values(id=customer, created_at=SEEDED_AT))
        for order in case.orders:
            conn.execute(
                insert(orders).values(
                    id=order.id,
                    owner_user_id=order.owner,
                    charged_minor=order.charged_minor,
                    currency=order.currency,
                    status=order.status,
                    delivered_at=order.delivered_at,
                    chargeback_open=order.chargeback_open,
                    created_at=SEEDED_AT,
                )
            )
        currencies = {order.id: order.currency for order in case.orders}
        for index, ticket in enumerate(case.past_tickets):
            past = case_uuid(case, f"past/{index}")
            conn.execute(
                insert(conversations).values(id=past, user_id=case.customer, created_at=ticket.at)
            )
            conn.execute(insert(runs).values(**ticket_row(case, index, ticket, past, currencies)))
        conversation = conversation_id(case)
        conn.execute(
            insert(conversations).values(id=conversation, user_id=case.customer, created_at=ANCHOR)
        )
    return conversation


def ticket_row(
    case: Case, index: int, ticket: PastTicket, conversation: uuid.UUID, currencies: dict[str, str]
) -> dict[str, Any]:
    """A finished run that passes every ADR-008/009/014 CHECK for its status and outcome."""
    row: dict[str, Any] = {
        "id": case_uuid(case, f"past/{index}/run"),
        "conversation_id": conversation,
        "user_id": case.customer,
        "status": RunStatus.COMPLETED if ticket.status == "completed" else RunStatus.ESCALATED,
        "refund_outcome": RefundOutcome.NONE,
        "created_at": ticket.at,
    }
    if ticket.status == "escalated":
        row.update(escalated_at=ticket.at, escalation_reason=ticket.reason)
    if ticket.refund is not None:
        refund = ticket.refund
        row.update(
            action_id=f"act_{case_uuid(case, f'past/{index}/action').hex}",
            order_id=refund.order,
            amount_minor=refund.amount_minor,
            currency=currencies[refund.order],
            refund_outcome=refund.outcome,
            approval_source=ApprovalSource.POLICY,
            approved_at=ticket.at,
            first_send_started_at=ticket.at,
            send_window_ms=WINDOW_MS,
        )
    return row
