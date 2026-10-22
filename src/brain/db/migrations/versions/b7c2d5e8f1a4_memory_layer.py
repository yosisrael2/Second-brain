"""Memory-layer migration: entities, entity_aliases, memory_facts.

Revision ID: b7c2d5e8f1a4
Revises: d2f6a9c4e7b1
Create Date: 2026-10-22 20:05:00.000000

Durable, confirmed knowledge about the user's world, separate from the
capture stream (design: meaning-extraction "MEMORY LAYER"). Facts are
time-versioned: a change closes the old row (valid_to) and opens a new one
linked via supersedes_id — history is never destroyed by an edit.
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "b7c2d5e8f1a4"
down_revision: str | None = "d2f6a9c4e7b1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "entities",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint(
            "kind IN ('person', 'group', 'place', 'org', 'channel')",
            name=op.f("ck_entities_kind"),
        ),
    )
    op.create_table(
        "entity_aliases",
        sa.Column(
            "entity_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("entities.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("alias", sa.Text(), primary_key=True),
        sa.Column("lang", sa.String(8), nullable=True),
    )
    op.create_table(
        "memory_facts",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "subject_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("entities.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("predicate", sa.String(64), nullable=False),
        sa.Column(
            "object_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("entities.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("value", sa.Text(), nullable=True),
        sa.Column(
            "valid_from",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("valid_to", sa.DateTime(timezone=True), nullable=True),
        sa.Column("status", sa.String(16), nullable=False, server_default="proposed"),
        sa.Column(
            "supersedes_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("memory_facts.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "provenance",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint(
            "status IN ('proposed', 'confirmed', 'rejected')",
            name=op.f("ck_memory_facts_status"),
        ),
        sa.CheckConstraint(
            "object_id IS NOT NULL OR value IS NOT NULL",
            name=op.f("ck_memory_facts_has_object_or_value"),
        ),
    )
    op.create_index("ix_memory_facts_subject", "memory_facts", ["subject_id"])
    op.create_index("ix_memory_facts_status", "memory_facts", ["status"])


def downgrade() -> None:
    op.drop_table("memory_facts")
    op.drop_table("entity_aliases")
    op.drop_table("entities")
