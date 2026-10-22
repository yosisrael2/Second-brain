"""Ops layer core tests: events, heartbeats, spans, retention, never-raise.

Every behavior here is a spec item; each test failed before its
implementation existed (skeleton stubs raised NotImplementedError).
"""

import time
import uuid

import pytest
from sqlalchemy import text

from brain import ops
from brain.config import get_settings
from brain.db.session import engine
from brain.ops import health as ops_health


def _healthy_ctx():
    """All adapters report healthy (mirrors test_ops_health's helper)."""
    return ops_health.HealthContext(
        db=lambda: {"reachable": True, "drift": False, "items_by_status": {}, "size": "1"},
        qdrant=lambda: {
            "reachable": True, "collection_exists": True,
            "vectors": 1, "items": 1, "dims_ok": True,
        },
        telegram=lambda: {"token_set": True, "allowlist_set": True, "heartbeat_age_s": 5.0},
    )


@pytest.fixture(name="ops_clean")
def ops_clean_fixture(database: None):
    """Empty the ops tables around each test (brain_test only, guarded)."""
    url = str(engine.url)
    assert url.rsplit("/", 1)[-1].endswith("_test"), f"not a test db: {url}"
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM ops_events"))
        conn.execute(text("DELETE FROM ops_heartbeats"))
    yield
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM ops_events"))
        conn.execute(text("DELETE FROM ops_heartbeats"))


class TestRecordEvent:
    def test_persists_a_row(self, ops_clean: None):
        ops.record_event(
            "jev", "classify", True, latency_ms=12.5, detail={"labels": ["task"]},
            edge="ingest->jev",
        )
        with engine.connect() as conn:
            row = conn.execute(
                text("SELECT component, kind, ok, latency_ms, detail, edge FROM ops_events")
            ).fetchone()
        assert row is not None
        assert row.component == "jev" and row.kind == "classify"
        assert row.ok is True and row.latency_ms == pytest.approx(12.5)
        assert row.detail == {"labels": ["task"]}
        assert row.edge == "ingest->jev"

    def test_never_raises_when_db_is_down(self, monkeypatch: pytest.MonkeyPatch):
        # Point ops at a dead endpoint: callers must never see an exception.
        monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://brain:brain@localhost:1/nope")
        get_settings.cache_clear()
        ops.reset_engine_for_tests()
        try:
            ops.record_event("jev", "classify", True)  # must not raise
            ops.heartbeat("jev")  # must not raise
            with ops.span("jev", "classify"):
                pass  # must not raise
        finally:
            ops.reset_engine_for_tests()
            get_settings.cache_clear()  # un-poison the memoized Settings

    def test_detail_with_item_ids_only(self, ops_clean: None):
        item_id = uuid.uuid4()
        ops.record_event(
            "ingest", "capture", True, detail={"item_ids": [str(item_id)], "chars": 42}
        )
        with engine.connect() as conn:
            detail = conn.execute(text("SELECT detail FROM ops_events")).scalar_one()
        assert detail == {"item_ids": [str(item_id)], "chars": 42}


class TestHeartbeat:
    def test_upserts_last_seen(self, ops_clean: None):
        ops.heartbeat("telegram", {"allowed_users": 2})
        with engine.connect() as conn:
            row = conn.execute(
                text("SELECT component, pid, detail FROM ops_heartbeats WHERE component='telegram'")
            ).fetchone()
        assert row is not None
        assert row.detail == {"allowed_users": 2}
        assert row.pid > 0

        ops.heartbeat("telegram", {"allowed_users": 3})  # same PK: updated, not duplicated
        with engine.connect() as conn:
            count = conn.execute(
                text("SELECT count(*) FROM ops_heartbeats WHERE component='telegram'")
            ).scalar_one()
            detail = conn.execute(
                text("SELECT detail FROM ops_heartbeats WHERE component='telegram'")
            ).scalar_one()
        assert count == 1
        assert detail == {"allowed_users": 3}

    def test_heartbeat_age_query(self, ops_clean: None):
        ops.heartbeat("telegram", {})
        with engine.connect() as conn:
            age = conn.execute(
                text("SELECT EXTRACT(EPOCH FROM (now() - last_seen)) FROM ops_heartbeats")
            ).scalar_one()
        assert age < 60


class TestSpan:
    def test_records_latency_and_ok(self, ops_clean: None):
        with ops.span("jev", "classify", edge="ingest->jev") as s:
            time.sleep(0.01)
            s.detail = {"labels": ["task"]}
        with engine.connect() as conn:
            row = conn.execute(
                text("SELECT kind, ok, latency_ms, detail, edge FROM ops_events "
                     "WHERE component='jev' ORDER BY ts DESC LIMIT 1")
            ).fetchone()
        assert row.kind == "classify" and row.ok is True
        assert row.latency_ms >= 10
        assert row.detail == {"labels": ["task"]}
        assert row.edge == "ingest->jev"

    def test_records_exception_type_and_reraises(self, ops_clean: None):
        with pytest.raises(ValueError, match="boom"):
            with ops.span("llm", "complete", edge="ask->llm"):
                raise ValueError("boom")
        with engine.connect() as conn:
            row = conn.execute(
                text("SELECT ok, detail FROM ops_events "
                     "WHERE component='llm' ORDER BY ts DESC LIMIT 1")
            ).fetchone()
        assert row.ok is False
        assert "ValueError" in str(row.detail.get("error", ""))


class TestRetention:
    def test_deletes_rows_older_than_7_days(self, ops_clean: None):
        with engine.begin() as conn:
            conn.execute(
                text("INSERT INTO ops_events (ts, component, kind, ok, detail) "
                     "VALUES (now() - interval '8 days', 'jev', 'classify', true, '{}')")
            )
            conn.execute(
                text("INSERT INTO ops_events (ts, component, kind, ok, detail) "
                     "VALUES (now() - interval '1 day', 'jev', 'classify', true, '{}')")
            )
        deleted = ops.prune_events(max_age_days=7)
        assert deleted >= 1
        with engine.connect() as conn:
            remaining = conn.execute(text("SELECT count(*) FROM ops_events")).scalar_one()
        assert remaining == 1


class TestComponentStatus:
    """The contract the Telegram bot's /status shim expects (zero bot changes)."""

    def test_returns_name_ok_summary_tuples(self, ops_clean: None, monkeypatch):
        monkeypatch.setattr(
            ops_health, "_default_context", lambda: _healthy_ctx(), raising=False
        )
        lines = ops.component_status()
        names = {name for name, _ok, _summary in lines}
        assert {"api", "postgres", "telegram"} <= names
        for _name, ok, summary in lines:
            assert isinstance(ok, bool) and isinstance(summary, str) and summary

    def test_disabled_counts_as_ok_not_down(self, ops_clean: None, monkeypatch):
        monkeypatch.setattr(
            ops_health, "_default_context", lambda: _healthy_ctx(), raising=False
        )
        lines = dict((name, (ok, summary)) for name, ok, summary in ops.component_status())
        assert lines["jev"][0] is True  # unconfigured jev is off, not failing
        assert lines["telegram"][0] is True  # no token = disabled = ok

    def test_never_raises_when_health_breaks(self, ops_clean: None, monkeypatch):
        def broken():
            raise RuntimeError("boom")

        monkeypatch.setattr(ops_health, "run_health_checks", broken)
        assert ops.component_status() == []
