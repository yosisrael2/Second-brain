"""Attachments: files linked to items, each also its own searchable item.

Revision ID: a5c8f1d9e3b7
Revises: b7c2d5e8f1a4
Create Date: 2026-10-23 00:30:00.000000

Design delta item 43: original files live OUTSIDE the repo and the DB
(content-addressed by sha256 in the configured files dir); the attachments
table links each file to its parent message/email item and to the
attachment's OWN searchable memory item (raw text = filename + extracted
text). Reading is async with a daily budget; failures are recorded, never
silent (status + error).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "a5c8f1d9e3b7"
down_revision: str | None = "a9c3e1f5b2d7"  # was b7c2d5e8f1a4; main moved past it
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "attachments",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "item_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("memory_items.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "parent_item_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("memory_items.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column("mime", sa.String(128), nullable=True),
        sa.Column("filename", sa.Text(), nullable=False),
        sa.Column("size_bytes", sa.Integer(), nullable=False),
        sa.Column("source", sa.String(16), nullable=False),
        sa.Column("source_ref", sa.Text(), nullable=True),
        sa.Column("text", sa.Text(), nullable=True),
        sa.Column("text_method", sa.String(16), nullable=True),
        sa.Column("pages", sa.Integer(), nullable=True),
        sa.Column("status", sa.String(16), nullable=False, server_default="pending"),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("read_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint(
            "source IN ('telegram', 'whatsapp', 'email')",
            name=op.f("ck_attachments_source"),
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'done', 'failed', 'unsupported', 'too_large')",
            name=op.f("ck_attachments_status"),
        ),
        sa.CheckConstraint(
            "text_method IS NULL OR text_method IN ('pdf_text', 'claude', 'none')",
            name=op.f("ck_attachments_text_method"),
        ),
    )
    op.create_index(op.f("ix_attachments_sha256"), "attachments", ["sha256"], unique=False)
    op.create_index(
        op.f("ix_attachments_parent_item_id"),
        "attachments",
        ["parent_item_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_attachments_parent_item_id"), table_name="attachments")
    op.drop_index(op.f("ix_attachments_sha256"), table_name="attachments")
    op.drop_table("attachments")
