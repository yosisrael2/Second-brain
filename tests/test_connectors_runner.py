"""ConnectorRunner semantics (connectors ticket, C1 acceptance).

Real IngestService + real repositories on the brain_w8_test database; the
connector itself, the indexer, the enrichment queue and the attachments core
are recording fakes. No network anywhere.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from qdrant_client import QdrantClient
from sqlalchemy import text
from tests.fakes import StubClassifier

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
    connector = FakeConnector(
        batches=[_batch(make_record()), _batch(make_record())]
    )
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
    connector = FakeConnector(
        batches=[_batch(make_record()), _batch(make_record(deleted=True))]
    )
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
    assert item.meta["connector_text_sha"] == hashlib.sha256(
        item.raw_text.encode("utf-8")
    ).hexdigest()


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
    s2 = runner.run()
    assert s2.errors == 1 and s2.cursor_saved is False

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

    runner.run()  # failure 2
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
    for _ in range(3):
        runner.run()
    row = h.records.get("gcal", "bad")
    assert row is not None and row.status == "failed"

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
    connector = FakeConnector(
        batches=[_batch(make_record()), _batch(make_record(deleted=True))]
    )
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
    connector = FakeConnector(
        batches=[_batch(*records)], attachments={("r3", "present"): b"x"}
    )
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
        _batch(
            *[
                make_record(external_id=f"r{i}", text=f"body {i}")
                for i in range(1, 6)
            ]
        )
    )
    retry = runner.run()
    # The loop continues past a failed record, so r4/r5 were already written
    # in the first run; the retry finds them UNCHANGED (no duplicates),
    # writes the one record that failed, and only now moves the cursor.
    assert retry.created == 1  # r3, the one that failed last time
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


def test_retryable_error_backs_off_and_is_capped(runner_db):
    h = Harness(runner_db)
    connector = FakeConnector(
        errors=[ConnectorRetryableError("http 429"), ConnectorRetryableError("http 500")],
        batches=[_batch(make_record())],
    )
    runner = h.runner(connector)
    first = runner.run()
    second = runner.run()

    assert (first.state, second.state) == ("retry_error", "retry_error")
    assert first.cursor_saved is False
    state = h.state.get("gcal")
    assert state.last_error == "retryable: http 500"
    assert h.state.attempt("gcal") == 2
    assert backoff_seconds(2) > backoff_seconds(1) > backoff_seconds(0)
    assert backoff_seconds(99) == backoff_seconds(98)  # capped
    # A success resets the backoff counter and saves the cursor.
    third = runner.run()
    assert third.state == "ok"
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
    connector = FakeConnector(
        batches=[_batch(record)], attachments={("r1", "att1"): b"%PDF-bytes"}
    )
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
