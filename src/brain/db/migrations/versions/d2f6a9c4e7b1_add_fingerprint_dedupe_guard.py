"""add fingerprint dedupe guard + repair occurred_at column type

Revision ID: d2f6a9c4e7b1
Revises: c7d91e4a2b58
Create Date: 2026-10-22 17:30:00.000000

Two changes:

1. ``memory_items.fingerprint`` (String(64), indexed) — content fingerprint
   for the exact-duplicate ingest guard, backfilled here in Python via the
   same :func:`brain.services.dedupe.content_fingerprint` the runtime uses
   (guarantees backfilled and freshly computed fingerprints never diverge).

2. Repair ``occurred_at`` column-type drift found on a live database: the
   table there was created as ``timestamp WITHOUT time zone`` while the model
   and every migration say timestamptz. Values in that drift state are UTC
   wall-time (aware datetimes were stored via the driver's UTC encoding), so
   the repair is ``USING occurred_at AT TIME ZONE 'UTC'``. A database whose
   column is already timestamptz is left untouched.
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from brain.services.dedupe import content_fingerprint

revision: str = "d2f6a9c4e7b1"
down_revision: str | None = "c7d91e4a2b58"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_BATCH = 500


def upgrade() -> None:
    _repair_occurred_at_type()
    op.add_column("memory_items", sa.Column("fingerprint", sa.String(64), nullable=True))
    op.create_index("ix_memory_items_fingerprint", "memory_items", ["fingerprint"])
    _backfill_fingerprints()


def _repair_occurred_at_type() -> None:
    """Convert a naive `timestamp` occurred_at column to timestamptz (no-op otherwise).

    Drifted values hold UTC wall-time, hence `AT TIME ZONE 'UTC'`. Sanity
    guard: the repair only runs when the column type is missing a timezone.
    """
    conn = op.get_bind()
    kind = conn.execute(
        sa.text(
            "SELECT data_type FROM information_schema.columns "
            "WHERE table_name = 'memory_items' AND column_name = 'occurred_at'"
        )
    ).scalar()
    if kind == "timestamp without time zone":
        conn.execute(
            sa.text(
                "ALTER TABLE memory_items ALTER COLUMN occurred_at "
                "TYPE timestamptz USING occurred_at AT TIME ZONE 'UTC'"
            )
        )


def _backfill_fingerprints() -> None:
    """Fill fingerprint for existing rows with the runtime's exact function."""
    conn = op.get_bind()
    offset = 0
    while True:
        rows = conn.execute(
            sa.text(
                "SELECT id, raw_text, occurred_at, source FROM memory_items "
                "ORDER BY created_at, id LIMIT :limit OFFSET :offset"
            ),
            {"limit": _BATCH, "offset": offset},
        ).fetchall()
        if not rows:
            return
        updates = [
            {
                "id": row.id,
                "fp": content_fingerprint(row.raw_text, row.occurred_at, row.source),
            }
            for row in rows
        ]
        conn.execute(
            sa.text(
                "UPDATE memory_items SET fingerprint = :fp WHERE id = :id"
            ),
            updates,  # type: ignore[arg-type]  (executemany params list)
        )
        offset += len(rows)


def downgrade() -> None:
    op.drop_index("ix_memory_items_fingerprint", table_name="memory_items")
    op.drop_column("memory_items", "fingerprint")
    # occurred_at is intentionally left at timestamptz on downgrade: it is a
    # type repair (not a schema feature) and converting back would make every
    # stored value naive again — data loss, not a rollback.
