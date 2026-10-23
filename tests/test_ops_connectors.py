"""C6 Sources panel: GET /ops/connectors (read-only feed for the cockpit).

The panel lists every REGISTERED connector plus every connector that has
state rows (stale ones included, greyed), with last ok / last error / cursor
presence / records synced / quarantined. The cursor token itself is never
exposed; the cockpit renders last_error as text (textContent), so no payload
value can inject HTML.
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from brain.main import create_app


@pytest.fixture(name="conn_db")
def conn_db_fixture(database: None):
    """Fresh connector tables; memory_items cleaned after (runner_db shape)."""
    from brain.db.session import SessionLocal, engine

    with engine.begin() as conn:
        for table in ("connector_records", "connector_state"):
            conn.execute(text(f"DELETE FROM {table}"))
        conn.execute(text("DELETE FROM memory_items"))
    session = SessionLocal()
    yield session
    session.rollback()
    session.close()
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM connector_records"))
        conn.execute(text("DELETE FROM connector_state"))
        conn.execute(text("DELETE FROM memory_items"))


def _state(session: Any, name: str, **fields: Any) -> None:
    from brain.db.models import ConnectorState

    defaults: dict[str, Any] = {"name": name}
    defaults.update(fields)
    session.add(ConnectorState(**defaults))
    session.commit()


def _records(session: Any, source: str, external_ids: list[str]) -> None:
    from brain.db.models import ConnectorRecord

    for eid in external_ids:
        session.add(
            ConnectorRecord(source=source, external_id=eid, version="v1", item_id=None)
        )
    session.commit()


def _get(monkeypatch: pytest.MonkeyPatch, *, registered: list[str], enabled: str) -> dict:
    import brain.api.routes.ops as ops_routes

    def settings():
        return SimpleNamespace(
            connectors_enabled=tuple(p for p in enabled.split(",") if p)
        )

    monkeypatch.setattr(ops_routes, "all_names", lambda: registered)
    monkeypatch.setattr(ops_routes, "get_settings", settings)
    client = TestClient(create_app())
    return client.get("/ops/connectors").json()


def test_zero_connectors_render_an_empty_panel(conn_db, monkeypatch: pytest.MonkeyPatch):
    payload = _get(monkeypatch, registered=[], enabled="")

    assert payload == {"connectors": []}


def test_one_ok_and_one_errored_with_last_error_and_counts(
    conn_db, monkeypatch: pytest.MonkeyPatch
):
    _state(
        conn_db,
        "email",
        cursor="c1",
        last_run_at=datetime(2026, 10, 23, 9, 0, tzinfo=UTC),
        last_ok_at=datetime(2026, 10, 23, 9, 0, tzinfo=UTC),
        last_error=None,
        counters={"quarantined": 2},
    )
    _state(
        conn_db,
        "gcal",
        cursor=None,
        last_run_at=datetime(2026, 10, 23, 8, 0, tzinfo=UTC),
        last_ok_at=datetime(2026, 10, 22, 8, 0, tzinfo=UTC),
        last_error="refresh token rejected: <invalid_grant> & more",
    )
    _records(conn_db, "email", ["m1", "m2", "m3"])
    _records(conn_db, "gcal", ["e1"])

    payload = _get(monkeypatch, registered=["email", "gcal"], enabled="email")
    rows = {row["name"]: row for row in payload["connectors"]}

    assert set(rows) == {"email", "gcal"}
    email, gcal = rows["email"], rows["gcal"]
    assert email["enabled"] is True
    assert email["cursor_present"] is True
    assert email["records"] == 3
    assert email["quarantined"] == 2
    assert email["last_error"] is None
    assert email["last_ok_at"] == "2026-10-23T09:00:00+00:00"
    # The errored one: enabled=False (not in BRAIN_CONNECTORS) but LISTED
    # (stale state rows are shown greyed, per the approved C6 plan).
    assert gcal["enabled"] is False
    assert gcal["cursor_present"] is False
    assert gcal["records"] == 1
    assert gcal["quarantined"] == 0
    assert gcal["last_error"] == "refresh token rejected: <invalid_grant> & more"
    assert gcal["last_ok_at"] == "2026-10-22T08:00:00+00:00"


def test_last_error_is_returned_verbatim_for_text_rendering(
    conn_db, monkeypatch: pytest.MonkeyPatch
):
    """The cockpit renders last_error via textContent (never innerHTML), so
    the endpoint returns the provider string VERBATIM — no HTML wrapping on
    either side; the escaping happens by construction at render time."""
    raw = '<script>alert("x")</script> & quoting'
    _state(conn_db, "gcal", last_error=raw)

    payload = _get(monkeypatch, registered=["gcal"], enabled="gcal")

    assert payload["connectors"][0]["last_error"] == raw


def test_stale_state_row_without_registration_shows_disabled(
    conn_db, monkeypatch: pytest.MonkeyPatch
):
    _state(conn_db, "gdrive", cursor=None)
    payload = _get(monkeypatch, registered=["email"], enabled="email")

    rows = {row["name"]: row for row in payload["connectors"]}
    # email: registered + enabled but never synced (all-null stats, 0 records);
    # gdrive: a STALE state row — not registered, shown greyed (enabled=False).
    assert set(rows) == {"email", "gdrive"}
    assert rows["email"]["enabled"] is True
    assert rows["email"]["records"] == 0
    assert rows["gdrive"]["enabled"] is False
    assert rows["gdrive"]["cursor_present"] is False
