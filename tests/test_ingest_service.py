"""IngestService tests: capture never blocked, classification, indexing."""

import pytest
from qdrant_client import QdrantClient
from tests.fakes import FULL_CLASSIFICATION, FakeRepository, StubClassifier

from brain import schemas
from brain.ai.classify import FALLBACK_CLASSIFICATION
from brain.ai.embedder import FakeEmbedder
from brain.ai.jev import JevError
from brain.ai.vectorstore import QdrantVectorStore, VectorStoreError
from brain.db.enums import Actionability, ItemStatus, ItemType
from brain.services.indexing import QdrantItemIndexer
from brain.services.ingest import IngestService


def make_service(
    classifier: StubClassifier, repo: FakeRepository | None = None, store=None
) -> IngestService:
    store = store or QdrantVectorStore(client=QdrantClient(":memory:"))
    indexer = QdrantItemIndexer(FakeEmbedder(dim=8), store)
    return IngestService(repo or FakeRepository(), classifier, indexer)


def payload(**overrides: object) -> schemas.ItemCreate:
    values: dict = {"raw_text": "Renew the TLS certificate before Friday"}
    values.update(overrides)
    return schemas.ItemCreate(**values)


def test_create_classifies_and_indexes_when_confident():
    repo = FakeRepository()
    service = make_service(StubClassifier(FULL_CLASSIFICATION), repo=repo)

    item = service.create(payload())

    assert item.type is ItemType.TASK
    assert item.importance == 4
    assert item.actionability is Actionability.NOW
    assert item.confidence == pytest.approx(0.95)
    assert item.status is ItemStatus.FILED  # 0.95 >= 0.9
    assert len(repo.items) == 1


def test_create_routes_low_confidence_to_needs_review():
    classifier = StubClassifier({**FULL_CLASSIFICATION, "confidence": 0.7})
    service = make_service(classifier)

    item = service.create(payload())

    assert item.status is ItemStatus.NEEDS_REVIEW


def test_create_with_explicit_type_skips_classification():
    classifier = StubClassifier(FULL_CLASSIFICATION)
    service = make_service(classifier)

    item = service.create(payload(type="note", status="inbox"))

    assert item.type is ItemType.NOTE  # caller's value kept
    assert item.importance is None  # classification not applied
    assert classifier.calls == []  # classifier not even invoked


def test_create_with_classify_flag_false_keeps_item_raw():
    classifier = StubClassifier(FULL_CLASSIFICATION)
    service = make_service(classifier)

    item = service.create(payload(), classify=False)

    assert item.type is None
    assert item.status is ItemStatus.INBOX
    assert classifier.calls == []


def test_jev_failure_never_blocks_capture():
    repo = FakeRepository()
    service = make_service(StubClassifier(error=JevError("jev down")), repo=repo)

    item = service.create(payload())

    assert item.id in repo.items  # captured
    assert item.type is None
    assert item.status is ItemStatus.INBOX  # stays in inbox, indexed later


def test_classifier_fallback_result_keeps_inbox():
    service = make_service(StubClassifier(FALLBACK_CLASSIFICATION))

    item = service.create(payload())

    assert item.type is None
    assert item.status is ItemStatus.INBOX


def test_index_failure_never_blocks_capture():
    class BrokenIndexer:
        def index(self, item) -> None:
            raise VectorStoreError("qdrant down")

        def remove(self, item_id) -> None:
            raise VectorStoreError("qdrant down")

        def purge(self) -> None:
            raise VectorStoreError("qdrant down")

    repo = FakeRepository()
    service = IngestService(
        repo,
        StubClassifier(FULL_CLASSIFICATION),
        BrokenIndexer(),  # type: ignore[arg-type]
    )

    item = service.create(payload())

    assert item.id in repo.items  # captured anyway
    assert item.status is ItemStatus.FILED  # classification still applied


def test_reindex_purges_ghosts_and_indexes_every_item():
    repo = FakeRepository()
    store = QdrantVectorStore(client=QdrantClient(":memory:"))
    service = make_service(StubClassifier(FULL_CLASSIFICATION), repo=repo, store=store)
    embedder = FakeEmbedder(dim=8)

    first = service.create(payload(raw_text="first note"), classify=False)
    second = service.create(payload(raw_text="second note"), classify=False)
    ghost_id = "00000000-0000-0000-0000-00000000dead"
    store.ensure_collection(8)
    store.upsert(ghost_id, embedder.embed("ghost"), {"id": ghost_id, "text": "ghost"})

    indexed = service.reindex(batch_size=1)

    assert indexed == 2
    hits = store.search(embedder.embed("second note"), k=10)
    hit_ids = [hit["id"] for hit in hits]
    assert str(second.id) in hit_ids
    assert ghost_id not in hit_ids  # ghost purged by the full rebuild
    assert str(first.id) in hit_ids
