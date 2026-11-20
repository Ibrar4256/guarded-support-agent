"""llm_calls outcome 'request_too_large': a 413 from the provider (Groq above its tokens a
minute), kept apart from 'context_too_long'

Written by hand. Downgrading folds the new outcome back into 'context_too_long', the class it
was handled as, so the old CHECK can return.

Revision ID: 0007
Revises: 0006
Create Date: 2026-11-20
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

NAME = "ck_llm_calls_known_outcome"
BEFORE = (
    "ok",
    "rate_limited",
    "provider_timeout",
    "provider_unavailable",
    "quota_exhausted",
    "auth_failed",
    "context_too_long",
    "input_rejected",
    "invalid_request",
)
AFTER = (*BEFORE[:7], "request_too_large", *BEFORE[7:])


def _check(outcomes: Sequence[str]) -> str:
    return "outcome IN (" + ", ".join(f"'{o}'" for o in outcomes) + ")"


def upgrade() -> None:
    op.drop_constraint(op.f(NAME), "llm_calls", type_="check")
    op.create_check_constraint(op.f(NAME), "llm_calls", _check(AFTER))


def downgrade() -> None:
    op.execute(
        "UPDATE llm_calls SET outcome = 'context_too_long' WHERE outcome = 'request_too_large'"
    )
    op.drop_constraint(op.f(NAME), "llm_calls", type_="check")
    op.create_check_constraint(op.f(NAME), "llm_calls", _check(BEFORE))
