"""Agent database schema (ADR-005, ADR-008, ADR-009) as SQLAlchemy Core tables.

Postgres enums are built from the ``agent.core`` enums, so the database and the domain
logic share one list of values. The CHECK constraints mirror
``agent.core.lifecycle.invariant_violations``; an integration test inserts every
combination and asserts the two agree.
"""

from datetime import datetime
from enum import Enum

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    PrimaryKeyConstraint,
    SmallInteger,
    String,
    Table,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy import Enum as SAEnum
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID

from agent.core.capacity import ApprovalSource
from agent.core.lifecycle import RefundOutcome, RunStatus
from agent.core.policy import FulfillmentStatus

NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}
metadata = MetaData(naming_convention=NAMING_CONVENTION)

PRE_SEND = "('AGENT_STEP', 'AWAIT_APPROVAL', 'APPROVED')"
UNCERTAIN = "('EXECUTING', 'NEEDS_RECONCILIATION')"
TERMINAL = "('COMPLETED', 'ESCALATED')"


def pg_enum(enum_cls: type[Enum], name: str) -> SAEnum:
    """A native Postgres enum whose labels are the Python enum's values."""
    return SAEnum(
        enum_cls,
        name=name,
        values_callable=lambda cls: [member.value for member in cls],
        validate_strings=True,
    )


run_status = pg_enum(RunStatus, "run_status")
refund_outcome = pg_enum(RefundOutcome, "refund_outcome")
approval_source = pg_enum(ApprovalSource, "approval_source")
fulfillment_status = pg_enum(FulfillmentStatus, "fulfillment_status")


def timestamp(name: str, *, nullable: bool = True) -> Column[datetime]:
    return Column(name, DateTime(timezone=True), nullable=nullable)


def created_at() -> Column[datetime]:
    return Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now())


customers = Table(
    "customers",
    metadata,
    # Lock target only (ADR-005): no code path updates this row after insert.
    Column("id", Text, primary_key=True),
    created_at(),
)

orders = Table(
    "orders",
    metadata,
    Column("id", Text, primary_key=True),
    Column("owner_user_id", Text, ForeignKey("customers.id"), nullable=False),
    Column("charged_minor", BigInteger, nullable=False),
    Column("currency", String(3), nullable=False),
    # Fulfillment status only; refund state is derived from refund history (ADR-009).
    Column("status", fulfillment_status, nullable=False),
    timestamp("delivered_at"),
    Column("chargeback_open", Boolean, nullable=False, server_default=text("false")),
    created_at(),
    CheckConstraint("charged_minor > 0", name="charged_positive"),
    CheckConstraint("status <> 'delivered' OR delivered_at IS NOT NULL", name="delivered_has_time"),
)

conversations = Table(
    "conversations",
    metadata,
    Column("id", UUID(as_uuid=True), primary_key=True),
    Column("user_id", Text, ForeignKey("customers.id"), nullable=False),
    # Turn lease (ADR-008): one turn at a time per conversation.
    Column("active_turn_id", UUID(as_uuid=True)),
    timestamp("turn_expires_at"),
    created_at(),
)

runs = Table(
    "runs",
    metadata,
    Column("id", UUID(as_uuid=True), primary_key=True),
    Column("conversation_id", UUID(as_uuid=True), ForeignKey("conversations.id"), nullable=False),
    Column("user_id", Text, ForeignKey("customers.id"), nullable=False),
    Column("status", run_status, nullable=False),
    Column("refund_outcome", refund_outcome, nullable=False, server_default="none"),
    # Claiming: lease plus fencing (ADR-004 step 7).
    Column("claim_version", BigInteger, nullable=False, server_default="0"),
    Column("worker_id", Text),
    timestamp("lease_expires_at"),
    # The proposal (ADR-009: action_id assigned at proposal time).
    Column("action_id", Text),
    Column("order_id", Text, ForeignKey("orders.id")),
    Column("amount_minor", BigInteger),
    Column("currency", String(3)),
    Column("review_hash", Text),
    Column("review_revision", Integer, nullable=False, server_default="0"),
    timestamp("review_deadline"),
    # Approval (ADR-003, ADR-009).
    Column("approval_source", approval_source),
    Column("approved_by", Text),
    timestamp("approved_at"),
    # Execution (ADR-004).
    Column("idempotency_key", Text),
    Column("canonical_payload", Text),
    timestamp("first_send_started_at"),
    timestamp("last_attempt_at"),
    Column("presend_attempts", Integer, nullable=False, server_default="0"),
    # Attention (ADR-008, ADR-009).
    timestamp("escalated_at"),
    timestamp("c01_flagged_at"),
    timestamp("escalation_resolved_at"),
    Column("escalation_resolved_by", Text),
    created_at(),
    UniqueConstraint("action_id"),
    UniqueConstraint("idempotency_key"),
    CheckConstraint("amount_minor IS NULL OR amount_minor > 0", name="amount_positive"),
    CheckConstraint("presend_attempts >= 0", name="presend_attempts_non_negative"),
    # --- ADR-008 invariants (mirrors agent.core.lifecycle.invariant_violations) ---
    CheckConstraint(
        "(refund_outcome = 'none') = (first_send_started_at IS NULL)", name="outcome_first_send"
    ),
    CheckConstraint(f"status NOT IN {PRE_SEND} OR refund_outcome = 'none'", name="pre_send_none"),
    CheckConstraint(
        f"status NOT IN {UNCERTAIN} OR refund_outcome = 'unknown'", name="uncertain_unknown"
    ),
    CheckConstraint(
        f"status NOT IN {TERMINAL} OR refund_outcome <> 'unknown'", name="terminal_not_unknown"
    ),
    CheckConstraint(f"status NOT IN {TERMINAL} OR worker_id IS NULL", name="terminal_no_claim"),
    CheckConstraint(
        "status <> 'AWAIT_APPROVAL' OR review_deadline IS NOT NULL", name="review_has_deadline"
    ),
    CheckConstraint("status <> 'ESCALATED' OR escalated_at IS NOT NULL", name="escalated_flagged"),
    # --- ADR-009 approval invariants ---
    CheckConstraint(
        "approval_source IS DISTINCT FROM 'reviewer' OR approved_by IS NOT NULL",
        name="reviewer_has_approver",
    ),
    CheckConstraint(
        "approval_source IS DISTINCT FROM 'policy' OR approved_by IS NULL",
        name="policy_has_no_approver",
    ),
    CheckConstraint("approved_by IS NULL OR approved_by <> user_id", name="no_self_approval"),
    CheckConstraint(
        f"status NOT IN ('APPROVED', {UNCERTAIN[1:-1]}) OR approved_at IS NOT NULL",
        name="approved_has_time",
    ),
    CheckConstraint(
        "refund_outcome = 'none' OR (approved_at IS NOT NULL AND approval_source IS NOT NULL)",
        name="sent_was_approved",
    ),
)

