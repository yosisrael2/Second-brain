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
        return None if row is None else self.repo.get(row.item_id)


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
