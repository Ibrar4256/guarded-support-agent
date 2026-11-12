"""reconciler bounds: the longest a run's sends could take to commit (ADR-016)

Written by hand (ADR-016 Part 2, S4). Refuses to run when a run has already been sent: its
bounds can't be known now, and guessing one could start reconciliation before a send could
have committed (ADR-016 Part 2).

Revision ID: 0003
Revises: 0002
Create Date: 2026-11-12
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

REFUSE_SENT_RUNS = """
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM runs WHERE send_attempts > 0) THEN
        RAISE EXCEPTION 'runs have already been sent, so their in-flight bounds are unknown; '
            'resolve them before running this migration';
    END IF;
END
$$
"""

CHECKS = {
    "in_flight_bound_iff_sent": "(in_flight_bound_ms IS NULL) = (send_attempts = 0)",
    "max_timeout_iff_sent": "(max_client_timeout_ms IS NULL) = (send_attempts = 0)",
    "in_flight_bound_covers_timeout": (
        "in_flight_bound_ms IS NULL OR in_flight_bound_ms > max_client_timeout_ms"
    ),
    "max_timeout_positive": "max_client_timeout_ms IS NULL OR max_client_timeout_ms > 0",
}


def upgrade() -> None:
    op.execute(REFUSE_SENT_RUNS)
    op.add_column("runs", sa.Column("in_flight_bound_ms", sa.BigInteger(), nullable=True))
    op.add_column("runs", sa.Column("max_client_timeout_ms", sa.BigInteger(), nullable=True))
    for name, condition in CHECKS.items():
        op.create_check_constraint(op.f(f"ck_runs_{name}"), "runs", condition)


def downgrade() -> None:
    for name in CHECKS:
        op.drop_constraint(op.f(f"ck_runs_{name}"), "runs", type_="check")
    for column in ("max_client_timeout_ms", "in_flight_bound_ms"):
        op.drop_column("runs", column)
