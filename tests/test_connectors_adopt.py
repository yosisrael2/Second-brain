"""brain connectors adopt — teach the idempotency ledger about legacy items.

One-time per-connector step run before a connector's FIRST live sync: match
the connector's fetched records against the legacy items (pasted calendar
one-liners, bot-forwarded emails) and write connector_records rows pointing
at the EXISTING items, so the first sync sees them as known instead of
capturing second copies (PR #55 review F1, PR #59 review F1).

Real repositories on the *_test database; the connector is the kit's paging
FakeConnector (calendar-like and email-like sources) — no network, no
consent, never the live server.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from qdrant_client import QdrantClient
from sqlalchemy import text
from tests.fakes import StubClassifier

from brain.ai.classify import FALLBACK_CLASSIFICATION
from brain.ai.embedder import FakeEmbedder
from brain.ai.vectorstore import QdrantVectorStore
from brain.connectors.adopt import (
    RULE_C_WINDOW,
    adopt_connector,
    undo_adopt,
)
from brain.connectors.base import FetchResult
from brain.connectors.runner import ConnectorRunner
from brain.connectors.state import ConnectorRecordRepo, ConnectorStateRepo
from brain.connectors.testing import (
    FakeConnector,
    RecordingAttachments,
    RecordingEnrichment,
    RecordingIndexer,
    make_record,
)
from brain.db.models import MemoryItem
from brain.repositories.items import ItemsRepository
from brain.services.ingest import IngestService


@pytest.fixture(name="adopt_db")
def adopt_db_fixture(database: None):
    """Fresh connector_state/connector_records; memory_items cleaned after."""
    from brain.db.session import SessionLocal, engine

    with engine.begin() as conn:
        for table in ("connector_records", "connector_state", "ops_events"):
            conn.execute(text(f"DELETE FROM {table}"))
        conn.execute(text("DELETE FROM memory_items"))
    session = SessionLocal()
    yield session
    session.rollback()
    session.close()
    with engine.begin() as conn:
        for table in ("connector_records", "connector_state", "ops_events"):
            conn.execute(text(f"DELETE FROM {table}"))
        conn.execute(text("DELETE FROM memory_items"))


class Harness:
    """Legacy items + production-shaped ingest for the money test."""

    def __init__(self, session: Any) -> None:
        self.session = session
        self.repo = ItemsRepository(session)
        self.classifier = StubClassifier(dict(FALLBACK_CLASSIFICATION))
        self.indexer = RecordingIndexer()
        self.enrichment = RecordingEnrichment()
        self.attachments = RecordingAttachments()
        self.ingest = IngestService(
            self.repo,
            self.classifier,
            _InMemoryIndexer(),
            enrichment=self.enrichment,
        )
        self.state = ConnectorStateRepo(session)
        self.records = ConnectorRecordRepo(session)

    def runner(self, connector: FakeConnector) -> ConnectorRunner:
        return ConnectorRunner(
            connector,
            ingest=self.ingest,
            repo=self.repo,
            state=self.state,
            records=self.records,
            indexer=self.indexer,
            enrichment=self.enrichment,
            attachments=self.attachments,
            clock=lambda: datetime(2026, 10, 23, 12, 0, tzinfo=UTC),
        )

    def seed_legacy(
        self,
        *,
        source: str,
        raw_text: str,
        occurred_at: datetime | None = None,
        meta: dict[str, Any] | None = None,
        type: str = "event",
    ) -> MemoryItem:
        """Insert a legacy item the way the pre-connector capture paths did."""
        item = MemoryItem(
            raw_text=raw_text,
            source=source,
            type=type,
            status="filed",
            actionability="none",
            importance=3,
            confidence=0.8,
            occurred_at=occurred_at,
            meta=meta or {},
        )
        self.session.add(item)
        self.session.commit()
        self.session.refresh(item)
        return item


class _InMemoryIndexer:
    """ItemIndexerProto over an in-memory Qdrant (the real indexer class)."""

    def __init__(self) -> None:
        from brain.services.indexing import QdrantItemIndexer

        self._inner = QdrantItemIndexer(
            FakeEmbedder(dim=8), QdrantVectorStore(client=QdrantClient(":memory:"))
        )
        self.calls = 0

    def index(self, item: MemoryItem) -> None:
        self.calls += 1
        self._inner.index(item)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


def _batch(*records: Any, cursor: str | None = None) -> FetchResult:
    return FetchResult(records=list(records), next_cursor=cursor, reset=False)


# --- the money test -----------------------------------------------------------


def test_adopt_then_first_sync_creates_no_duplicates(adopt_db):
    """Adopt -> real runner sync with the SAME records: created=0, updated=0,
    skipped=N. This is the guarantee the whole feature exists for."""
    h = Harness(adopt_db)
    legacy = h.seed_legacy(
        source="calendar",
        raw_text="אימון כדורסל — Thu 29 Oct 17:00–18:30 @ השדה 10",
        occurred_at=datetime(2026, 10, 29, 14, 30, tzinfo=UTC),
        meta={"title": "אימון כדורסל", "start": "2026-10-29T17:00:00+02:00"},
    )
    record = make_record(
        external_id="evt_1",
        version='"etag-1"',
        title="אימון כדורסל",
        text="אימון כדורסל — Thu 29 Oct 17:00–18:30 @ השדה 10",
        occurred_at=datetime(2026, 10, 29, 14, 30, tzinfo=UTC),
    )
    connector = FakeConnector(name="gcal", batches=[_batch(record)])

    report = adopt_connector(connector, adopt_db)
    assert report.adopted == [(("evt_1", '"etag-1"'), legacy.id)]
    assert report.refused_cursor is False

    summary = h.runner(FakeConnector(name="gcal", batches=[_batch(record)])).run()
    assert (summary.created, summary.updated, summary.skipped) == (0, 0, 1)
    # still ONE item for that event, the original legacy row
    assert adopt_db.query(MemoryItem).filter_by(source="gcal").count() == 0
    assert adopt_db.query(MemoryItem).filter_by(source="calendar").count() == 1


def test_adopted_item_updates_in_place_when_source_changed_between_adopt_and_sync(
    adopt_db,
):
    h = Harness(adopt_db)
    legacy = h.seed_legacy(
        source="calendar",
        raw_text="אימון כדורסל — Thu 29 Oct 17:00–18:30",
        occurred_at=datetime(2026, 10, 29, 14, 30, tzinfo=UTC),
    )
    at_adopt = make_record(
        external_id="evt_1",
        version='"etag-1"',
        text="אימון כדורסל — Thu 29 Oct 17:00–18:30",
        occurred_at=datetime(2026, 10, 29, 14, 30, tzinfo=UTC),
    )
    adopt_connector(FakeConnector(name="gcal", batches=[_batch(at_adopt)]), adopt_db)

    moved = make_record(
        external_id="evt_1",
        version='"etag-2"',
        text="אימון כדורסל — Fri 30 Oct 10:00–11:00",
        occurred_at=datetime(2026, 10, 30, 7, 0, tzinfo=UTC),
    )
    summary = h.runner(FakeConnector(name="gcal", batches=[_batch(moved)])).run()
    assert (summary.created, summary.updated) == (0, 1)
    refreshed = h.repo.get(legacy.id)
    assert refreshed is not None
    assert refreshed.occurred_at == datetime(2026, 10, 30, 7, 0, tzinfo=UTC)


# --- writes / no-writes --------------------------------------------------------


def test_adopt_dry_run_writes_nothing(adopt_db):
    h = Harness(adopt_db)
    h.seed_legacy(
        source="calendar",
        raw_text="אימון כדורסל — Thu 29 Oct 17:00–18:30",
        occurred_at=datetime(2026, 10, 29, 14, 30, tzinfo=UTC),
    )
    record = make_record(
        external_id="evt_1",
        version='"etag-1"',
        text="אימון כדורסל — Thu 29 Oct 17:00–18:30",
        occurred_at=datetime(2026, 10, 29, 14, 30, tzinfo=UTC),
    )

    def counts() -> dict[str, int]:
        out = {}
        for table in ("connector_records", "ops_events", "connector_state"):
            out[table] = adopt_db.execute(text(f"SELECT count(*) FROM {table}")).scalar_one()
        return out

    before = counts()
    report = adopt_connector(
        FakeConnector(name="gcal", batches=[_batch(record)]), adopt_db, dry_run=True
    )
    assert report.adopted and report.wrote is False
    assert counts() == before


def test_adopt_writes_ledger_rows_pointing_at_existing_items(adopt_db):
    h = Harness(adopt_db)
    legacy = h.seed_legacy(
        source="calendar",
        raw_text="אימון כדורסל — Thu 29 Oct 17:00–18:30",
        occurred_at=datetime(2026, 10, 29, 14, 30, tzinfo=UTC),
    )
    before = (
        legacy.raw_text,
        legacy.occurred_at,
        legacy.meta,
        legacy.source,
        legacy.status,
    )
    record = make_record(
        external_id="evt_1",
        version='"etag-1"',
        text="אימון כדורסל — Thu 29 Oct 17:00–18:30",
        occurred_at=datetime(2026, 10, 29, 14, 30, tzinfo=UTC),
    )
    report = adopt_connector(FakeConnector(name="gcal", batches=[_batch(record)]), adopt_db)
    assert report.wrote is True

    row = h.records.get("gcal", "evt_1")
    assert row is not None and row.item_id == legacy.id
    assert row.version == '"etag-1"'
    assert row.status == "synced"
    # the item CONTENT is untouched; meta gained only the baseline stamp the
    # runner's user-edited guard needs (connectors adopt the current text as
    # the framework baseline — see adopt._stamp_baseline)
    after = h.repo.get(legacy.id)
    assert after is not None
    assert after.raw_text == before[0]
    assert after.occurred_at == before[1]
    assert after.source == before[3]
    assert after.status == before[4]
    assert set(after.meta.keys()) - set(before[2].keys()) == {"connector_text_sha"}


def test_adopt_is_idempotent_second_run_writes_nothing(adopt_db):
    h = Harness(adopt_db)
    legacy = h.seed_legacy(
        source="calendar",
        raw_text="אימון כדורסל — Thu 29 Oct 17:00–18:30",
        occurred_at=datetime(2026, 10, 29, 14, 30, tzinfo=UTC),
    )
    record = make_record(
        external_id="evt_1",
        version='"etag-1"',
        text="אימון כדורסל — Thu 29 Oct 17:00–18:30",
        occurred_at=datetime(2026, 10, 29, 14, 30, tzinfo=UTC),
    )
    connector = FakeConnector(name="gcal", batches=[_batch(record), _batch(record)])
    first = adopt_connector(connector, adopt_db)
    second = adopt_connector(connector, adopt_db)
    assert first.wrote is True
    assert second.already == [("evt_1", legacy.id)]
    assert second.adopted == []
    assert second.wrote is False


def test_adopt_logs_one_ops_event_with_the_triples_it_wrote(adopt_db):
    h = Harness(adopt_db)
    legacy = h.seed_legacy(
        source="calendar",
        raw_text="אימון כדורסל — Thu 29 Oct 17:00–18:30",
        occurred_at=datetime(2026, 10, 29, 14, 30, tzinfo=UTC),
    )
    record = make_record(
        external_id="evt_1",
        version='"etag-1"',
        text="אימון כדורסל — Thu 29 Oct 17:00–18:30",
        occurred_at=datetime(2026, 10, 29, 14, 30, tzinfo=UTC),
    )
    adopt_connector(FakeConnector(name="gcal", batches=[_batch(record)]), adopt_db)
    from brain.db.models import OpsEvent

    events = adopt_db.query(OpsEvent).filter_by(component="connectors", kind="adopt").all()
    assert len(events) == 1
    detail = events[0].detail
    assert detail["connector"] == "gcal"
    assert detail["adopted"] == [
        ["evt_1", '"etag-1"', str(legacy.id), None]
    ]  # quad incl. prior_sha


# --- ambiguity / guards --------------------------------------------------------


def test_adopt_skips_ambiguous_matches_and_reports_them(adopt_db):
    h = Harness(adopt_db)
    text = "אימון כדורסל — Thu 29 Oct 17:00–18:30"
    occurred = datetime(2026, 10, 29, 14, 30, tzinfo=UTC)
    h.seed_legacy(source="calendar", raw_text=text, occurred_at=occurred)
    h.seed_legacy(source="calendar", raw_text=text, occurred_at=occurred)
    record = make_record(external_id="evt_1", version='"etag-1"', text=text, occurred_at=occurred)
    report = adopt_connector(FakeConnector(name="gcal", batches=[_batch(record)]), adopt_db)
    assert report.adopted == []
    assert report.ambiguous == ["evt_1"]
    assert h.records.get("gcal", "evt_1") is None


def test_adopt_respects_user_deleted_ledger_rows(adopt_db):
    h = Harness(adopt_db)
    h.seed_legacy(
        source="calendar",
        raw_text="אימון כדורסל — Thu 29 Oct 17:00–18:30",
        occurred_at=datetime(2026, 10, 29, 14, 30, tzinfo=UTC),
    )
    # the user deleted the item the ledger used to point at:
    # item_id NULL, fail_count=0, status synced — USER-DELETED marker.
    from brain.db.models import ConnectorRecord

    adopt_db.add(
        ConnectorRecord(
            source="gcal",
            external_id="evt_1",
            version='"etag-1"',
            item_id=None,
            status="synced",
            fail_count=0,
        )
    )
    adopt_db.commit()
    record = make_record(
        external_id="evt_1",
        version='"etag-1"',
        text="אימון כדורסל — Thu 29 Oct 17:00–18:30",
        occurred_at=datetime(2026, 10, 29, 14, 30, tzinfo=UTC),
    )
    report = adopt_connector(FakeConnector(name="gcal", batches=[_batch(record)]), adopt_db)
    assert report.user_deleted == ["evt_1"]
    row = h.records.get("gcal", "evt_1")
    assert row is not None and row.item_id is None  # never repointed


def test_adopt_refuses_when_cursor_already_exists(adopt_db):
    h = Harness(adopt_db)
    h.state.record_success(
        "gcal", cursor="sync-token-1", at=datetime(2026, 10, 23, 12, 0, tzinfo=UTC)
    )
    record = make_record(external_id="evt_1")
    report = adopt_connector(FakeConnector(name="gcal", batches=[_batch(record)]), adopt_db)
    assert report.refused_cursor is True
    assert report.adopted == []
    assert h.records.get("gcal", "evt_1") is None


def test_adopt_gdrive_gtasks_report_nothing_to_adopt_and_never_fetch(adopt_db):
    # a fetch here would return an empty batch — the guard must fire FIRST
    connector = FakeConnector(name="gdrive")
    report = adopt_connector(connector, adopt_db)
    assert report.nothing_to_adopt is True
    assert connector.cursors == []  # NO Google fetch at all


# --- email rules ----------------------------------------------------------------


def test_adopt_email_matches_by_rfc_message_id_first(adopt_db):
    h = Harness(adopt_db)
    h.seed_legacy(
        source="email",
        raw_text=(
            "---------- Forwarded message ---------\nFrom: X <x@y.z>\n\n"
            "שלום, מחר יש אימון. בואו בזמן."
        ),
        occurred_at=datetime(2026, 10, 20, 8, 47, tzinfo=UTC),
        meta={
            "subject": "עדכונים",
            "from": "X <x@y.z>",
            "date_iso": "2026-10-20T08:47:00+00:00",
            "rfc_message_id": "<abc@mail.gmail.com>",
        },
    )
    # record text DIFFERS from the item text; only the rfc id links them
    record = make_record(
        external_id="msg_1",
        version='"v1"',
        title="עדכונים",
        text="עדכונים\n\nשלום, מחר יש אימון. בואו בזמן.",
        occurred_at=datetime(2026, 10, 20, 8, 47, tzinfo=UTC),
        meta={"rfc_message_id": "<abc@mail.gmail.com>", "subject": "עדכונים", "from": "X <x@y.z>"},
    )
    connector = FakeConnector(name="email", batches=[_batch(record)])
    report = adopt_connector(connector, adopt_db)
    assert report.adopted and report.adopted[0][1] is not None
    assert report.adopted[0][0][0] == "msg_1"
    assert report.rules["msg_1"] == "A"


def test_adopt_email_falls_back_to_text_plus_minute(adopt_db):
    h = Harness(adopt_db)
    text = "עדכונים\n\nשלום, מחר יש אימון. בואו בזמן."
    h.seed_legacy(
        source="email",
        raw_text=text,
        occurred_at=datetime(2026, 10, 20, 8, 47, tzinfo=UTC),
    )
    record = make_record(
        external_id="msg_1",
        version='"v1"',
        text=text,
        occurred_at=datetime(2026, 10, 20, 8, 47, tzinfo=UTC),
    )
    report = adopt_connector(FakeConnector(name="email", batches=[_batch(record)]), adopt_db)
    assert report.adopted and report.rules == {"msg_1": "B"}


def test_adopt_rule_c_matches_subject_sender_and_time(adopt_db):
    h = Harness(adopt_db)
    h.seed_legacy(
        source="email",
        raw_text=(
            "---------- Forwarded message ---------\n"
            "מאת: בית הספר בראשית <bereshit.meshalev@edu-haifa.org.il>\n"
            'נושא: תשלומי הורים תשפ"ז\nתאריך: 2026-10-22 12:43\n\nגוף ההודעה'
        ),
        occurred_at=datetime(2026, 10, 22, 10, 43, tzinfo=UTC),
        meta={
            "subject": 'תשלומי הורים תשפ"ז',
            "from": "בית הספר בראשית <bereshit.meshalev@edu-haifa.org.il>",
            "date_iso": "2026-10-22T10:43:00+00:00",
        },
    )
    # Gmail record: own render (text differs from the forward), same headers
    record = make_record(
        external_id="msg_9",
        version='"v1"',
        title='תשלומי הורים תשפ"ז',
        text='תשלומי הורים תשפ"ז\n\nמ: בית הספר בראשית\n\nגוף ההודעה',
        occurred_at=datetime(2026, 10, 22, 10, 43, tzinfo=UTC),
        meta={
            "subject": 'תשלומי הורים תשפ"ז',
            "from": "בית הספר בראשית <bereshit.meshalev@edu-haifa.org.il>",
        },
    )
    report = adopt_connector(FakeConnector(name="email", batches=[_batch(record)]), adopt_db)
    assert report.adopted and report.rules == {"msg_9": "C"}


def test_adopt_rule_c_two_minute_boundary(adopt_db):
    h = Harness(adopt_db)
    meta = {
        "subject": "תשלומי הורים",
        "from": "בית הספר בראשית <bereshit.meshalev@edu-haifa.org.il>",
        "date_iso": "2026-10-22T10:43:00+00:00",
    }
    rec_meta = {"subject": "תשלומי הורים", "from": meta["from"]}
    h.seed_legacy(
        source="email",
        raw_text="---------- Forwarded message ---------",
        occurred_at=datetime(2026, 10, 22, 10, 43, tzinfo=UTC),
        meta=dict(meta),
    )
    at_two = make_record(
        external_id="msg_1",
        version='"v1"',
        title="תשלומי הורים",
        text="different render entirely",
        occurred_at=datetime(2026, 10, 22, 10, 45, tzinfo=UTC),  # exactly +2 min
        meta=dict(rec_meta),
    )
    at_three = make_record(
        external_id="msg_2",
        version='"v1"',
        title="תשלומי הורים",
        text="different render entirely",
        occurred_at=datetime(2026, 10, 22, 10, 46, tzinfo=UTC),  # +3 min: outside
        meta=dict(rec_meta),
    )
    first = adopt_connector(FakeConnector(name="email", batches=[_batch(at_two)]), adopt_db)
    assert first.adopted and first.rules == {"msg_1": "C"}
    assert RULE_C_WINDOW == timedelta(minutes=2)

    # outside the window nothing matches: rule C misses, rule B misses -> new
    second = adopt_connector(FakeConnector(name="email", batches=[_batch(at_three)]), adopt_db)
    assert second.adopted == []
    assert second.new == ["msg_2"]


def test_adopt_rule_c_ambiguous_two_candidates(adopt_db):
    h = Harness(adopt_db)
    rec_meta = {
        "subject": "תשלומי הורים",
        "from": "בית הספר בראשית <bereshit.meshalev@edu-haifa.org.il>",
    }
    item_meta = {
        "subject": "תשלומי הורים",
        "from": rec_meta["from"],
        "date_iso": "2026-10-22T10:43:00+00:00",
    }
    h.seed_legacy(
        source="email",
        raw_text="גוף אחד",
        occurred_at=datetime(2026, 10, 22, 10, 43, tzinfo=UTC),
        meta=dict(item_meta),
    )
    h.seed_legacy(
        source="email",
        raw_text="גוף שונה לגמרי",
        occurred_at=datetime(2026, 10, 22, 10, 44, tzinfo=UTC),  # also within ±2 min
        meta=dict(item_meta),
    )
    record = make_record(
        external_id="msg_1",
        version='"v1"',
        text="תשלומי הורים render של gmail",
        occurred_at=datetime(2026, 10, 22, 10, 44, 30, tzinfo=UTC),
        meta=dict(rec_meta),
    )
    report = adopt_connector(FakeConnector(name="email", batches=[_batch(record)]), adopt_db)
    assert report.adopted == []
    assert report.ambiguous == ["msg_1"]


# --- undo -----------------------------------------------------------------------


def test_adopt_undo_restores_row_counts_and_refuses_touched_rows(adopt_db):
    h = Harness(adopt_db)
    h.seed_legacy(
        source="calendar",
        raw_text="אימון כדורסל — Thu 29 Oct 17:00–18:30",
        occurred_at=datetime(2026, 10, 29, 14, 30, tzinfo=UTC),
    )
    record = make_record(
        external_id="evt_1",
        version='"etag-1"',
        text="אימון כדורסל — Thu 29 Oct 17:00–18:30",
        occurred_at=datetime(2026, 10, 29, 14, 30, tzinfo=UTC),
    )
    adopt_connector(FakeConnector(name="gcal", batches=[_batch(record)]), adopt_db)
    assert h.records.get("gcal", "evt_1") is not None

    report = undo_adopt("gcal", adopt_db)
    assert report.removed == ["evt_1"]
    assert report.refused == []
    assert h.records.get("gcal", "evt_1") is None

    # a second undo has nothing to undo
    empty = undo_adopt("gcal", adopt_db)
    assert empty.removed == []


def test_adopt_undo_refuses_rows_a_sync_touched(adopt_db):
    h = Harness(adopt_db)
    h.seed_legacy(
        source="calendar",
        raw_text="אימון כדורסל — Thu 29 Oct 17:00–18:30",
        occurred_at=datetime(2026, 10, 29, 14, 30, tzinfo=UTC),
    )
    at_adopt = make_record(
        external_id="evt_1",
        version='"etag-1"',
        text="אימון כדורסל — Thu 29 Oct 17:00–18:30",
        occurred_at=datetime(2026, 10, 29, 14, 30, tzinfo=UTC),
    )
    adopt_connector(FakeConnector(name="gcal", batches=[_batch(at_adopt)]), adopt_db)
    # a sync ran and bumped the version (the row is now load-bearing)
    moved = make_record(
        external_id="evt_1",
        version='"etag-2"',
        text="אימון כדורסל — Fri 30 Oct 10:00–11:00",
        occurred_at=datetime(2026, 10, 30, 7, 0, tzinfo=UTC),
    )
    summary = h.runner(FakeConnector(name="gcal", batches=[_batch(moved)])).run()
    assert summary.updated == 1

    report = undo_adopt("gcal", adopt_db)
    assert report.removed == []
    assert report.refused == ["evt_1"]
    assert h.records.get("gcal", "evt_1") is not None


# --- CLI wiring -----------------------------------------------------------------


def _stub_registry(monkeypatch: pytest.MonkeyPatch, name: str, cls: type) -> None:
    """Hermetic registry entry: setitem (auto-restored) + no-op
    ensure_registered. The CLI tests never import the real Google adapters —
    a module-level register here would beat the REAL adapter's @register in
    CI's alphabetical collection order and crash test_gcal_connector."""
    import brain.connectors as registry

    monkeypatch.setattr(registry, "ensure_registered", lambda: None)
    monkeypatch.setitem(registry._REGISTRY, name, cls)


