"""ConnectorRunner semantics (connectors ticket, C1 acceptance).

Real IngestService + real repositories on the brain_w8_test database; the
connector itself, the indexer, the enrichment queue and the attachments core
are recording fakes. No network anywhere.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from qdrant_client import QdrantClient
from sqlalchemy import text
from tests.fakes import StubClassifier

from brain import schemas
from brain.ai.classify import FALLBACK_CLASSIFICATION
from brain.ai.embedder import FakeEmbedder
from brain.ai.vectorstore import QdrantVectorStore
from brain.connectors.base import (
    AttachmentRef,
    ConnectorAuthError,
    ConnectorRetryableError,
    FetchResult,
)
from brain.connectors.runner import ConnectorRunner, backoff_seconds
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


@pytest.fixture(name="runner_db")
def runner_db_fixture(database: None):
    """Fresh connector_state/connector_records; memory_items cleaned after."""
    from brain.db.session import SessionLocal, engine

    with engine.begin() as conn:
        for table in ("connector_records", "connector_state"):
            conn.execute(text(f"DELETE FROM {table}"))
    session = SessionLocal()
    yield session
    session.rollback()
    session.close()
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM connector_records"))
        conn.execute(text("DELETE FROM connector_state"))
        conn.execute(text("DELETE FROM memory_items"))


class Harness:
    """Everything one runner test needs, wired the production way."""

    def __init__(self, session: Any) -> None:
        self.session = session
        self.repo = ItemsRepository(session)
        self.classifier = StubClassifier(dict(FALLBACK_CLASSIFICATION))
        self.indexer = RecordingIndexer()
        self.enrichment = RecordingEnrichment()
        self.attachments = RecordingAttachments()
        # IngestService with an in-memory vector store — same shape as
        # tests/test_ingest_service.py; embedder/classifier never see network.
        self.ingest = IngestService(
            self.repo,
            self.classifier,
            _InMemoryIndexer(),
            enrichment=self.enrichment,
        )
        self.state = ConnectorStateRepo(session)
        self.records = ConnectorRecordRepo(session)
        # Movable clock: tests tick PAST the persisted backoff window to
        # simulate the next scheduler pass (an immediate retry of a FAILED
        # connector is deliberately skipped now — that is the backoff
        # contract). Successful runs reset the counter and need no tick.
        self.clock_box = {"now": datetime(2026, 10, 23, 12, 0, tzinfo=UTC)}

    def tick(self, seconds: float) -> None:
        """Move the harness clock forward (past a backoff window)."""
        self.clock_box["now"] += timedelta(seconds=seconds)

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
            clock=lambda: self.clock_box["now"],
        )

    def item(self, external_id: str) -> MemoryItem | None:
        row = self.records.get("gcal", external_id)
        if row is None or row.item_id is None:
            return None  # unknown record, or a poison/tombstone ledger row
        return self.repo.get(row.item_id)


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


def _batch(*records: Any, cursor: str | None = "c1", reset: bool = False) -> FetchResult:
    return FetchResult(records=list(records), next_cursor=cursor, reset=reset)


# --- new / unchanged / changed / deleted -------------------------------------


def test_new_record_creates_item_with_framework_meta(runner_db):
    h = Harness(runner_db)
    summary = h.runner(FakeConnector(batches=[_batch(make_record(url="http://x/1"))])).run()

    assert summary.created == 1
    item = h.item("r1")
    assert item is not None
    assert item.source == "gcal"
    assert item.raw_text == "body of r1"
    assert item.occurred_at == datetime(2026, 10, 1, 9, 0, tzinfo=UTC)
    assert item.meta["connector"] == "gcal"
    assert item.meta["external_id"] == "r1"
    assert item.meta["version"] == "v1"
    assert item.meta["url"] == "http://x/1"


def test_same_version_rerun_is_idempotent(runner_db):
    h = Harness(runner_db)
    connector = FakeConnector(batches=[_batch(make_record()), _batch(make_record())])
    runner = h.runner(connector)
    first = runner.run()
    second = runner.run()

    assert first.created == 1
    assert (second.created, second.updated, second.deleted) == (0, 0, 0)
    assert second.skipped == 1
    items = h.repo.list(item_type=None, project=None, status=None, limit=50, offset=0)
    assert len(items) == 1


def test_null_version_stored_and_incoming_is_unchanged(runner_db):
    """A stored NULL version and an incoming None count as EQUAL (skip).

    Sources with no natural version (e.g. bare text snippets) stay idempotent
    instead of being re-written on every sync. Only a DIFFERING pair is a
    change. (Manager decision, connectors/base thread.)
    """
    h = Harness(runner_db)
    connector = FakeConnector(
        batches=[_batch(make_record(version=None)), _batch(make_record(version=None))]
    )
    runner = h.runner(connector)
    first = runner.run()
    second = runner.run()

    assert first.created == 1
    assert (second.created, second.updated, second.deleted) == (0, 0, 0)
    assert second.skipped == 1


def test_user_edited_item_keeps_its_text_on_version_bump(runner_db):
    """The user edited the item (text != the framework's last-written text):
    a source-side version bump never overwrites their edit (verification a)."""
    h = Harness(runner_db)
    connector = FakeConnector(
        batches=[
            _batch(make_record()),
            _batch(make_record(version="v2", text="SOURCE says something else now")),
        ]
    )
    runner = h.runner(connector)
    runner.run()
    item = h.item("r1")
    assert item is not None
    item.raw_text = "USER edited this"  # e.g. PATCH /items or a bot edit
    h.repo.commit()

    summary = runner.run()

    assert summary.updated == 0
    item = h.item("r1")
    assert item is not None
    assert item.raw_text == "USER edited this"  # the user's text survives
    assert item.meta["version"] == "v2"  # only the marker moved


def test_user_deleted_item_is_never_recaptured(runner_db):
    """Deleting the item is a user decision: the ledger row stays (item_id
    NULL via ON DELETE SET NULL) and the runner never re-captures it."""
    h = Harness(runner_db)
    connector = FakeConnector(batches=[_batch(make_record()), _batch(make_record())])
    runner = h.runner(connector)
    runner.run()
    item = h.item("r1")
    assert item is not None
    h.repo.delete(item)
    h.repo.commit()

    summary = runner.run()

    assert summary.created == 0  # NOT re-captured
    assert summary.skipped == 1
    row = h.records.get("gcal", "r1")
    assert row is not None and row.item_id is None  # tombstone remains
    assert h.repo.list(item_type=None, project=None, status=None, limit=10, offset=0) == []


def test_deletion_touches_only_the_marks_not_the_content(runner_db):
    """Review bug 1: a deleted record's payload carries no content (Drive's
    deleted record has text=''; a cancelled event only id+status). The
    deletion must change ONLY the marks + ledger version — never wipe the
    text, re-index empty content, re-enqueue, or touch occurred_at."""
    h = Harness(runner_db)
    connector = FakeConnector(
        batches=[
            _batch(make_record()),
            # the delete payload: same id, NO useful content
            _batch(make_record(version="v2", text="", occurred_at=None, deleted=True)),
        ]
    )
    runner = h.runner(connector)
    runner.run()
    item = h.item("r1")
    assert item is not None
    text_before = item.raw_text
    when_before = item.occurred_at
    enqueued_before = len(h.enrichment.enqueued)
    indexed_before = len(h.indexer.indexed)

    summary = runner.run()

    assert summary.deleted == 1
    item = h.item("r1")
    assert item is not None
    assert item.raw_text == text_before  # content NOT wiped
    assert item.occurred_at == when_before
    assert item.meta["source_deleted"] is True
    assert len(h.enrichment.enqueued) == enqueued_before  # nothing re-enqueued
    assert len(h.indexer.indexed) == indexed_before  # nothing re-indexed


def test_user_edited_item_is_still_hidden_when_source_deletes(runner_db):
    """Review bug 2: deletion applies regardless of who owns the text."""
    h = Harness(runner_db)
    connector = FakeConnector(batches=[_batch(make_record()), _batch(make_record(deleted=True))])
    runner = h.runner(connector)
    runner.run()
    item = h.item("r1")
    assert item is not None
    item.raw_text = "USER edited this"
    h.repo.commit()

    summary = runner.run()

    assert summary.deleted == 1
    item = h.item("r1")
    assert item is not None
    assert item.meta["source_deleted"] is True
    assert item.superseded_by == item.id
    assert h.repo.list(item_type=None, project=None, status=None, limit=10, offset=0) == []


def test_relation_strike_survives_source_delete_and_undelete(runner_db):
    """Review bug 3: a REAL PR-B strike (superseded_by = a newer item) must
    survive both the source delete and the un-delete — the runner only ever
    sets/clears its OWN self-strike."""
    h = Harness(runner_db)
    connector = FakeConnector(
        batches=[
            _batch(make_record()),
            _batch(make_record(version="v2", deleted=True)),
            _batch(make_record(version="v3", text="body of r1, back")),
        ]
    )
    runner = h.runner(connector)
    runner.run()
    item = h.item("r1")
    assert item is not None
    newer = MemoryItem(raw_text="newer version of the fact", source="manual")
    h.repo.add(newer)
    h.repo.commit()
    item.superseded_by = newer.id  # a real supersedes relation struck this item
    h.repo.commit()

    runner.run()  # delete
    item = h.item("r1")
    assert item is not None
    assert item.meta["source_deleted"] is True
    assert item.superseded_by == newer.id  # NOT overwritten with self

    runner.run()  # un-delete (v3, not deleted)
    item = h.item("r1")
    assert item is not None
    assert item.meta.get("source_deleted") is None
    assert item.superseded_by == newer.id  # relation's strike still intact


def _link_child(h: Harness, parent: MemoryItem, title: str, state: str) -> MemoryItem:
    """Create an extraction child (state) quoting the parent via item_sources."""
    from brain.db.models import ItemSource

    child = MemoryItem(
        raw_text=title,
        source=parent.source,
        child_state=state,
        extraction_meta={"run_id": "r-test"},
    )
    h.repo.add(child)
    h.repo.commit()
    h.session.add(ItemSource(child_id=child.id, source_id=parent.id, quote="q"))
    h.session.commit()
    return child


def test_children_of_a_deleted_parent_hidden_or_flagged(runner_db):
    """Review bug 4: a trashed source must not leave its proposed children
    askable. Proposed children hide with the same mechanism; kept children
    (the user acted) stay visible but flagged; un-delete reverses exactly."""
    h = Harness(runner_db)
    connector = FakeConnector(
        batches=[
            _batch(make_record()),
            _batch(make_record(version="v2", deleted=True)),
            _batch(make_record(version="v3", text="body of r1, back")),
        ]
    )
    runner = h.runner(connector)
    runner.run()
    parent = h.item("r1")
    assert parent is not None
    proposed = _link_child(h, parent, "Dentist Tuesday", "proposed")
    kept = _link_child(h, parent, "Dentist (confirmed)", "kept")

    runner.run()  # delete
    proposed = h.repo.get(proposed.id)
    kept = h.repo.get(kept.id)
    assert proposed is not None and proposed.superseded_by == proposed.id
    assert kept is not None and kept.superseded_by is None  # user acted: visible
    assert kept.meta.get("parent_source_deleted") is True

    runner.run()  # un-delete
    proposed = h.repo.get(proposed.id)
    kept = h.repo.get(kept.id)
    assert proposed is not None and proposed.superseded_by is None
    assert kept is not None and kept.meta.get("parent_source_deleted") is None


def test_connector_text_sha_detects_user_edits_without_duplicating_text(runner_db):
    """Review finding 5: the framework stores a sha256 of its last-written
    text (NOT the full text) in meta, and the update path compares hashes."""
    import hashlib

    h = Harness(runner_db)
    connector = FakeConnector(batches=[_batch(make_record())])
    h.runner(connector).run()
    item = h.item("r1")
    assert item is not None
    assert item.meta["connector_text_sha"] == hashlib.sha256(b"body of r1").hexdigest()
    assert "connector_text" not in item.meta  # no full-text duplicate in JSONB


def test_long_text_record_is_clamped_not_fatal(runner_db):
    """Review round 2 A: ItemCreate rejects raw_text > RAW_TEXT_MAX_LENGTH
    (10_000), so a 12k-char record would fail ingest every run and hold the
    cursor forever. The runner clamps in ONE place and flags it in meta."""
    import hashlib

    from brain import schemas

    h = Harness(runner_db)
    connector = FakeConnector(
        batches=[_batch(make_record(text="x" * 12_345))],
    )
    h.runner(connector).run()
    item = h.item("r1")
    assert item is not None  # created, NOT failed
    assert len(item.raw_text) == schemas.RAW_TEXT_MAX_LENGTH
    assert item.meta["text_truncated"] is True
    assert item.meta["original_length"] == 12_345
    # the marker is the sha of the CLAMPED text, or every long item would
    # look user-edited forever
    assert (
        item.meta["connector_text_sha"] == hashlib.sha256(item.raw_text.encode("utf-8")).hexdigest()
    )


def test_long_text_update_clamps_too(runner_db):
    h = Harness(runner_db)
    connector = FakeConnector(
        batches=[
            _batch(make_record()),
            _batch(make_record(version="v2", text="y" * 11_111)),
        ]
    )
    h.runner(connector).run()
    item = h.item("r1")
    assert item is not None

    summary = h.runner(connector).run()  # second batch: the long update

    assert summary.updated == 1
    item = h.item("r1")
    assert item is not None
    assert len(item.raw_text) == 10_000
    assert item.meta["text_truncated"] is True
    assert item.meta["original_length"] == 11_111


def test_poison_record_quarantines_and_releases_cursor(runner_db):
    """Review round 2 A part 2: a record whose create always raises must not
    wedge the source. Failures 1-2 hold the cursor; on the 3rd the record is
    quarantined in the ledger (status failed + last error), the batch counts
    it as skipped and the cursor advances. Later runs skip it unattempted."""
    from sqlalchemy import text as sql_text

    from brain.db.session import SessionLocal

    h = Harness(runner_db)
    real_create = h.ingest.create

    def flaky_create(payload: Any) -> Any:
        if payload.raw_text == "poison body":
            raise RuntimeError("simulated ingest failure")
        return real_create(payload)

    h.ingest.create = flaky_create
    poison_batch = [
        make_record(external_id="good", text="good one"),
        make_record(external_id="bad", text="poison body"),
    ]
    connector = FakeConnector(
        batches=[
            _batch(*poison_batch),
            _batch(*poison_batch),
            _batch(*poison_batch),
            _batch(*poison_batch),  # source still re-offers it after quarantine
        ]
    )
    runner = h.runner(connector)

    s1 = runner.run()
    assert s1.errors == 1 and s1.cursor_saved is False
    h.tick(backoff_seconds(1) + 1)
    s2 = runner.run()
    assert s2.errors == 1 and s2.cursor_saved is False

    h.tick(backoff_seconds(2) + 1)
    s3 = runner.run()  # 3rd failure: quarantine, not an error -> cursor moves
    assert s3.errors == 0
    assert s3.cursor_saved is True
    row = h.records.get("gcal", "bad")
    assert row is not None
    assert row.status == "failed"
    assert row.fail_count == 3
    assert row.last_error is not None and "simulated ingest failure" in row.last_error
    with SessionLocal() as session:
        quarantined_events = session.execute(
            sql_text(
                "SELECT count(*) FROM ops_events "
                "WHERE component = 'connector:gcal' AND kind = 'quarantine'"
            )
        ).scalar()
    assert quarantined_events and quarantined_events >= 1

    s4 = runner.run()  # quarantined: skipped unattempted, still no error
    assert s4.errors == 0
    assert s4.skipped >= 1
    assert h.item("bad") is None  # never captured


def test_db_poisoned_record_quarantines_without_aborting_run(runner_db):
    """Review round 3: a record that fails with a REAL DB error (Postgres
    rejects the NaN in meta during flush) poisons the shared session; the
    failure bookkeeping must roll back FIRST, or its own commit raises
    PendingRollbackError inside the except block and aborts run(). Earlier
    records of the same batch are already committed per record — the
    rollback cannot touch them (the good record is asserted below)."""
    h = Harness(runner_db)
    poison = make_record(external_id="dbbad", meta={"weird": float("nan")})
    good = make_record(external_id="good2", text="good two")
    connector = FakeConnector(
        batches=[_batch(good, poison), _batch(good, poison), _batch(good, poison)]
    )
    runner = h.runner(connector)

    s1 = runner.run()  # pre-fix this raised PendingRollbackError out of run()

    assert s1.errors == 1
    assert s1.cursor_saved is False
    assert h.item("good2") is not None  # earlier success survived the rollback

    h.tick(backoff_seconds(1) + 1)
    runner.run()  # failure 2
    h.tick(backoff_seconds(2) + 1)
    s3 = runner.run()  # failure 3 -> quarantined, cursor released

    assert s3.errors == 0
    assert s3.cursor_saved is True
    row = h.records.get("gcal", "dbbad")
    assert row is not None and row.status == "failed"


def test_quarantined_record_retries_on_version_bump(runner_db):
    """A later version bump gives a quarantined record another chance; a
    success resets the quarantine bookkeeping."""
    h = Harness(runner_db)
    real_create = h.ingest.create

    def flaky_create(payload: Any) -> Any:
        if payload.raw_text == "poison body":
            raise RuntimeError("simulated ingest failure")
        return real_create(payload)

    h.ingest.create = flaky_create
    poison = make_record(external_id="bad", text="poison body")
    connector = FakeConnector(
        batches=[
            _batch(poison),
            _batch(poison),
            _batch(poison),
            _batch(make_record(external_id="bad", version="v2", text="fixed body")),
        ]
    )
    runner = h.runner(connector)
    for i in range(3):
        if i:
            h.tick(backoff_seconds(i) + 1)
        runner.run()
    row = h.records.get("gcal", "bad")
    assert row is not None and row.status == "failed"

    h.tick(backoff_seconds(3) + 1)
    summary = runner.run()  # v2: retried and it works now

    assert summary.created == 1
    item = h.item("bad")
    assert item is not None and item.raw_text == "fixed body"
    row = h.records.get("gcal", "bad")
    assert row is not None
    assert row.status == "synced"
    assert row.fail_count == 0
    assert row.last_error is None


def test_unhide_without_version_bump_reverses_marks(runner_db):
    """Review round 2 C (pins the semantics): the skip condition is version
    equal AND deleted-state equal, so a source un-delete WITHOUT a version
    bump gets through and reverses the marks."""
    h = Harness(runner_db)
    connector = FakeConnector(
        batches=[
            _batch(make_record()),  # v1 created
            _batch(make_record(deleted=True)),  # v1 deleted — SAME version
            _batch(make_record()),  # v1 restored — SAME version
        ]
    )
    runner = h.runner(connector)
    runner.run()
    runner.run()
    assert (h.item("r1") is not None) and (h.item("r1").meta.get("source_deleted") is True)  # type: ignore[union-attr]

    runner.run()  # un-delete, no version bump

    item = h.item("r1")
    assert item is not None
    assert item.meta.get("source_deleted") is None
    assert item.superseded_by is None


def test_new_version_updates_item_in_place_and_reindexes(runner_db):
    h = Harness(runner_db)
    connector = FakeConnector(
        batches=[
            _batch(make_record()),
            _batch(make_record(version="v2", text="body of r1, updated")),
        ]
    )
    runner = h.runner(connector)
    runner.run()
    second = runner.run()

    assert second.updated == 1
    item = h.item("r1")
    assert item is not None
    assert h.repo.get(item.id) is not None
    assert item.raw_text == "body of r1, updated"
    assert item.meta["version"] == "v2"
    assert item.id in h.indexer.indexed  # re-indexed after the update
    assert item.id in h.enrichment.enqueued  # enrichment re-enqueued


def test_deleted_marks_and_hides_but_never_hard_deletes(runner_db):
    h = Harness(runner_db)
    connector = FakeConnector(batches=[_batch(make_record()), _batch(make_record(deleted=True))])
    runner = h.runner(connector)
    first = runner.run()
    item = h.item("r1")
    assert item is not None
    item_id = item.id
    second = runner.run()

    assert first.created == 1
    assert second.deleted == 1
    # NEVER hard-deleted: the row is still there ...
    assert h.repo.get(item_id) is not None
    item = h.repo.get(item_id)
    assert item.meta["source_deleted"] is True
    assert item.superseded_by == item.id  # hidden via the PR B mechanism
    # ... and hidden from the retrieval paths ask uses.
    assert h.repo.list(item_type=None, project=None, status=None, limit=10, offset=0) == []
    assert h.repo.search(query="body of r1", limit=10, offset=0) == []
    assert (
        h.repo.by_occurrence(
            start=datetime(2026, 10, 1, tzinfo=UTC),
            end=datetime(2026, 10, 2, tzinfo=UTC),
            limit=10,
        )
        == []
    )
    # PR B mechanism reused end-to-end: the vector store may still hold the
    # point, but SearchService._load_items (ask's semantic leg) skips it.
    from brain.services.search import SearchService

    search = SearchService(h.repo, _FailingEmbedder(), None, _PassThroughReranker())
    hits = search._load_items([{"id": str(item_id), "vector_score": 0.9}])
    assert hits == []


def test_undeleted_record_restores_the_item(runner_db):
    h = Harness(runner_db)
    connector = FakeConnector(
        batches=[
            _batch(make_record()),
            _batch(make_record(deleted=True)),
            _batch(make_record(version="v3", text="body of r1, back")),
        ]
    )
    runner = h.runner(connector)
    runner.run()
    runner.run()
    third = runner.run()

    assert third.updated == 1
    item = h.item("r1")
    assert item is not None
    assert item.meta.get("source_deleted") is None
    assert item.superseded_by is None
    assert item.raw_text == "body of r1, back"
    assert h.repo.list(item_type=None, project=None, status=None, limit=10, offset=0) != []


def test_deleted_record_for_unknown_id_creates_nothing(runner_db):
    h = Harness(runner_db)
    summary = h.runner(FakeConnector(batches=[_batch(make_record(deleted=True))])).run()

    assert summary.created == 0
    assert summary.skipped == 1
    assert h.repo.list(item_type=None, project=None, status=None, limit=10, offset=0) == []


def test_reset_batch_upserts_without_inferring_deletions(runner_db):
    h = Harness(runner_db)
    connector = FakeConnector(
        batches=[_batch(make_record()), _batch(make_record(), make_record(external_id="r2"))]
    )
    # Batch 2 arrives with reset=True: r1 is absent, which must NOT delete it.
    connector._batches[1] = FetchResult(
        records=[make_record(external_id="r2", text="body of r2")],
        next_cursor="c2",
        reset=True,
    )
    runner = h.runner(connector)
    runner.run()
    summary = runner.run()

    assert summary.reset is True
    assert summary.created == 1
    assert summary.deleted == 0
    r1 = h.item("r1")
    assert r1 is not None
    assert r1.meta.get("source_deleted") is None  # absence proved nothing
    assert h.state.get("gcal").cursor == "c2"


# --- cursor / failure semantics ----------------------------------------------


def test_cursor_advances_only_after_full_batch_success(runner_db):
    h = Harness(runner_db)
    # Record 3 of 5 fails: its attachment bytes are missing.
    records = [make_record(external_id=f"r{i}", text=f"body {i}") for i in range(1, 6)]
    records[2] = make_record(
        external_id="r3",
        text="body 3",
        attachments=(AttachmentRef(ref_id="missing", filename="f", mime_type="text/plain"),),
    )
    connector = FakeConnector(batches=[_batch(*records)], attachments={("r3", "present"): b"x"})
    runner = h.runner(connector)
    summary = runner.run()

    assert summary.errors == 1
    assert summary.cursor_saved is False
    assert h.state.get("gcal").cursor is None  # NOT advanced
    assert h.state.get("gcal").last_error is not None
    # Records 1-2 were still written (at-least-once): the retry skips them.
    assert h.item("r1") is not None
    assert h.item("r2") is not None
    # Fix the failure and re-run: nothing duplicates, cursor advances now.
    connector._batches.append(
        _batch(*[make_record(external_id=f"r{i}", text=f"body {i}") for i in range(1, 6)])
    )
    h.tick(backoff_seconds(1) + 1)
    retry = runner.run()
    # The loop continues past a failed record, so r4/r5 were already written
    # in the first run; the retry finds them UNCHANGED (no duplicates),
    # writes the one record that failed, and only now moves the cursor.
    # r3's ITEM already existed (run 1 created it, then its attachment
    # capture failed): the retry's create returns ingest's dedupe snapshot —
    # counted as deduped, not created (no second capture happened).
    assert retry.created == 0
    assert retry.deduped == 1  # r3, the one that failed last time
    assert retry.skipped == 4  # r1/r2/r4/r5: unchanged, NOT duplicated
    assert h.state.get("gcal").cursor == "c1"
    items = h.repo.list(item_type=None, project=None, status=None, limit=50, offset=0)
    assert len(items) == 5  # exactly one item per external id


def test_auth_error_stops_the_run_with_clear_state(runner_db):
    h = Harness(runner_db)
    connector = FakeConnector(
        errors=[ConnectorAuthError("refresh token rejected: invalid_grant")],
        batches=[_batch(make_record())],
    )
    runner = h.runner(connector)
    summary = runner.run()

    assert summary.state == "auth_error"
    assert "re-consent" in (summary.error or "") or "invalid_grant" in (summary.error or "")
    assert summary.cursor_saved is False
    assert connector.cursors == [None]  # one fetch attempt, then STOP
    assert h.item("r1") is None  # the queued batch never ran
    state = h.state.get("gcal")
    assert "auth" in (state.last_error or "")
    assert state.cursor is None


def test_backoff_window_skips_the_fetch_and_reports_retry_in_s(runner_db):
    """The persisted backoff is ENFORCED: after a retryable failure with
    attempt=1 (window 10s) an immediate re-run must NOT fetch — the runner
    returns state='backoff' with the seconds remaining, and the persisted
    state (last_run_at/attempt) is left untouched so the window keeps
    shrinking."""
    h = Harness(runner_db)
    connector = FakeConnector(
        errors=[ConnectorRetryableError("http 500")], batches=[_batch(make_record())]
    )
    runner = h.runner(connector)
    first = runner.run()
    assert first.state == "retry_error"
    first_state = h.state.get("gcal")

    h.tick(3)  # 3s into the 10s window
    second = runner.run()

    assert second.state == "backoff"
    assert second.retry_in_s is not None
    assert 0 < second.retry_in_s <= 7.0  # 10s window, 3s elapsed
    assert second.fetched == 0 and second.created == 0
    assert connector.cursors == [None]  # the second run NEVER fetched
    after = h.state.get("gcal")
    assert after.last_run_at == first_state.last_run_at  # untouched
    assert h.state.attempt("gcal") == 1  # not bumped by the skip


def test_backoff_window_expires_and_the_fetch_resumes(runner_db):
    """Past the window the connector is fetched again (the backoff only ever
    DELAYS, never disables)."""
    h = Harness(runner_db)
    connector = FakeConnector(
        errors=[ConnectorRetryableError("http 500")], batches=[_batch(make_record())]
    )
    runner = h.runner(connector)
    assert runner.run().state == "retry_error"

    h.tick(backoff_seconds(1) + 1)
    resumed = runner.run()

    assert resumed.state == "ok"
    assert resumed.retry_in_s is None
    assert connector.cursors == [None, None]  # fetched again past the window


def test_healthy_connector_is_never_backed_off(runner_db):
    """A connector with NO failure history (attempt=0, last_error None) is
    fetched on every run, even back-to-back with an unmoving clock."""
    h = Harness(runner_db)
    connector = FakeConnector(
        batches=[_batch(make_record()), _batch(make_record(external_id="r2"))]
    )
    runner = h.runner(connector)

    first = runner.run()
    second = runner.run()

    assert (first.state, second.state) == ("ok", "ok")
    assert connector.cursors == [None, "c1"]  # both fetched


def test_retryable_error_backs_off_and_is_capped(runner_db):
    h = Harness(runner_db)
    connector = FakeConnector(
        errors=[ConnectorRetryableError("http 429"), ConnectorRetryableError("http 500")],
        batches=[_batch(make_record())],
    )
    r = h.runner(connector)
    first = r.run()

    # Inside the window: the persisted backoff DELAYS the fetch entirely.
    second = r.run()
    assert (first.state, second.state) == ("retry_error", "backoff")
    assert first.cursor_saved is False
    assert connector.cursors == [None]  # run 2 never fetched
    state = h.state.get("gcal")
    assert state.last_error == "retryable: http 429"  # untouched by the skip
    assert h.state.attempt("gcal") == 1
    assert backoff_seconds(2) > backoff_seconds(1) > backoff_seconds(0)
    assert backoff_seconds(99) == backoff_seconds(98)  # capped

    # Past the window the retry actually runs (attempt 2).
    h.tick(backoff_seconds(1) + 1)
    third = r.run()
    assert third.state == "retry_error"
    assert h.state.attempt("gcal") == 2

    # A success (after its window) resets the backoff counter and saves the cursor.
    h.tick(backoff_seconds(2) + 1)
    fourth = r.run()
    assert fourth.state == "ok"
    assert h.state.attempt("gcal") == 0
    assert h.state.get("gcal").cursor == "c1"


def test_connector_close_is_not_the_runners_job(runner_db):
    h = Harness(runner_db)
    connector = FakeConnector(batches=[_batch(make_record())])
    h.runner(connector).run()
    assert connector.closed is False  # the caller (CLI) owns the lifecycle


# --- attachments --------------------------------------------------------------


def test_attachments_flow_through_the_attachments_pipeline(runner_db):
    h = Harness(runner_db)
    record = make_record(
        attachments=(AttachmentRef(ref_id="att1", filename="a.pdf", mime_type="application/pdf"),)
    )
    connector = FakeConnector(batches=[_batch(record)], attachments={("r1", "att1"): b"%PDF-bytes"})
    summary = h.runner(connector).run()

    assert summary.created == 1
    assert len(h.attachments.captures) == 1
    capture = h.attachments.captures[0]
    assert capture["content"] == b"%PDF-bytes"
    assert capture["filename"] == "a.pdf"
    assert capture["source"] == "gcal"  # connector name
    assert capture["source_ref"] == "r1"
    assert len(h.attachments.submitted) == 1  # queued for reading


# --- helpers for the ask-hiding test -----------------------------------------


class _FailingEmbedder:
    def embed(self, text: str) -> list[float]:
        raise RuntimeError("semantic leg unavailable in this test")


class _PassThroughReranker:
    def rerank(self, question, candidates, top_n):
        return list(candidates)[:top_n]


def test_deduped_create_counts_as_deduped_not_created(runner_db):
    """A new external id whose content is IDENTICAL to an existing item is a
    dedupe hit, not a capture (worker_6 found the overcount on #59: the DB
    still held one item while summary.created said 1).

    Ingest exposes the outcome without any semantics change: create() returns
    a detached snapshot with deduplicated=True (ingest.py _dedupe_snapshot)
    and the EXISTING id. The runner must count it as its own outcome, point
    the ledger at the existing item, and never create a second item.
    """
    h = Harness(runner_db)
    # The item the connector's record will collide with: same source, text,
    # occurred_at minute -> identical content fingerprint.
    existing = h.ingest.create(
        schemas.ItemCreate(
            raw_text="body of r1",
            source="gcal",
            occurred_at=datetime(2026, 10, 1, 9, 0, tzinfo=UTC),
        )
    )
    assert existing.deduplicated is False  # a real create, not a snapshot

    connector = FakeConnector(batches=[_batch(make_record(external_id="new-id"))])
    summary = h.runner(connector).run()

    assert summary.created == 0  # RED TODAY: 1 — the overstatement
    assert summary.deduped == 1
    # The ledger points the NEW external id at the EXISTING item …
    row = h.records.get("gcal", "new-id")
    assert row is not None and row.item_id == existing.id
    assert h.item("new-id").id == existing.id
    # … and no second item exists.
    assert runner_db.query(MemoryItem).filter(MemoryItem.source == "gcal").count() == 1


# --- FetchResult.more plumbing (finding 4) -------------------------------------


def test_summary_carries_more_from_the_fetch_result(runner_db):
    """The runner must surface the source's 'truncated, more coming' signal
    (FetchResult.more) on the summary, so the CLI --drain loop can keep
    going even when the cursor did not move (email's query path drops the
    cursor when it truncates)."""
    h = Harness(runner_db)
    connector = FakeConnector(
        batches=[FetchResult(records=[make_record()], next_cursor=None, more=True)]
    )
    summary = h.runner(connector).run()

    assert summary.more is True
    # ... and the default stays False for every other connector.
    plain = Harness(runner_db)
    plain_summary = plain.runner(FakeConnector(batches=[_batch(make_record("r2"))])).run()
    assert plain_summary.more is False

# --- PR-c finding (6): a tombstone must be able to clear a quarantine --------

def test_tombstone_clears_quarantine(runner_db):
    """PR-c finding (6): a deletion whose application fails 3x is QUARANTINED
    with the failing version recorded — for Gmail that version is None (all
    gmail records are version=None by design). When the transient failure
    heals and the source re-offers the SAME tombstone (version=None), the
    version-equality skip at the quarantine gate holds it skipped forever and
    the item stays visible despite the source deletion. A deleted=True record
    must always reach the deletion path."""
    h = Harness(runner_db)

    # A transient repo hiccup on the deletion path: children_of raises while
    # armed (a DB hiccup healing later — the tombstone is re-offered after).
    real_children = h.repo.children_of

    def flaky_children(item_id: Any) -> Any:
        if getattr(flaky_children, "armed", False):
            raise RuntimeError("simulated transient repo failure")
        return real_children(item_id)

    h.repo.children_of = flaky_children
    tombstone = make_record(external_id="q1", version=None, deleted=True, text="")
    connector = FakeConnector(
        batches=[
            _batch(make_record(external_id="q1", version=None)),  # created
            _batch(tombstone),  # deletion attempt 1: transient failure
            _batch(tombstone),  # attempt 2
            _batch(tombstone),  # attempt 3 -> quarantined, cursor advances
            _batch(tombstone),  # healed: the tombstone must now apply
        ]
    )
    runner = h.runner(connector)

    runner.run()
    assert h.item("q1") is not None  # created, visible

    flaky_children.armed = True
    h.tick(backoff_seconds(1) + 1)
    runner.run()  # fail 1
    h.tick(backoff_seconds(2) + 1)
    runner.run()  # fail 2
    h.tick(backoff_seconds(4) + 1)
    s3 = runner.run()  # fail 3 -> quarantined (errors 0, cursor advances)
    assert s3.errors == 0 and s3.cursor_saved is True
    row = h.records.get("gcal", "q1")
    assert row is not None and row.status == "failed"  # precondition: quarantined
    assert row.version is None  # the None the re-offered tombstone carries
    assert h.item("q1").meta.get("source_deleted") is not True  # type: ignore[union-attr]

    flaky_children.armed = False  # the transient failure heals
    s = runner.run()  # the healed tombstone is re-offered

    assert s.deleted >= 1, "the healed tombstone must reach the deletion path"
    row = h.records.get("gcal", "q1")
    assert row is not None and row.status != "failed", "quarantine cleared"
    assert row.fail_count == 0
    item = h.item("q1")
    assert item is not None
    assert item.meta.get("source_deleted") is True, "the item is hidden"


def test_quarantine_still_holds_same_version_content_record(runner_db):
    """The OTHER half of the (6) rule: a CONTENT record (deleted=False) whose
    version equals the quarantined row's is still skipped unattempted — the
    quarantine keeps protecting against a poison record. Only tombstones
    bypass the version-equality skip."""
    h = Harness(runner_db)
    real_create = h.ingest.create

    def flaky_create(payload: Any) -> Any:
        if payload.raw_text == "poison body":
            raise RuntimeError("simulated ingest failure")
        return real_create(payload)

    h.ingest.create = flaky_create
    # Quarantine via the REAL poison-create path (the item is never captured,
    # the ledger row holds the failing version "v1").
    connector = FakeConnector(
        batches=[
            _batch(make_record(external_id="q2", version="v1", text="poison body")),
            _batch(make_record(external_id="q2", version="v1", text="poison body")),
            _batch(make_record(external_id="q2", version="v1", text="poison body")),
            _batch(make_record(external_id="q2", version="v1", text="still poison")),
        ]
    )
    runner = h.runner(connector)
    runner.run()  # fail 1: errors 1, cursor held
    h.tick(backoff_seconds(1) + 1)
    runner.run()  # fail 2
    h.tick(backoff_seconds(2) + 1)
    s3 = runner.run()  # fail 3 -> quarantined, cursor advances
    assert s3.errors == 0 and s3.cursor_saved is True
    row = h.records.get("gcal", "q2")
    assert row is not None and row.status == "failed"  # precondition
    assert row.version == "v1" and row.fail_count == 3

    s = runner.run()  # same-version CONTENT record: must stay skipped

    assert s.errors == 0
    assert s.skipped >= 1
    row = h.records.get("gcal", "q2")
    assert row is not None and row.status == "failed", "still quarantined"
    assert row.fail_count == 3
    assert _email_item(h, "q2") is None  # never captured


# --- PR-c finding (5): reset reconcile — ledger vs the full reset sweep -------

def _seed_items(h: Any, external_ids: list[str]) -> None:
    """Create one captured item per external id through the runner (v1).

    Distinct texts: ingest's fingerprint dedupe would otherwise collapse
    same-text records onto one item."""
    connector = FakeConnector(
        name="email",
        batches=[
            _batch(
                *[
                    make_record(external_id=eid, source="email", text=f"body of {eid}")
                    for eid in external_ids
                ]
            )
        ],
    )
    h.runner(connector).run()


def _email_item(h: Any, external_id: str) -> Any:
    """h.item is gcal-hardcoded; the email ledger needs its own lookup."""
    row = h.records.get("email", external_id)
    if row is None or row.item_id is None:
        return None
    return h.repo.get(row.item_id)


def test_reset_reconcile_tombstones_missing_ledger_ids(runner_db):
    """PR-c finding (5): on a reset batch whose connector swept the ENTIRE
    sync scope, ledgered ids the sweep no longer lists are tombstoned through
    the normal deletion path (source_deleted marks, no content write)."""
    h = Harness(runner_db)
    ids = [f"r{i}" for i in range(1, 11)]  # 10 live items
    _seed_items(h, ids)
    assert all(_email_item(h, eid) is not None for eid in ids)

    # History expired: the connector re-lists the scope and sweeps ALL ids.
    # r9 and r10 lost the label before the reset — 2 of 10 missing (20%,
    # under the 25% ceiling): a legitimate reconcile.
    connector = FakeConnector(
        name="email",
        batches=[
            FetchResult(records=[], next_cursor="h2", reset=True,
                        full_listing_ids=tuple(ids[:8])),
        ],
    )
    s = h.runner(connector).run()

    assert s.deleted == 2, "the two missing ids are tombstoned"
    for eid in ("r9", "r10"):
        item = _email_item(h, eid)
        assert item is not None
        assert item.meta.get("source_deleted") is True, f"{eid} hidden"
    assert _email_item(h, "r1").meta.get("source_deleted") is None


def test_reset_reconcile_refused_above_fraction_ceiling(runner_db):
    """A diff above the fraction ceiling (more than 25% of the ledgered live
    items) is REFUSED and logged — a mass-hide from a bad listing must never
    happen silently. Nothing is tombstoned on refusal."""
    h = Harness(runner_db)
    ids = [f"r{i}" for i in range(1, 5)]  # 4 live items
    _seed_items(h, ids)
    connector = FakeConnector(
        name="email",
        batches=[
            FetchResult(records=[], next_cursor="h2", reset=True,
                        full_listing_ids=("r1",)),  # 3 of 4 missing = 75%
        ],
    )
    s = h.runner(connector).run()

    assert s.deleted == 0, "refused: nothing tombstoned"
    for eid in ("r2", "r3", "r4"):
        item = _email_item(h, eid)
        assert item is not None
        assert item.meta.get("source_deleted") is not True
    from sqlalchemy import text as sql_text

    refused = h.session.execute(
        sql_text("SELECT count(*) FROM ops_events WHERE kind = 'reset_reconcile_refused'")
    ).scalar()
    assert refused and refused >= 1, "the refusal is recorded as an ops event"


def test_reset_reconcile_refused_above_absolute_cap(runner_db):
    """More than 200 missing ids is refused outright, whatever the fraction."""
    h = Harness(runner_db)
    ids = [f"m{i:03d}" for i in range(210)]
    _seed_items(h, ids)
    connector = FakeConnector(
        name="email",
        batches=[
            FetchResult(records=[], next_cursor="h2", reset=True,
                        full_listing_ids=("m000",)),  # 209 missing
        ],
    )
    s = h.runner(connector).run()
    assert s.deleted == 0, "refused: above the absolute cap"


def test_reset_reconcile_ignores_user_deleted_and_incomplete_sweep(runner_db):
    """(a) A user-deleted row (item_id NULL) is never resurrected or deleted
    by the sweep; (b) an INCOMPLETE sweep (full_listing_ids None) reconciles
    nothing — never tombstone on a partial list."""
    from brain.db.models import ConnectorRecord

    h = Harness(runner_db)
    ids = [f"r{i}" for i in range(1, 9)]  # 8 live items
    _seed_items(h, ids)
    # A user-deleted row: the item was deleted out-of-band; the ledger keeps
    # the row with item_id NULL.
    h.session.add(ConnectorRecord(source="email", external_id="u1", version=None, item_id=None))
    h.session.commit()

    # Incomplete sweep: the connector could not finish the walk.
    connector = FakeConnector(
        name="email",
        batches=[FetchResult(records=[], next_cursor="h2", reset=True, full_listing_ids=None)],
    )
    s = h.runner(connector).run()
    assert s.deleted == 0, "an incomplete sweep reconciles nothing"
    assert _email_item(h, "r1").meta.get("source_deleted") is not True

    # Complete sweep missing r1 AND u1: r1 tombstoned (1 of 8 = 12.5%, under
    # the ceiling), u1 untouched (item_id NULL is not a diff candidate).
    connector = FakeConnector(
        name="email",
        batches=[FetchResult(records=[], next_cursor="h3", reset=True,
                             full_listing_ids=tuple(ids[1:]))],
    )
    s = h.runner(connector).run()
    assert s.deleted == 1
    assert _email_item(h, "r1").meta.get("source_deleted") is True
    row = h.session.get(ConnectorRecord, ("email", "u1"))
    assert row is not None and row.item_id is None  # user-deleted: untouched


def test_reset_reconcile_is_idempotent(runner_db):
    """A second reset with the same sweep re-tombstones nothing: the items
    are already source_deleted (put + skipped)."""
    h = Harness(runner_db)
    ids = [f"r{i}" for i in range(1, 9)]
    _seed_items(h, ids)
    deleted_counts = []
    for cursor in ("h2", "h3"):
        connector = FakeConnector(
            name="email",
            batches=[FetchResult(records=[], next_cursor=cursor, reset=True,
                                 full_listing_ids=tuple(ids[1:]))],
        )
        deleted_counts.append(h.runner(connector).run().deleted)
    assert deleted_counts == [1, 0], (
        "the first reset tombstones r1; the second re-tombstones nothing"
    )
    item = _email_item(h, "r1")
    assert item is not None and item.meta.get("source_deleted") is True




def test_reset_reconcile_excludes_already_source_deleted_items(runner_db):
    """Review fix: the diff base must be the LIVE ledger set only. Rows whose
    item is already source_deleted (earlier label removals / tombstones) are
    legitimately absent from any sweep — counting them as missing would push
    every future reconcile over the ceilings and hide real deletions behind
    the refusal. 10 live + 5 already deleted; a sweep listing 9 live misses
    exactly 1 → exactly 1 tombstone, no refusal (pre-fix: 6 of 15 = 40% ->
    refused, deleted == 0)."""
    h = Harness(runner_db)
    live = [f"r{i}" for i in range(1, 11)]
    gone = [f"d{i}" for i in range(1, 6)]
    _seed_items(h, live + gone)
    # Source-delete the 5 through the REAL deletion path (version bump).
    connector = FakeConnector(
        name="email",
        batches=[
            _batch(
                *[
                    make_record(
                        external_id=eid, source="email", version="v2", deleted=True, text=""
                    )
                    for eid in gone
                ]
            )
        ],
    )
    h.runner(connector).run()
    assert all(_email_item(h, eid).meta.get("source_deleted") is True for eid in gone)

    # Reset: the sweep lists 9 of the 10 LIVE ids (r10 lost its label since).
    connector = FakeConnector(
        name="email",
        batches=[
            FetchResult(records=[], next_cursor="h2", reset=True,
                        full_listing_ids=tuple(live[:9])),
        ],
    )
    from sqlalchemy import text as sql_text

    # ops_events is never cleaned between tests: count the DELTA.
    refused_before = h.session.execute(
        sql_text("SELECT count(*) FROM ops_events WHERE kind = 'reset_reconcile_refused'")
    ).scalar()

    s = h.runner(connector).run()

    assert s.deleted == 1, "exactly the one newly-missing live id is tombstoned"
    item = _email_item(h, "r10")
    assert item is not None and item.meta.get("source_deleted") is True
    refused_after = h.session.execute(
        sql_text("SELECT count(*) FROM ops_events WHERE kind = 'reset_reconcile_refused'")
    ).scalar()
    assert refused_after == refused_before, (
        "no refusal: the already-deleted rows are not diff candidates"
    )
