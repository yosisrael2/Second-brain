"""FTS config english -> simple (Hebrew keyword search)

Revision ID: e8a3f7b9c4d2
Revises: c7d91e4a2b58
Create Date: 2026-10-22 17:30:00.000000

The 'english' text-search config stems English only and turns Hebrew tokens
into noise — the user's data is mostly Hebrew. The generated tsvector column
is rebuilt with the 'simple' config (no stemming; exact-token matching for
both languages). Postgres recomputes the column for every row on re-add.

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from brain.db.enums import FTS_LANGUAGE

revision: str = "e8a3f7b9c4d2"
down_revision: str | None = "c7d91e4a2b58"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE = "memory_items"
COLUMN = "search_vector"
INDEX = "ix_memory_items_search_vector"


def _rebuild(config: str) -> None:
    op.drop_index(INDEX, table_name=TABLE)
    op.drop_column(TABLE, COLUMN)
    op.add_column(
        TABLE,
        sa.Column(
            COLUMN,
            postgresql.TSVECTOR(),
            sa.Computed(f"to_tsvector('{config}', coalesce(raw_text, ''))", persisted=True),
            nullable=True,
        ),
    )
    op.create_index(INDEX, TABLE, [COLUMN], postgresql_using="gin")


def upgrade() -> None:
    _rebuild(FTS_LANGUAGE)  # 'simple'


def downgrade() -> None:
    _rebuild("english")
