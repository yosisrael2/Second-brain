"""The connectors migration: up/down on brain_w8_test + source domains.

The ticket owns exactly ONE migration (c4e8a1d7b2f9): connector_state,
connector_records, the memory_items.source CHECK (new, NOT VALID) and the
attachments.source widening. This module proves both directions and that
the new source names are accepted while junk is still rejected.
"""

from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import text

from brain.db.models import Attachment, ConnectorRecord, ConnectorState, MemoryItem

_REVISION = "c4e8a1d7b2f9"
_PARENT = "b8c4f2a6e9d1"  # the pre-connectors head
_REPO_ROOT = Path(__file__).resolve().parents[1]


def _alembic_config() -> Config:
    cfg = Config(str(_REPO_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(_REPO_ROOT / "src/brain/db/migrations"))
    return cfg


def _tables(engine) -> set[str]:
    with engine.connect() as conn:
        rows = conn.execute(
            text("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
        ).fetchall()
    return {row[0] for row in rows}


def _constraint_sql(engine, table: str, name: str) -> str | None:
    with engine.connect() as conn:
        row = conn.execute(
            text(
                "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
                "WHERE conname = :name AND conrelid = CAST(:table AS regclass)"
            ),
            {"name": name, "table": f"public.{table}"},
        ).fetchone()
    return row[0] if row else None


def test_migration_up_and_down_on_the_test_database(database):
    from brain.db.session import SessionLocal, engine

    cfg = _alembic_config()

    # --- down: tables gone, domains back to pre-connectors -------------------
    command.downgrade(cfg, _PARENT)
    assert "connector_state" not in _tables(engine)
    assert "connector_records" not in _tables(engine)
    attachments_check = _constraint_sql(engine, "attachments", "ck_attachments_source")
    assert attachments_check is not None and "gcal" not in attachments_check
    assert _constraint_sql(engine, "memory_items", "ck_memory_items_source") is None

    # --- up: tables exist, domains widened ------------------------------------
    command.upgrade(cfg, "head")
    tables = _tables(engine)
    assert "connector_state" in tables and "connector_records" in tables
    attachments_check = _constraint_sql(engine, "attachments", "ck_attachments_source")
    for name in ("gcal", "gdrive", "gtasks"):
        assert name in attachments_check
    items_check = _constraint_sql(engine, "memory_items", "ck_memory_items_source")
    for name in ("gcal", "gdrive", "gtasks"):
        assert name in items_check

    # --- rows: connector tables accept the shapes the runner writes -----------
    item = MemoryItem(raw_text="event from calendar", source="gcal")
    with SessionLocal() as session:
        session.add(item)
        session.flush()
        session.add(ConnectorState(name="gcal", cursor="tok-1"))
        session.add(
            ConnectorRecord(source="gcal", external_id="ev1", version="v1", item_id=item.id)
        )
        session.commit()
        att = Attachment(
            item_id=item.id,
            parent_item_id=item.id,
            sha256="0" * 64,
            filename="a.pdf",
            size_bytes=3,
            source="gdrive",
        )
        session.add(att)
        session.commit()


def test_items_source_check_still_rejects_junk(database):
    """The new CHECK is real: a junk source must fail at the database."""
    from sqlalchemy.exc import IntegrityError

    from brain.db.session import SessionLocal

    with SessionLocal() as session:
        session.add(MemoryItem(raw_text="junk", source="not-a-source"))
        with pytest.raises(IntegrityError):
            session.commit()
