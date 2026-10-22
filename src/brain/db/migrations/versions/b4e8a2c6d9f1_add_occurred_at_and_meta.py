"""add occurred_at and meta to memory_items

Revision ID: b4e8a2c6d9f1
Revises: f50d89c124c2
Create Date: 2026-10-22 09:10:00.000000

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "b4e8a2c6d9f1"
down_revision: str | None = "f50d89c124c2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "memory_items",
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "memory_items",
        sa.Column(
            "meta",
            postgresql.JSONB(),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
    )


def downgrade() -> None:
    op.drop_column("memory_items", "meta")
    op.drop_column("memory_items", "occurred_at")
