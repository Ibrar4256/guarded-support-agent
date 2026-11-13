"""retention_floor_is_set function (ADR-021 Part 2)

refund-api checks at startup that the key-retention floor is set, so a deploy that skipped
``python -m refund_api.retention_floor`` stops at the start instead of at the first refund.
The runtime role has no privileges on key_retention_policy (0002), so this SECURITY DEFINER
function, owned by the owner role, answers only "is it set": never the value, never a write.

Written by hand: autogenerate doesn't see functions or grants. The integration tests cover
them.

Revision ID: 0005
Revises: 0004
Create Date: 2026-11-14 12:00:00.000000
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

FLOOR_CHECK_SIGNATURE = "public.retention_floor_is_set()"

# Schema-qualified names and a pinned search_path, as for prune_expired_key (0002).
CREATE_FLOOR_CHECK = f"""
CREATE FUNCTION {FLOOR_CHECK_SIGNATURE}
RETURNS boolean
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $$
    SELECT EXISTS (SELECT 1 FROM public.key_retention_policy WHERE id = 1)
$$
"""


def upgrade() -> None:
    op.execute(CREATE_FLOOR_CHECK)
    # Postgres grants EXECUTE on new functions to PUBLIC by default.
    op.execute(f"REVOKE EXECUTE ON FUNCTION {FLOOR_CHECK_SIGNATURE} FROM PUBLIC")
    op.execute(f"GRANT EXECUTE ON FUNCTION {FLOOR_CHECK_SIGNATURE} TO refund_api_app")


def downgrade() -> None:
    op.execute(f"DROP FUNCTION {FLOOR_CHECK_SIGNATURE}")
