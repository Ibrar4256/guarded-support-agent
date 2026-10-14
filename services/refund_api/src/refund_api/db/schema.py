"""refund-api database schema (ADR-004, ADR-005) as SQLAlchemy Core tables.

Three tables:
- ``charges``: what a refund may be checked against (``refund_exceeds_charge``). Only the
  fault-gated test endpoint creates them; production has no way to.
- ``refunds``: append-only. One row per committed refund. ``action_id`` is deliberately not
  unique: under ``dedup_disabled`` or after key retention, a second row is the real hazard
  the tests must be able to see.
- ``idempotency_keys``: the key, the payload hash it is bound to, and the stored response,
  written in the same transaction as the refund. Pruned on access after retention, only
  through the ``prune_expired_key`` function (migration 0002); the refund row survives so
  reconciliation by ``action_id`` still finds it.
- ``key_retention_policy``: the owner-set floor below which no key is ever pruned.
"""

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Column,
    DateTime,
    ForeignKey,
    Index,
    MetaData,
    SmallInteger,
    String,
    Table,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import INTERVAL, UUID

NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}
metadata = MetaData(naming_convention=NAMING_CONVENTION)

CURRENCY_FORMAT = "currency ~ '^[A-Z]{3}$'"


def created_at() -> Column[datetime]:
    return Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now())


charges = Table(
    "charges",
    metadata,
    # One charge per order in v1; the order ID is the charge's identity.
    Column("order_id", Text, primary_key=True),
    Column("amount_minor", BigInteger, nullable=False),
    Column("currency", String(3), nullable=False),
    created_at(),
    CheckConstraint("amount_minor > 0", name="amount_positive"),
    CheckConstraint(CURRENCY_FORMAT, name="currency_format"),
)

refunds = Table(
    "refunds",
    metadata,
    Column("id", UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()),
    Column("order_id", Text, ForeignKey("charges.order_id"), nullable=False),
    Column("action_id", Text, nullable=False),
    Column("amount_minor", BigInteger, nullable=False),
    Column("currency", String(3), nullable=False),
    created_at(),
    CheckConstraint("amount_minor > 0", name="amount_positive"),
    CheckConstraint(CURRENCY_FORMAT, name="currency_format"),
    # The reconcile endpoint: GET /refunds?order_id=&action_id= (ADR-004 step 5).
    Index(None, "order_id", "action_id"),
)

idempotency_keys = Table(
    "idempotency_keys",
    metadata,
    Column("key", Text, primary_key=True),
    Column("payload_hash", String(64), nullable=False),
    Column("refund_id", UUID(as_uuid=True), ForeignKey("refunds.id"), nullable=False),
    Column("response_status", SmallInteger, nullable=False),
    # Text, not JSONB: JSONB reorders keys and drops whitespace, and a replay must be
    # byte-identical to the original response.
    Column("response_body", Text, nullable=False),
    # First time the provider saw the key: the retention anchor.
    created_at(),
    CheckConstraint("payload_hash ~ '^[0-9a-f]{64}$'", name="payload_hash_format"),
    # Only commits store a key (ADR-004: business rejections are never replayed).
    CheckConstraint("response_status BETWEEN 200 AND 299", name="response_is_success"),
)

# The retention floor for prune_expired_key (ADR-010 Part 5). One row, set by the owner
# role at deploy time; the app group has no privileges on it, so application code can't
# lower it. A custom setting was rejected: any session can SET one.
key_retention_policy = Table(
    "key_retention_policy",
    metadata,
    Column("id", SmallInteger, primary_key=True),
    Column("min_retention", INTERVAL, nullable=False),
    CheckConstraint("id = 1", name="single_row"),
    CheckConstraint("min_retention > interval '0'", name="min_retention_positive"),
)
