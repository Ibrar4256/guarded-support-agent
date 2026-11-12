"""worker columns: attempt count, window snapshot, cooldown, release streak (ADR-014)

Written by hand (ADR-012 Part 3, ADR-013, ADR-014 S1, S12, S13). Refuses to run when a run
has already been sent: its window snapshot can't be known now, and guessing one could
lengthen the time its key is trusted (ADR-012 Part 3).

Revision ID: 0002
Revises: 0001
Create Date: 2026-11-12
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

REFUSE_SENT_RUNS = """
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM runs WHERE first_send_started_at IS NOT NULL) THEN
        RAISE EXCEPTION 'runs have already been sent, so their window snapshot is unknown; '
            'resolve them before running this migration';
    END IF;
END
$$
"""

CHECKS = {
    "send_attempts_non_negative": "send_attempts >= 0",
    "window_iff_first_send": "(send_window_ms IS NULL) = (first_send_started_at IS NULL)",
    "window_positive": "send_window_ms IS NULL OR send_window_ms > 0",
    "no_attempts_before_send": "first_send_started_at IS NOT NULL OR send_attempts = 0",
    "release_streak_non_negative": "release_streak >= 0",
}
CLAIMABLE = sa.text("status IN ('APPROVED', 'EXECUTING')")


def upgrade() -> None:
    op.execute(REFUSE_SENT_RUNS)
    op.add_column(
        "runs", sa.Column("send_attempts", sa.Integer(), server_default="0", nullable=False)
    )
    op.add_column("runs", sa.Column("send_window_ms", sa.BigInteger(), nullable=True))
    op.add_column("runs", sa.Column("claimable_after", sa.DateTime(timezone=True), nullable=True))
    op.add_column(
        "runs", sa.Column("release_streak", sa.Integer(), server_default="0", nullable=False)
    )
    for name, condition in CHECKS.items():
        op.create_check_constraint(op.f(f"ck_runs_{name}"), "runs", condition)
    op.create_index(
        "ix_runs_claimable", "runs", ["approved_at"], unique=False, postgresql_where=CLAIMABLE
    )


def downgrade() -> None:
    op.drop_index("ix_runs_claimable", table_name="runs", postgresql_where=CLAIMABLE)
    for name in CHECKS:
        op.drop_constraint(op.f(f"ck_runs_{name}"), "runs", type_="check")
    for column in ("release_streak", "claimable_after", "send_window_ms", "send_attempts"):
        op.drop_column("runs", column)
