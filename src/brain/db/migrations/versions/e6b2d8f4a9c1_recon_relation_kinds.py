"""item_relations: reconciliation kinds details/cancels/conflicts (PR D-recon).

Delta item 37: every extracted child gets exactly one reconciliation verdict
before it becomes a proposal. Three verdicts need QUERYABLE rows (Ask must
surface conflicts — item 38; cancellations must be restorable — D4; additions
attach to their item), so the kind domain grows from four to seven:

  supersedes, corrects, derived_from, same_as   (PR B, unchanged)
  details      — MORE DETAIL: proposed addition -> the existing item
  cancels      — CANCELLATION: proposed strike (provenance.state
                 proposed/applied/undone, D4 restore, never deleted)
  conflicts    — CONFLICT: both versions kept side by side (item 38)

Plain CHECK swap; no table or column changes. Downgrade refuses to run while
rows with the new kinds exist — those rows record user-visible decisions and
are never silently destroyed (delete them first).

Revision ID: e6b2d8f4a9c1
Revises: c4e8a1d7b2f9 (was d7b3e9c1a5f4; the connectors PR #63 landed
between — single-head chain kept)
Create Date: 2026-10-24
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy import bindparam

revision: str = "e6b2d8f4a9c1"
down_revision: str | None = "c4e8a1d7b2f9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_LEGACY = "kind IN ('supersedes', 'corrects', 'derived_from', 'same_as')"
_FULL = (
    "kind IN ('supersedes', 'corrects', 'derived_from', 'same_as', "
    "'details', 'cancels', 'conflicts')"
)
_NEW_KINDS = ("details", "cancels", "conflicts")


def upgrade() -> None:
    op.drop_constraint(op.f("ck_item_relations_kind"), "item_relations", type_="check")
    op.create_check_constraint(op.f("ck_item_relations_kind"), "item_relations", _FULL)


def downgrade() -> None:
    conn = op.get_bind()
    alive = conn.execute(
        sa.text("SELECT count(*) FROM item_relations WHERE kind IN :kinds").bindparams(
            bindparam("kinds", expanding=True)
        ),
        {"kinds": list(_NEW_KINDS)},
    ).scalar()
    if alive:
        raise RuntimeError(
            "cannot downgrade: item_relations rows with reconciliation kinds "
            f"{_NEW_KINDS} exist ({alive} rows) — delete them first; they are "
            "user-visible decisions, never silently destroyed"
        )
    op.drop_constraint(op.f("ck_item_relations_kind"), "item_relations", type_="check")
    op.create_check_constraint(op.f("ck_item_relations_kind"), "item_relations", _LEGACY)