# ADR-009: at most one unfinished run per conversation.
Index(
    "uq_runs_one_unfinished_per_conversation",
    runs.c.conversation_id,
    unique=True,
    postgresql_where=text(f"status NOT IN {TERMINAL}"),
)
# ADR-005: capacity query inside the customer lock stays an index scan.
Index(
    "ix_runs_capacity",
    runs.c.user_id,
    runs.c.first_send_started_at,
    postgresql_where=text("refund_outcome IN ('unknown', 'confirmed')"),
)
# ADR-009: R11 (per customer) and R13 (system-wide) policy-approved totals.
Index(
    "ix_runs_policy_approved_by_user",
    runs.c.user_id,
    runs.c.approved_at,
    postgresql_where=text("approval_source = 'policy'"),
)
Index(
    "ix_runs_policy_approved_global",
    runs.c.approved_at,
    postgresql_where=text("approval_source = 'policy'"),
)
# R12 and pending exposure look up actions by order.
Index("ix_runs_order_id", runs.c.order_id)

run_order_lookups = Table(
    "run_order_lookups",
    metadata,
    Column("run_id", UUID(as_uuid=True), ForeignKey("runs.id"), nullable=False),
    Column("order_id", Text, ForeignKey("orders.id"), nullable=False),
    Column("looked_up_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    PrimaryKeyConstraint("run_id", "order_id"),
)

events = Table(
    "events",
    metadata,
    Column("id", BigInteger, primary_key=True, autoincrement=True),
    Column("conversation_id", UUID(as_uuid=True), ForeignKey("conversations.id"), nullable=False),
    Column("run_id", UUID(as_uuid=True), ForeignKey("runs.id")),
    Column("kind", Text, nullable=False),
    Column("payload", JSONB, nullable=False, server_default=text("'{}'::jsonb")),
    created_at(),
)
Index("ix_events_conversation_id_id", events.c.conversation_id, events.c.id)

DECISION_STAGES = (
    "proposal",
    "presend",
    "reviewer_approve",
    "reviewer_decline",
    "review_expiry",
    "escalation_resolution",
    "conversation_escalation",
    "breaker_trip",
    "breaker_reset",
    "kill_switch",
)

decisions = Table(
    "decisions",
    metadata,
    # Append-only: the runtime role gets INSERT and SELECT only (ADR-009).
    Column("id", BigInteger, primary_key=True, autoincrement=True),
    Column("run_id", UUID(as_uuid=True), ForeignKey("runs.id")),
    Column("action_id", Text),
    Column("stage", Text, nullable=False),
    Column("outcome", Text, nullable=False),
    Column("rule_ids", ARRAY(Text), nullable=False, server_default=text("'{}'::text[]")),
    Column("policy_version", Text, nullable=False),
    Column("config_snapshot", JSONB, nullable=False),
    Column("input_snapshot", JSONB, nullable=False),
    Column("review_hash", Text),
    Column("trace_id", Text),
    Column("decided_by", Text, nullable=False),
    Column("decided_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    CheckConstraint(
        "stage IN (" + ", ".join(f"'{s}'" for s in DECISION_STAGES) + ")", name="known_stage"
    ),
)
Index("ix_decisions_run_id", decisions.c.run_id)
Index("ix_decisions_decided_at", decisions.c.decided_at)

policy_controls = Table(
    "policy_controls",
    metadata,
    Column("id", SmallInteger, primary_key=True),
    timestamp("breaker_tripped_at"),
    Column("breaker_reset_by", Text),
    timestamp("breaker_reset_at"),
    Column("sends_enabled", Boolean, nullable=False, server_default=text("true")),
    CheckConstraint("id = 1", name="single_row"),
)
