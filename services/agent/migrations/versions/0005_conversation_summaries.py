"""conversation summaries: the rolling summary, one append-only row per summary (ADR-002,
ADR-022 Part 1, S6)

Written by hand in the shape autogenerate produces, then checked with `alembic check`. The
runtime role keeps INSERT and SELECT from the default privileges; UPDATE is taken back, as for
`decisions` and `llm_calls`: a summary is never edited, a newer one is added.

Revision ID: 0005
Revises: 0004
Create Date: 2026-11-16
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "conversation_summaries",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("conversation_id", sa.UUID(), nullable=False),
        sa.Column("covers_through_event_id", sa.BigInteger(), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("prompt_version", sa.Text(), nullable=False),
        sa.Column("provider", sa.Text(), nullable=False),
        sa.Column("model", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "length(summary) > 0", name=op.f("ck_conversation_summaries_summary_not_empty")
        ),
        sa.ForeignKeyConstraint(
            ["conversation_id"],
            ["conversations.id"],
            name=op.f("fk_conversation_summaries_conversation_id_conversations"),
        ),
        sa.ForeignKeyConstraint(
            ["covers_through_event_id"],
            ["events.id"],
            name=op.f("fk_conversation_summaries_covers_through_event_id_events"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_conversation_summaries")),
    )
    op.create_index(
        "ix_conversation_summaries_conversation_id_covers",
        "conversation_summaries",
        ["conversation_id", "covers_through_event_id"],
        unique=False,
    )
    op.execute("REVOKE UPDATE, DELETE, TRUNCATE ON conversation_summaries FROM agent_app")


def downgrade() -> None:
    op.drop_index(
        "ix_conversation_summaries_conversation_id_covers", table_name="conversation_summaries"
    )
    op.drop_table("conversation_summaries")
