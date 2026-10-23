"""whatsapp triage columns on memory_items

Revision ID: c9d2e7f4a1b8
Revises: a5c8f1d9e3b7
Create Date: 2026-10-23 03:00:00.000000

D2a (design delta item 36): each WhatsApp conversation is triaged
relevant | noise. Four nullable columns on memory_items (label, confidence,
method, version): the default listings filter on the label in SQL (same
style as the A1 child_state columns), so a column beats a meta JSON key.
NULL means not triaged yet — always listed. Triage never deletes anything.
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c9d2e7f4a1b8"
down_revision: str | None = "b8c4f2a6e9d1"  # was a5c8f1d9e3b7; PR B (#48) landed between
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_COLUMNS = (
    ("triage_label", sa.String(length=16)),
    ("triage_confidence", sa.Float()),
    ("triage_method", sa.String(length=8)),
    ("triage_version", sa.String(length=16)),
)


def upgrade() -> None:
    for name, kind in _COLUMNS:
        op.add_column("memory_items", sa.Column(name, kind, nullable=True))


def downgrade() -> None:
    for name, _kind in reversed(_COLUMNS):
        op.drop_column("memory_items", name)
