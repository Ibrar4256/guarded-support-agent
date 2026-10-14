"""lock_charge function (ADR-010 Part 2)

Postgres requires UPDATE privilege for SELECT ... FOR UPDATE, and migration 0001 revoked
UPDATE on charges so they stay immutable to the runtime role. This SECURITY DEFINER
function, owned by the owner role, takes the row lock on the caller's behalf. The lock
belongs to the caller's transaction, so concurrent refunds on one charge still serialize,
and the runtime role still can't change a charge. Postgres never inlines a SECURITY DEFINER
function, so the FOR UPDATE always runs.

Written by hand: autogenerate doesn't see functions or grants. The integration tests cover
them.

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-14 17:02:11.000000
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

LOCK_SIGNATURE = "public.lock_charge(text)"

# Schema-qualified names and a pinned search_path, as for prune_expired_key (0002).
CREATE_LOCK_FUNCTION = f"""
CREATE FUNCTION {LOCK_SIGNATURE}
RETURNS TABLE (order_id text, amount_minor bigint, currency varchar)
LANGUAGE sql
VOLATILE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $$
    SELECT c.order_id, c.amount_minor, c.currency
    FROM public.charges AS c
    WHERE c.order_id = $1
    FOR UPDATE
$$
"""


def upgrade() -> None:
    op.execute(CREATE_LOCK_FUNCTION)
    # Postgres grants EXECUTE on new functions to PUBLIC by default.
    op.execute(f"REVOKE EXECUTE ON FUNCTION {LOCK_SIGNATURE} FROM PUBLIC")
    op.execute(f"GRANT EXECUTE ON FUNCTION {LOCK_SIGNATURE} TO refund_api_app")


def downgrade() -> None:
    op.execute(f"DROP FUNCTION {LOCK_SIGNATURE}")
