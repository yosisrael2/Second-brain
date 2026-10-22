"""Every datetime in API responses carries an offset (PART 3 guard).

Root cause (found on the live DB): ``memory_items.occurred_at`` had drifted
to ``timestamp WITHOUT time zone`` (model/migrations say timestamptz). The
driver then returns naive datetimes and pydantic serializes them without an
offset, e.g. "2026-10-29T15:00:00" — which clients/LLMs misread as local
time. Fix at the source: migration d2f6a9c4e7b1 converts the column with
``USING occurred_at AT TIME ZONE 'UTC'`` (drifted values are UTC wall-time).

These tests assert the JSON contract on /items, /capture and /search, and
that the migration actually repairs a drifted (naive) column.
"""

from zoneinfo import ZoneInfo

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import text

from brain.config import get_settings
from brain.db.session import engine

NEW_HEAD = "d2f6a9c4e7b1"
PREVIOUS_HEAD = "c7d91e4a2b58"


def _alembic_cfg() -> Config:
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    cfg = Config(str(root / "alembic.ini"))
    cfg.set_main_option("script_location", str(root / "src/brain/db/migrations"))
    return cfg


def _assert_offsets(values: list, field: str = "occurred_at") -> None:
    for value in values:
        assert value is not None, f"{field} missing in response"
        assert value.endswith(("Z", "+00:00")), (
            f"{field} serialized without an offset: {value!r} "
            "(naive datetime in API response)"
        )


# ---- JSON contract (needs Postgres) ---------------------------------------------


@pytest.fixture(name="jerusalem_client")
def jerusalem_client_fixture(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    """A client whose TIMEZONE is pinned (zone-less pastes -> aware UTC)."""
    monkeypatch.setenv("TIMEZONE", "Asia/Jerusalem")
    get_settings.cache_clear()
    yield TestClient(__import__("brain.main", fromlist=["create_app"]).create_app())
    get_settings.cache_clear()


def test_items_and_capture_and_search_datetimes_carry_offsets(
    jerusalem_client: TestClient, clean_items
) -> None:
    ics = (
        "BEGIN:VEVENT\n"
        "SUMMARY:offset probe\n"
        "DTSTART;TZID=Asia/Jerusalem:20261029T170000\n"
        "END:VEVENT"
    )
    capture = jerusalem_client.post("/capture", json={"text": ics})
    assert capture.status_code == 201, capture.text

    for payload in (
        capture.json()["items"],
        jerusalem_client.get("/items?limit=200").json(),
        jerusalem_client.get("/search?q=offset+probe&limit=5").json(),
    ):
        _assert_offsets([item["occurred_at"] for item in payload])
        _assert_offsets([item["created_at"] for item in payload], field="created_at")


# ---- migration repairs the drifted column (needs Postgres) ----------------------


def test_migration_repairs_naive_occurred_at_and_backfills_fingerprint():
    cfg = _alembic_cfg()
    probe_id = None
    try:
        # rewind to the previous head so the repair migration re-runs below
        command.downgrade(cfg, PREVIOUS_HEAD)
        with engine.begin() as conn:
            # reproduce the live drift exactly: naive column holding UTC wall-time
            conn.execute(
                text(
                    "ALTER TABLE memory_items ALTER COLUMN occurred_at "
                    "TYPE timestamp USING occurred_at AT TIME ZONE 'UTC'"
                )
            )
            probe_uuid = __import__("uuid").uuid4()
            conn.execute(
                text(
                    "INSERT INTO memory_items (id, raw_text, source, status, occurred_at) "
                    "VALUES (:i, 'drift probe', 'calendar', 'inbox', '2026-10-29 15:00:00')"
                ),
                {"i": probe_uuid},
            )
            probe_id = probe_uuid

        command.upgrade(cfg, "head")  # runs d2f6a9c4e7b1 on top of the drifted state

        with engine.connect() as conn:
            kind = conn.execute(
                text(
                    "SELECT data_type FROM information_schema.columns "
                    "WHERE table_name = 'memory_items' AND column_name = 'occurred_at'"
                )
            ).scalar()
            assert kind == "timestamp with time zone", (
                f"occurred_at still drifted after migration: {kind}"
            )
            value, fp = conn.execute(
                text(
                    "SELECT occurred_at, fingerprint FROM memory_items WHERE id = :i"
                ),
                {"i": probe_id},
            ).fetchone()
            assert value is not None and value.utcoffset() is not None
            assert value.astimezone(ZoneInfo("UTC")).isoformat() == "2026-10-29T15:00:00+00:00"
            assert fp, "fingerprint not backfilled for the drifted row"
    finally:
        command.upgrade(cfg, "head")  # leave the shared test db at head, always
        with engine.begin() as conn:
            if probe_id is not None:
                conn.execute(
                    text("DELETE FROM memory_items WHERE id = :i"), {"i": probe_id}
                )


def test_migration_round_trip_downgrade_upgrade():
    cfg = _alembic_cfg()
    try:
        command.downgrade(cfg, PREVIOUS_HEAD)
        with engine.connect() as conn:
            cols = _memory_item_columns(conn)
            assert "fingerprint" not in cols
            # the type repair is NOT reverted (converting back would drop tz info)
            assert _occurred_at_type(conn) == "timestamp with time zone"
        command.upgrade(cfg, "head")
        with engine.connect() as conn:
            assert "fingerprint" in _memory_item_columns(conn)
            assert _occurred_at_type(conn) == "timestamp with time zone"
    finally:
        command.upgrade(cfg, "head")  # leave the shared test db at head, always


def _memory_item_columns(conn) -> set[str]:
    return {
        row[0]
        for row in conn.execute(
            text(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name = 'memory_items'"
            )
        )
    }


def _occurred_at_type(conn) -> str:
    return conn.execute(
        text(
            "SELECT data_type FROM information_schema.columns "
            "WHERE table_name = 'memory_items' AND column_name = 'occurred_at'"
        )
    ).scalar()
