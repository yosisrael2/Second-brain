"""item_sources: drop the (child_id, source_id) UNIQUE — item 7 many-to-many.

Spec item 7 is many-to-many FROM DAY ONE, and that includes SEVERAL quotes
per (child, source) pair: a deduped near-duplicate's verbatim quote and a
re-run that grounds the same child through a different span are EVIDENCE and
belong in the link table (they used to be parked in
extraction_meta['extra_quotes'], which hid them from every consumer of
item_sources). The UNIQUE constraint also could not cover the quote column
(a long TEXT inside the UNIQUE btree fails with 'index row size exceeds
maximum' — the reason the constraint indexed only the ids); dropping it
removes that trade-off entirely. Plain indexes on both ids keep the lookups.

Revision ID: d7b3e9c1a5f4
Revises: c9d2e7f4a1b8
Create Date: 2026-10-23
"""

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "d7b3e9c1a5f4"
down_revision: str | None = "c9d2e7f4a1b8"
branch_labels = None
depends_on = None

# The constraint's generated name under the metadata naming convention
# (uq_%(table_name)s_%(column_0_name)s) — see src/brain/db/base.py.
_UQ = "uq_item_sources_child_id"
_IX_CHILD = "ix_item_sources_child_id"


def upgrade() -> None:
    op.drop_constraint(_UQ, "item_sources", type_="unique")
    op.create_index(_IX_CHILD, "item_sources", ["child_id"])


def downgrade() -> None:
    # Rebuilding the UNIQUE is only possible when no pair carries two rows;
    # collapse duplicates back to one row per (child_id, source_id) first,
    # keeping the earliest quote, exactly what pre-item-7 consumers saw.
    op.execute(
        """
        DELETE FROM item_sources a
        USING item_sources b
        WHERE a.child_id = b.child_id
          AND a.source_id = b.source_id
          AND a.created_at > b.created_at
        """
    )
    op.drop_index(_IX_CHILD, table_name="item_sources")
    op.create_unique_constraint(_UQ, "item_sources", ["child_id", "source_id"])
