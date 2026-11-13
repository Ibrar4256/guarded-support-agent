"""per-key expiry (ADR-012 Part 5)

Each idempotency key carries its own `expires_at`, stamped at insert on the database clock
and never updated (the runtime role has no UPDATE on keys). A later retention change only
affects keys created afterwards, so it can't shorten a key already in flight.

`prune_expired_key(key)` deletes a key only when its stamped `expires_at` has passed **and**
it is older than the owner-set floor (ADR-010 Part 5), so even a bad config value or
fault-plan override can't get a key pruned before the floor. It never touches refunds.

Backfill: existing keys get `created_at + floor`. The floor and the retention are separate
settings; the floor is used only because it's the best value available to a migration.
That's safe: pruning also needs the key to be older than the floor. If keys exist but no
floor is set, the migration refuses rather than guess.

Written by hand: autogenerate can't add a NOT NULL column with a backfill or see functions.

Revision ID: 0004
Revises: 0003
Create Date: 2026-11-12 18:30:00.000000
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

OLD_SIGNATURE = "public.prune_expired_key(text, interval)"
NEW_SIGNATURE = "public.prune_expired_key(text)"

REFUSE_WITHOUT_FLOOR = """
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM public.idempotency_keys)
       AND NOT EXISTS (SELECT 1 FROM public.key_retention_policy WHERE id = 1) THEN
        RAISE EXCEPTION 'idempotency keys exist but no key retention floor is set; set it '
            '(python -m refund_api.retention_floor) and run the migration again';
    END IF;
END
$$
"""

CREATE_NEW_PRUNE = f"""
CREATE FUNCTION {NEW_SIGNATURE} RETURNS boolean
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $$
DECLARE
    floor_retention interval;
    deleted_rows integer;
BEGIN
    SELECT min_retention INTO floor_retention FROM public.key_retention_policy WHERE id = 1;
    IF floor_retention IS NULL THEN
        RAISE EXCEPTION 'key retention floor is not set' USING ERRCODE = '55000';
    END IF;
    DELETE FROM public.idempotency_keys
    WHERE key = $1
      AND expires_at < pg_catalog.now()
      AND created_at < pg_catalog.now() - floor_retention;
    GET DIAGNOSTICS deleted_rows = ROW_COUNT;
    RETURN deleted_rows > 0;
END
$$
"""

# 0002's function, restored on downgrade.
CREATE_OLD_PRUNE = f"""
CREATE FUNCTION {OLD_SIGNATURE} RETURNS boolean
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $$
DECLARE
    floor_retention interval;
    deleted_rows integer;
BEGIN
    IF $2 IS NULL OR $2 <= interval '0' THEN
        RAISE EXCEPTION 'retention must be positive, got %', $2 USING ERRCODE = '22023';
    END IF;
    SELECT min_retention INTO floor_retention FROM public.key_retention_policy WHERE id = 1;
    IF floor_retention IS NULL THEN
        RAISE EXCEPTION 'key retention floor is not set' USING ERRCODE = '55000';
    END IF;
    DELETE FROM public.idempotency_keys
    WHERE key = $1 AND created_at < pg_catalog.now() - GREATEST($2, floor_retention);
    GET DIAGNOSTICS deleted_rows = ROW_COUNT;
    RETURN deleted_rows > 0;
END
$$
"""


def lock_down(signature: str) -> None:
    # Postgres grants EXECUTE on new functions to PUBLIC by default.
    op.execute(f"REVOKE EXECUTE ON FUNCTION {signature} FROM PUBLIC")
    op.execute(f"GRANT EXECUTE ON FUNCTION {signature} TO refund_api_app")


def upgrade() -> None:
    op.execute(REFUSE_WITHOUT_FLOOR)
    op.execute("ALTER TABLE idempotency_keys ADD COLUMN expires_at timestamp with time zone")
    op.execute(
        "UPDATE idempotency_keys SET expires_at = created_at + "
        "(SELECT min_retention FROM key_retention_policy WHERE id = 1)"
    )
    op.execute("ALTER TABLE idempotency_keys ALTER COLUMN expires_at SET NOT NULL")
    op.execute(f"DROP FUNCTION {OLD_SIGNATURE}")
    op.execute(CREATE_NEW_PRUNE)
    lock_down(NEW_SIGNATURE)


def downgrade() -> None:
    op.execute(f"DROP FUNCTION {NEW_SIGNATURE}")
    op.execute(CREATE_OLD_PRUNE)
    lock_down(OLD_SIGNATURE)
    op.execute("ALTER TABLE idempotency_keys DROP COLUMN expires_at")
