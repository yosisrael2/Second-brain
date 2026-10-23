"""Item relations: supersedes/corrects/derived_from/same_as (PR B).

Revision ID: b8c4f2a6e9d1
Revises: a5c8f1d9e3b7
Create Date: 2026-10-23 02:30:00.000000

Design delta items 7, 20, 21, 22 + D4: item_relations(from_id, to_id, kind,
provenance, created_at) with a UNIQUE triple; memory_items.superseded_by
carries the strike-through state (hidden by default from /recent, /review
and search; restorable; never deleted).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "b8c4f2a6e9d1"
down_revision: str | None = "a5c8f1d9e3b7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "memory_items",
        sa.Column("superseded_by", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.create_foreign_key(
        op.f("fk_memory_items_superseded_by_memory_items"),
        "memory_items",
        "memory_items",
        ["superseded_by"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index(
        op.f("ix_memory_items_superseded_by"), "memory_items", ["superseded_by"], unique=False
    )
    op.create_table(
        "item_relations",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "from_id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
        ),
        sa.Column(
            "to_id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
        ),
        sa.Column("kind", sa.String(16), nullable=False),
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
        sa.ForeignKeyConstraint(
            ["from_id"],
            ["memory_items.id"],
            ondelete="CASCADE",
            name=op.f("fk_item_relations_from_id_memory_items"),
        ),
        sa.ForeignKeyConstraint(
            ["to_id"],
            ["memory_items.id"],
            ondelete="CASCADE",
            name=op.f("fk_item_relations_to_id_memory_items"),
        ),
        sa.CheckConstraint(
            "kind IN ('supersedes', 'corrects', 'derived_from', 'same_as')",
            name=op.f("ck_item_relations_kind"),
        ),
        sa.UniqueConstraint("from_id", "to_id", "kind", name=op.f("uq_item_relations_triple")),
    )
    op.create_index(op.f("ix_item_relations_from_id"), "item_relations", ["from_id"], unique=False)
    op.create_index(op.f("ix_item_relations_to_id"), "item_relations", ["to_id"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_item_relations_to_id"), table_name="item_relations")
    op.drop_index(op.f("ix_item_relations_from_id"), table_name="item_relations")
    op.drop_table("item_relations")
    op.drop_index(op.f("ix_memory_items_superseded_by"), table_name="memory_items")
    op.drop_constraint(
        op.f("fk_memory_items_superseded_by_memory_items"), "memory_items", type_="foreignkey"
    )
    op.drop_column("memory_items", "superseded_by")
