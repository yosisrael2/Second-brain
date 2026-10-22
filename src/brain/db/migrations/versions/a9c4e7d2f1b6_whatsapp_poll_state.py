"""WhatsApp poll-state table: per-chat ingestion cursor (PR D1).

Revision ID: a9c4e7d2f1b6
Revises: a1e5f7b9c3d2
Create Date: 2026-10-22 22:30:00.000000

One row per allowlisted bridge chat: cursor (UTC), last poll time and
per-channel counts (surfaced on GET /ops/whatsapp). The cursor plus an
overlap window makes polling idempotent across restarts.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "a9c4e7d2f1b6"
down_revision: str | None = "a1e5f7b9c3d2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "whatsapp_poll_state",
        sa.Column("chat_jid", sa.String(255), primary_key=True),
        sa.Column("channel_name", sa.Text(), nullable=True),
        sa.Column("cursor_utc", sa.DateTime(timezone=True), nullable=True),
        sa.Column("history_until_utc", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_poll_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_message_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("messages_seen", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("conversations_created", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("conversations_updated", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("open_item_id", postgresql.UUID(as_uuid=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_table("whatsapp_poll_state")
