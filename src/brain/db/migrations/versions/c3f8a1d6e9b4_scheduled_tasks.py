"""scheduled_tasks + scheduled_task_runs: user-defined recurring prompts

Revision ID: c3f8a1d6e9b4
Revises: f3a7c9e2b5d8
Create Date: 2026-11-08 18:00:00.000000

Two NEW tables only — no existing table or row is touched, so the upgrade
is safe on a populated database (no rewrite, no lock on hot tables).
A task is a name + prompt + 5-field cron ``schedule`` in ``timezone``;
``next_run_at`` is the compare-and-set claim column the bot's runner moves
forward (NULL while paused); ``run_requested_at`` is the run-now queue.
Runs cascade-delete with their task.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "c3f8a1d6e9b4"
down_revision: str | None = "f3a7c9e2b5d8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_STATUSES = "'running', 'ok', 'error', 'skipped'"


def upgrade() -> None:
    op.create_table(
        "scheduled_tasks",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("prompt", sa.Text(), nullable=False),
        sa.Column("schedule", sa.String(120), nullable=False),
        sa.Column("timezone", sa.String(64), nullable=False, server_default="Asia/Jerusalem"),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("next_run_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_run_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_status", sa.String(16), nullable=True),
        sa.Column("run_requested_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint(
            f"last_status IS NULL OR last_status IN ({_STATUSES})",
            name=op.f("ck_scheduled_tasks_last_status"),
        ),
    )
    op.create_table(
        "scheduled_task_runs",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "task_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey(
                "scheduled_tasks.id",
                ondelete="CASCADE",
                name="fk_scheduled_task_runs_task_id_scheduled_tasks",
            ),
            nullable=False,
        ),
        sa.Column("trigger", sa.String(16), nullable=False),
        sa.Column("scheduled_for", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "started_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("output", sa.Text(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column(
            "cited_ids",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.CheckConstraint(f"status IN ({_STATUSES})", name=op.f("ck_scheduled_task_runs_status")),
        sa.CheckConstraint(
            "trigger IN ('schedule', 'manual')", name=op.f("ck_scheduled_task_runs_trigger")
        ),
    )
    op.create_index(
        "ix_scheduled_task_runs_task_started",
        "scheduled_task_runs",
        ["task_id", "started_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_scheduled_task_runs_task_started", table_name="scheduled_task_runs")
    op.drop_table("scheduled_task_runs")
    op.drop_table("scheduled_tasks")
