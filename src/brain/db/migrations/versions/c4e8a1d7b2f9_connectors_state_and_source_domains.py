"""Connectors: sync state + idempotency ledger + source domains.

Revision ID: c4e8a1d7b2f9
Revises: b8c4f2a6e9d1
Create Date: 2026-10-23 09:00:00.000000

The connectors ticket's ONE migration (owned by worker_8; adapters add none):

- ``connector_state(name PK, cursor, last_run_at, last_ok_at, last_error,
  counters)`` — per-connector cursor + heartbeat fields.
- ``connector_records(source, external_id, version, item_id,
  PK(source, external_id))`` — the idempotency ledger; FK to memory_items is
  ON DELETE SET NULL so a user-deleted item keeps its ledger row (item_id
  NULL) and the runner never re-captures it.
- Source domains widened for the connector names (read-only this ticket):
  ``memory_items.source`` gets its CHECK here (it never had one; the live
  table predates it, so it is added NOT VALID — new rows are enforced,
  existing rows are not re-scanned). ``gmail`` keeps the existing source
  "email" for compatibility with the email data; the new names are
  "gcal", "gdrive", "gtasks". ``attachments.source`` gains the same three.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "c4e8a1d7b2f9"
down_revision: str | None = "d7b3e9c1a5f4"  # re-pointed: #49 (triage) and #39 (A2) merged first
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# (telegram, whatsapp, email) is the attachments.domain the last migration
# created; the new sources are appended, never reordered.
_ATTACHMENT_SOURCES = ("telegram", "whatsapp", "email", "gcal", "gdrive", "gtasks")
_ITEM_SOURCES = (
    "manual",
    "note",
    "calendar",
    "email",
    "outlook",
    "whatsapp",
    "telegram",
    "gcal",
    "gdrive",
    "gtasks",
)


def _domain(values: Sequence[str]) -> str:
    return "source IN (" + ", ".join(f"'{value}'" for value in values) + ")"


def upgrade() -> None:
    op.create_table(
        "connector_state",
        sa.Column("name", sa.String(64), primary_key=True),
        sa.Column("cursor", sa.Text(), nullable=True),
        sa.Column("last_run_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_ok_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column(
            "counters",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
    )
    op.create_table(
        "connector_records",
        sa.Column("source", sa.String(64), primary_key=True),
        sa.Column("external_id", sa.Text(), primary_key=True),
        sa.Column("version", sa.Text(), nullable=True),
        sa.Column(
            "item_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("memory_items.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        # Poison-record quarantine: 3 consecutive record-level failures put
        # the row in status 'failed'; the runner then skips it (cursor can
        # advance) until the source bumps the version.
        sa.Column("status", sa.String(16), nullable=False, server_default="synced"),
        sa.Column("fail_count", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("last_error", sa.Text(), nullable=True),
    )
    op.create_index(
        op.f("ix_connector_records_item_id"), "connector_records", ["item_id"], unique=False
    )
    # memory_items.source had no CHECK; add the closed domain NOT VALID so
    # the live table (with its historical values) is never re-scanned, while
    # every new row is enforced. Fresh databases get the same constraint.
    # (Raw SQL: op.drop_constraint would run the name back through the
    # naming convention and double-prefix it.)
    op.execute(
        f"ALTER TABLE memory_items ADD CONSTRAINT ck_memory_items_source "
        f"CHECK ({_domain(_ITEM_SOURCES)}) NOT VALID"
    )
    op.execute("ALTER TABLE attachments DROP CONSTRAINT ck_attachments_source")
    op.execute(
        f"ALTER TABLE attachments ADD CONSTRAINT ck_attachments_source "
        f"CHECK ({_domain(_ATTACHMENT_SOURCES)})"
    )


def downgrade() -> None:
    op.execute(
        "ALTER TABLE attachments DROP CONSTRAINT ck_attachments_source"
    )
    op.execute(
        "ALTER TABLE attachments ADD CONSTRAINT ck_attachments_source "
        f"CHECK ({_domain(('telegram', 'whatsapp', 'email'))})"
    )
    op.execute("ALTER TABLE memory_items DROP CONSTRAINT IF EXISTS ck_memory_items_source")
    op.drop_index(op.f("ix_connector_records_item_id"), table_name="connector_records")
    op.drop_table("connector_records")
    op.drop_table("connector_state")
