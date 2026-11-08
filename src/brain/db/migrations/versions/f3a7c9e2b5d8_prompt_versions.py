"""prompt_versions: user-editable prompt texts + the memory file

Revision ID: f3a7c9e2b5d8
Revises: 7d90f6ea8407
Create Date: 2026-11-08 12:00:00.000000

Append-only, versioned texts per registry key (brain.services.prompts):
current = the highest version per key; ``text`` NULL marks a reset to the
code default; history is never rewritten. No rows = every prompt renders
byte-identical to the hard-coded defaults.

memory_items.triage_version widens 16 -> 32: an overridden triage prompt
records "triage-v3+u<N>[+m<M>]", which can outgrow 16 characters.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "f3a7c9e2b5d8"
down_revision: str | None = "7d90f6ea8407"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "prompt_versions",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("key", sa.String(64), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("text", sa.Text(), nullable=True),
        sa.Column("source", sa.String(16), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint("key", "version", name="uq_prompt_versions_key_version"),
        sa.CheckConstraint(
            "source IN ('edit', 'reset', 'restore')",
            name=op.f("ck_prompt_versions_source"),
        ),
        sa.CheckConstraint("version >= 1", name=op.f("ck_prompt_versions_version_positive")),
    )
    op.alter_column(
        "memory_items",
        "triage_version",
        existing_type=sa.String(16),
        type_=sa.String(32),
        existing_nullable=True,
    )


def downgrade() -> None:
    # Truncating keeps the downgrade total; a suffixed tag loses its tail.
    op.execute(
        "UPDATE memory_items SET triage_version = left(triage_version, 16) "
        "WHERE length(triage_version) > 16"
    )
    op.alter_column(
        "memory_items",
        "triage_version",
        existing_type=sa.String(32),
        type_=sa.String(16),
        existing_nullable=True,
    )
    op.drop_table("prompt_versions")
