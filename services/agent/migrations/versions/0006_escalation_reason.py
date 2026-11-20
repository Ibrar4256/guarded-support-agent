"""escalation reason: why a run went to a person, for operators and eval judges (ADR-026 S4)

Written by hand. Runs already ESCALATED get 'unrecorded': the reason wasn't stored then, and
inventing one would be worse than saying so. The format is copied here, not imported, so this
migration keeps meaning what it meant if agent.core.escalations changes later.

Revision ID: 0006
Revises: 0005
Create Date: 2026-11-19
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

CHECKS = {
    "reason_iff_escalated": "(status = 'ESCALATED') = (escalation_reason IS NOT NULL)",
    "escalation_reason_format": (
        "escalation_reason IS NULL OR escalation_reason ~ '^[a-z][a-z0-9_]*(:[A-Za-z0-9_,]+)?$'"
    ),
}


def upgrade() -> None:
    op.add_column("runs", sa.Column("escalation_reason", sa.Text(), nullable=True))
    op.execute("UPDATE runs SET escalation_reason = 'unrecorded' WHERE status = 'ESCALATED'")
    for name, condition in CHECKS.items():
        op.create_check_constraint(op.f(f"ck_runs_{name}"), "runs", condition)


def downgrade() -> None:
    for name in reversed(CHECKS):
        op.drop_constraint(op.f(f"ck_runs_{name}"), "runs", type_="check")
    op.drop_column("runs", "escalation_reason")
