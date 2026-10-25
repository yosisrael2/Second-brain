"""entity archived_at

Revision ID: 7d90f6ea8407
Revises: e6b2d8f4a9c1
Create Date: 2026-10-25 15:48:48.578842

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "7d90f6ea8407"
down_revision: str | None = "e6b2d8f4a9c1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "entities",
        sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("entities", "archived_at")