class _StubGcal(FakeConnector):
    name = "gcal"


@pytest.fixture(name="adopt_env")
def adopt_env_fixture(monkeypatch: pytest.MonkeyPatch):
    """Hermetic settings: gcal enabled, no credentials path."""
    monkeypatch.setenv("BRAIN_CONNECTORS", "gcal")
    monkeypatch.setenv("GMAIL_CREDENTIALS_PATH", "")
    from brain.config import get_settings

    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def test_cli_adopt_dry_run_prints_report_and_writes_nothing(
    adopt_db, adopt_env, capsys, monkeypatch
):
    import brain.cli as cli

    h = Harness(adopt_db)
    h.seed_legacy(
        source="calendar",
        raw_text="אימון כדורסל — Thu 29 Oct 17:00–18:30",
        occurred_at=datetime(2026, 10, 29, 14, 30, tzinfo=UTC),
    )
    record = make_record(
        external_id="evt_1",
        version='"etag-1"',
        text="אימון כדורסל — Thu 29 Oct 17:00–18:30",
        occurred_at=datetime(2026, 10, 29, 14, 30, tzinfo=UTC),
    )
    connector = FakeConnector(name="gcal", batches=[_batch(record)])
    monkeypatch.setattr(cli, "_build_connector", lambda name: connector)
    _stub_registry(monkeypatch, "gcal", _StubGcal)

    assert cli.main(["connectors", "adopt", "gcal", "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "WOULD ADOPT (1)" in out
    assert "evt_1" in out
    assert "[B]" in out  # the matching rule is reported
    assert "nothing written" not in out  # the report says would-adopt, no writes
    assert h.records.get("gcal", "evt_1") is None  # dry-run wrote NOTHING


def test_cli_adopt_real_run_writes_rows_and_undo_removes_them(
    adopt_db, adopt_env, capsys, monkeypatch
):
    import brain.cli as cli

    h = Harness(adopt_db)
    h.seed_legacy(
        source="calendar",
        raw_text="אימון כדורסל — Thu 29 Oct 17:00–18:30",
        occurred_at=datetime(2026, 10, 29, 14, 30, tzinfo=UTC),
    )
    record = make_record(
        external_id="evt_1",
        version='"etag-1"',
        text="אימון כדורסל — Thu 29 Oct 17:00–18:30",
        occurred_at=datetime(2026, 10, 29, 14, 30, tzinfo=UTC),
    )
    connector = FakeConnector(name="gcal", batches=[_batch(record)])
    monkeypatch.setattr(cli, "_build_connector", lambda name: connector)
    _stub_registry(monkeypatch, "gcal", _StubGcal)

    assert cli.main(["connectors", "adopt", "gcal"]) == 0
    out = capsys.readouterr().out
    assert "ADOPTED (1)" in out
    assert "Undo: brain connectors adopt gcal --undo" in out
    assert h.records.get("gcal", "evt_1") is not None

    assert cli.main(["connectors", "adopt", "gcal", "--undo"]) == 0
    assert h.records.get("gcal", "evt_1") is None


def test_cli_adopt_refuses_when_not_enabled(adopt_db, capsys, monkeypatch):
    import brain.cli as cli

    monkeypatch.setenv("BRAIN_CONNECTORS", "gtasks")
    from brain.config import get_settings

    get_settings.cache_clear()
    try:
        assert cli.main(["connectors", "adopt", "gcal"]) == 78
    finally:
        get_settings.cache_clear()
    assert "not enabled" in capsys.readouterr().err


def test_cli_adopt_refuses_when_cursor_exists(adopt_db, adopt_env, capsys, monkeypatch):
    import brain.cli as cli

    h = Harness(adopt_db)
    h.state.record_success(
        "gcal", cursor="sync-token-1", at=datetime(2026, 10, 23, 12, 0, tzinfo=UTC)
    )
    connector = FakeConnector(name="gcal")
    monkeypatch.setattr(cli, "_build_connector", lambda name: connector)
    _stub_registry(monkeypatch, "gcal", _StubGcal)

    assert cli.main(["connectors", "adopt", "gcal"]) == 78
    out = capsys.readouterr().out
    assert "REFUSED" in out and "already holds a cursor" in out
    assert connector.cursors == []  # refused BEFORE any fetch


def test_cli_adopt_gdrive_nothing_to_adopt_never_fetches(adopt_db, capsys, monkeypatch):
    import brain.cli as cli

    monkeypatch.setenv("BRAIN_CONNECTORS", "gdrive")
    from brain.config import get_settings

    get_settings.cache_clear()

    class _StubGdrive(FakeConnector):
        name = "gdrive"

    # setitem: auto-restored, no global-registry pollution; the no-op
    # ensure_registered keeps the real Google adapters out of this test
    _stub_registry(monkeypatch, "gdrive", _StubGdrive)
    connector = _StubGdrive()
    monkeypatch.setattr(cli, "_build_connector", lambda name: connector)
    try:
        assert cli.main(["connectors", "adopt", "gdrive"]) == 0
    finally:
        get_settings.cache_clear()
    out = capsys.readouterr().out
    assert "no legacy gdrive items" in out
    assert connector.cursors == []  # never fetched


# --- mixed report sections -------------------------------------------------------


def test_adopt_mixed_report_sections(adopt_db):
    h = Harness(adopt_db)
    matched = h.seed_legacy(
        source="calendar",
        raw_text="one — Thu 29 Oct 17:00–18:30",
        occurred_at=datetime(2026, 10, 29, 14, 30, tzinfo=UTC),
    )
    records = [
        make_record(
            external_id="hit",
            version='"v1"',
            text="one — Thu 29 Oct 17:00–18:30",
            occurred_at=datetime(2026, 10, 29, 14, 30, tzinfo=UTC),
        ),
        make_record(
            external_id="miss",
            version='"v1"',
            text="totally unknown — Fri 30 Oct 10:00",
            occurred_at=datetime(2026, 10, 30, 7, 0, tzinfo=UTC),
        ),
        make_record(
            external_id="cancelled_match",
            version='"v1"',
            text="one — Thu 29 Oct 17:00–18:30",
            occurred_at=datetime(2026, 10, 29, 14, 30, tzinfo=UTC),
            deleted=True,
        ),
    ]
    report = adopt_connector(FakeConnector(name="gcal", batches=[_batch(*records)]), adopt_db)
    assert report.adopted and len(report.adopted) == 2
    ids = {item_id for _, item_id in report.adopted}
    assert ids == {matched.id}
    assert report.new == ["miss"]


# --- review round 1: atomicity + undo-the-stamp ---------------------------------


def test_adopt_crash_leaves_no_partial_writes(adopt_db, monkeypatch):
    """A crash between the ledger puts and the ops event must leave NOTHING:
    no ledger rows, no stamps, no event — one transaction, rolled back whole
    (undo reads only the last adopt event; orphaned rows would be invisible)."""
    import brain.connectors.adopt as adopt_mod
    from brain.db.session import engine

    h = Harness(adopt_db)
    h.seed_legacy(
        source="calendar",
        raw_text="אימון כדורסל — Thu 29 Oct 17:00–18:30",
        occurred_at=datetime(2026, 10, 29, 14, 30, tzinfo=UTC),
    )
    record = make_record(
        external_id="evt_1",
        version='"etag-1"',
        text="אימון כדורסל — Thu 29 Oct 17:00–18:30",
        occurred_at=datetime(2026, 10, 29, 14, 30, tzinfo=UTC),
    )

    def boom(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("ops event write failed")

    monkeypatch.setattr(adopt_mod, "_log_adopt", boom)
    with pytest.raises(RuntimeError):
        adopt_connector(FakeConnector(name="gcal", batches=[_batch(record)]), adopt_db)

    with engine.begin() as conn:
        assert conn.execute(text("SELECT count(*) FROM connector_records")).scalar() == 0
        assert conn.execute(text("SELECT count(*) FROM ops_events")).scalar() == 0
        assert (
            conn.execute(text("SELECT meta->>'connector_text_sha' FROM memory_items")).scalar()
            is None
        )


def test_adopt_undo_restores_items_byte_identical(adopt_db, adopt_env, monkeypatch):
    """Undo removes the connector_text_sha stamp it wrote: the adopted item's
    meta after undo equals its meta before adopt (byte-identical)."""
    import brain.cli as cli

    h = Harness(adopt_db)
    legacy = h.seed_legacy(
        source="calendar",
        raw_text="אימון כדורסל — Thu 29 Oct 17:00–18:30",
        occurred_at=datetime(2026, 10, 29, 14, 30, tzinfo=UTC),
    )
    before_meta = dict(legacy.meta)
    record = make_record(
        external_id="evt_1",
        version='"etag-1"',
        text="אימון כדורסל — Thu 29 Oct 17:00–18:30",
        occurred_at=datetime(2026, 10, 29, 14, 30, tzinfo=UTC),
    )
    connector = FakeConnector(name="gcal", batches=[_batch(record)])
    monkeypatch.setattr(cli, "_build_connector", lambda name: connector)
    _stub_registry(monkeypatch, "gcal", _StubGcal)

    assert cli.main(["connectors", "adopt", "gcal"]) == 0
    adopt_db.expire_all()  # the CLI committed in its own session
    stamped = h.repo.get(legacy.id)
    assert stamped is not None
    assert stamped.meta.get("connector_text_sha") is not None

    assert cli.main(["connectors", "adopt", "gcal", "--undo"]) == 0
    adopt_db.expire_all()
    restored = h.repo.get(legacy.id)
    assert restored is not None
    assert restored.meta == before_meta  # stamp GONE after undo


def test_adopt_undo_keeps_stamp_on_refused_rows(adopt_db):
    """A refused row (sync touched it) keeps its stamp: undo only reverts what
    it actually removed."""
    h = Harness(adopt_db)
    h.seed_legacy(
        source="calendar",
        raw_text="אימון כדורסל — Thu 29 Oct 17:00–18:30",
        occurred_at=datetime(2026, 10, 29, 14, 30, tzinfo=UTC),
    )
    at_adopt = make_record(
        external_id="evt_1",
        version='"etag-1"',
        text="אימון כדורסל — Thu 29 Oct 17:00–18:30",
        occurred_at=datetime(2026, 10, 29, 14, 30, tzinfo=UTC),
    )
    adopt_connector(FakeConnector(name="gcal", batches=[_batch(at_adopt)]), adopt_db)
    moved = make_record(
        external_id="evt_1",
        version='"etag-2"',
        text="אימון כדורסל — Fri 30 Oct 10:00–11:00",
        occurred_at=datetime(2026, 10, 30, 7, 0, tzinfo=UTC),
    )
    summary = h.runner(FakeConnector(name="gcal", batches=[_batch(moved)])).run()
    assert summary.updated == 1

    report = undo_adopt("gcal", adopt_db)
    assert report.refused == ["evt_1"]
    kept = h.repo.get(h.records.get("gcal", "evt_1").item_id)
    assert kept is not None
    assert "connector_text_sha" in (kept.meta or {})  # untouched


# --- _fetch_all + FetchResult.more (finding 4) ----------------------------------


def test_fetch_all_drains_while_more_even_when_the_cursor_does_not_move():
    """The email query path cannot express a mid-listing resume cursor: a
    truncated batch carries next_cursor=None AND more=True. _fetch_all must
    keep fetching while more is set (the 201st record must be counted),
    instead of silently stopping after the first truncated batch."""
    from brain.connectors.adopt import _fetch_all
    from brain.connectors.testing import make_record

    connector = FakeConnector(
        name="email",
        batches=[
            FetchResult(
                records=[make_record(f"r{i}", text=f"body {i}") for i in range(200)],
                next_cursor=None,
                more=True,
            ),
            FetchResult(records=[make_record("r200", text="body 200")], next_cursor=None),
        ],
    )
    records = _fetch_all(connector, limit=200)

    assert len(records) == 201
    assert connector.cursors == [None, None]  # the cursor never moved; still drained
