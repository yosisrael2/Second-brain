"""Extraction children: item_sources, enrichment_jobs, child columns.

Revision ID: a1e5f7b9c3d2
Revises: b7c2d5e8f1a4
Create Date: 2026-10-22 21:40:00.000000

Meaning extraction PR A1 (design: meaning-extraction-spec.md + v2 delta):
one capture can hold several facts; an enrichment pass creates CHILD items
linked to their source. Children are ordinary memory_items (one search
index — delta item 6); the link table is many-to-many from day one
(item 7). The durable enrichment queue lets the runner drain oldest-first
under a daily LLM cap (D5/D6) and survive restarts.
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "a1e5f7b9c3d2"
down_revision: str | None = "b7c2d5e8f1a4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_CHILD_STATES = ("proposed", "kept", "dropped")


def upgrade() -> None:
    # --- child markers on memory_items -----------------------------------
    # child_state: NULL = ordinary top-level item; else the D2/item-19
    # lifecycle. 'kept' children are user-approved; 'dropped' are user-
    # rejected and remembered so re-runs never recreate them (item 30).
    op.add_column(
        "memory_items",
        sa.Column("child_state", sa.String(16), nullable=True),
    )
    op.create_check_constraint(
        op.f("ck_memory_items_child_state"),
        "memory_items",
        "child_state IS NULL OR child_state IN ('proposed', 'kept', 'dropped')",
    )
    # applies_to: resolved audience (item 17 / rule R1-R5) as a JSON list —
    # e.g. ["Rona"], ["me"], or ["unknown"] (unknown stays unknown, R4).
    op.add_column(
        "memory_items",
        sa.Column("applies_to", postgresql.JSONB(), nullable=True),
    )
    # date_conflict: weekday-vs-date disagreement (item 2b) — the child is
    # flagged, never silently fixed; conflict children land in needs_review.
    op.add_column(
        "memory_items",
        sa.Column("date_conflict", sa.Boolean(), nullable=True),
    )
    # extraction_meta: provenance (item 7): model, prompt_version, run_id,
    # extracted_at, and date-resolution notes (e.g. both weekday readings).
    op.add_column(
        "memory_items",
        sa.Column("extraction_meta", postgresql.JSONB(), nullable=True),
    )

    # --- many-to-many child<->source links (item 7) ----------------------
    op.create_table(
        "item_sources",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "child_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("memory_items.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "source_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("memory_items.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("quote", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        # convention uq_%(table_name)s_%(column_0_name)s -> uq_item_sources_child_id
        sa.UniqueConstraint("child_id", "source_id", "quote"),
    )
    op.create_index(
        op.f("ix_item_sources_source_id"), "item_sources", ["source_id"]
    )

    # --- durable enrichment queue -----------------------------------------
    op.create_table(
        "enrichment_jobs",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "item_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("memory_items.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("state", sa.String(16), nullable=False),
        sa.Column(
            "budget",
            sa.String(16),
            nullable=False,
            server_default="daily",
        ),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("llm_used", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("not_before", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "queued_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.CheckConstraint(
            "state IN ('queued', 'running', 'done', 'failed')",
            name=op.f("ck_enrichment_jobs_state"),
        ),
        sa.CheckConstraint(
            "budget IN ('daily', 'backfill')",
            name=op.f("ck_enrichment_jobs_budget"),
        ),
        # convention uq_%(table_name)s_%(column_0_name)s -> uq_enrichment_jobs_item_id
        sa.UniqueConstraint("item_id"),
    )
    op.create_index(
        op.f("ix_enrichment_jobs_state_not_before"),
        "enrichment_jobs",
        ["state", "not_before"],
    )


def downgrade() -> None:
    op.drop_index(
        op.f("ix_enrichment_jobs_state_not_before"), table_name="enrichment_jobs"
    )
    op.drop_table("enrichment_jobs")
    op.drop_index(op.f("ix_item_sources_source_id"), table_name="item_sources")
    op.drop_table("item_sources")
    op.drop_column("memory_items", "extraction_meta")
    op.drop_column("memory_items", "date_conflict")
    op.drop_column("memory_items", "applies_to")
    op.drop_constraint(
        op.f("ck_memory_items_child_state"), "memory_items", type_="check"
    )
    op.drop_column("memory_items", "child_state")
