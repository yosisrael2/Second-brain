"""create ops tables (events, heartbeats)

Revision ID: c7d91e4a2b58
Revises: b4e8a2c6d9f1
Create Date: 2026-10-22 11:45:00.000000

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "c7d91e4a2b58"
down_revision: str | None = "b4e8a2c6d9f1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "ops_events",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text("gen_random_uuid()")),
        sa.Column("ts", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
        sa.Column("component", sa.String(32), nullable=False),
        sa.Column("kind", sa.String(64), nullable=False),
        sa.Column("ok", sa.Boolean(), nullable=False),
        sa.Column("latency_ms", sa.Float(), nullable=True),
        sa.Column("edge", sa.String(64), nullable=True),
        sa.Column("detail", postgresql.JSONB(), nullable=False,
                  server_default=sa.text("'{}'::jsonb")),
    )
    op.create_index("ix_ops_events_component_ts", "ops_events", ["component", "ts"])
    op.create_table(
        "ops_heartbeats",
        sa.Column("component", sa.String(32), primary_key=True),
        sa.Column("last_seen", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
        sa.Column("pid", sa.Integer(), nullable=True),
        sa.Column("version", sa.String(64), nullable=True),
        sa.Column("detail", postgresql.JSONB(), nullable=False,
                  server_default=sa.text("'{}'::jsonb")),
    )


def downgrade() -> None:
    op.drop_table("ops_heartbeats")
    op.drop_index("ix_ops_events_component_ts", table_name="ops_events")
    op.drop_table("ops_events")
