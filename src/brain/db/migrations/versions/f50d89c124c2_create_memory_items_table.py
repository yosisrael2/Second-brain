"""create memory_items table

Revision ID: f50d89c124c2
Revises:
Create Date: 2026-10-11 18:31:06.488243

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "f50d89c124c2"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "memory_items",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("raw_text", sa.Text(), nullable=False),
        sa.Column("source", sa.String(length=64), nullable=False),
        sa.Column(
            "type",
            sa.Enum(
                "task", "decision", "note", "event", "reference", "idea",
                name="itemtype", native_enum=False, length=32,
            ),
            nullable=True,
        ),
        sa.Column("project", sa.String(length=128), nullable=True),
        sa.Column("importance", sa.Integer(), nullable=True),
        sa.Column(
            "actionability",
            sa.Enum("none", "later", "now", name="actionability", native_enum=False, length=32),
            nullable=True,
        ),
        sa.Column("confidence", sa.Float(), nullable=True),
        sa.Column(
            "status",
            sa.Enum("inbox", "filed", "needs_review", name="itemstatus", native_enum=False, length=32),
            nullable=False,
        ),
        sa.Column(
            "search_vector",
            postgresql.TSVECTOR(),
            sa.Computed("to_tsvector('english', coalesce(raw_text, ''))", persisted=True),
            nullable=True,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("actionability IN ('none', 'later', 'now')", name=op.f("ck_memory_items_actionability")),
        sa.CheckConstraint("status IN ('inbox', 'filed', 'needs_review')", name=op.f("ck_memory_items_status")),
        sa.CheckConstraint(
            "type IN ('task', 'decision', 'note', 'event', 'reference', 'idea')",
            name=op.f("ck_memory_items_type"),
        ),
        sa.CheckConstraint(
            "confidence IS NULL OR confidence BETWEEN 0.0 AND 1.0",
            name=op.f("ck_memory_items_confidence_range"),
        ),
        sa.CheckConstraint(
            "importance IS NULL OR importance BETWEEN 1 AND 5",
            name=op.f("ck_memory_items_importance_range"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_memory_items")),
    )
    op.create_index(
        "ix_memory_items_search_vector",
        "memory_items",
        ["search_vector"],
        unique=False,
        postgresql_using="gin",
    )


def downgrade() -> None:
    op.drop_index("ix_memory_items_search_vector", table_name="memory_items", postgresql_using="gin")
    op.drop_table("memory_items")
