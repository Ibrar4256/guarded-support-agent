"""llm_calls purpose 'warrant': the warrant check's model calls (ADR-029 W01)

Written by hand. Downgrading folds warrant calls into 'turn', like 0007's fold: they were made
during a turn (they carry its turn id), and spend totals stay whole. Call records outlive their
runs by design, so refusing while any exist would block every later downgrade.

Revision ID: 0008
Revises: 0007
Create Date: 2026-11-27
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

NAME = "ck_llm_calls_known_purpose"
BEFORE = ("turn", "summary")
AFTER = (*BEFORE, "warrant")


def _check(purposes: Sequence[str]) -> str:
    return "purpose IN (" + ", ".join(f"'{p}'" for p in purposes) + ")"


def upgrade() -> None:
    op.drop_constraint(op.f(NAME), "llm_calls", type_="check")
    op.create_check_constraint(op.f(NAME), "llm_calls", _check(AFTER))


def downgrade() -> None:
    op.execute("UPDATE llm_calls SET purpose = 'turn' WHERE purpose = 'warrant'")
    op.drop_constraint(op.f(NAME), "llm_calls", type_="check")
    op.create_check_constraint(op.f(NAME), "llm_calls", _check(BEFORE))
