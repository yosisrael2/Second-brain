"""email_messages: seen-message ledger for the Gmail label poller (F2)

Revision ID: a9c3e1f5b2d7
Revises: b7c2d5e8f1a4
Create Date: 2026-10-22 23:30:00.000000

One row per processed Gmail message; the unique ``dedupe_id`` is the RFC
5322 Message-ID (or ``fp:`` + sha256(sender, subject, minute) when the
message has none) — the poller's at-most-once guarantee. ``item_id`` links
the captured memory item (SET NULL on item delete; the ledger row stays so
a re-import still cannot double-capture).
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "a9c3e1f5b2d7"
down_revision: str | None = "b7c2d5e8f1a4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "email_messages",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text("gen_random_uuid()")),
        sa.Column("dedupe_id", sa.String(255), nullable=False),
        sa.Column("gmail_id", sa.String(128), nullable=False),
        sa.Column("rfc_message_id", sa.String(998), nullable=True),
        sa.Column("item_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
        sa.ForeignKeyConstraint(["item_id"], ["memory_items.id"],
                                ondelete="SET NULL"),
        sa.UniqueConstraint("dedupe_id", name="uq_email_messages_dedupe_id"),
    )
    op.create_index("ix_email_messages_gmail_id", "email_messages", ["gmail_id"])


def downgrade() -> None:
    op.drop_index("ix_email_messages_gmail_id", table_name="email_messages")
    op.drop_table("email_messages")
