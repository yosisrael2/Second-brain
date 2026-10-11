"""IngestService tests: capture never blocked, classification, indexing."""

import pytest
from qdrant_client import QdrantClient
from tests.fakes import FULL_CLASSIFICATION, FakeRepository, StubClassifier

from brain import schemas
from brain.ai.embedder import FakeEmbedder
from brain.ai.vectorstore import QdrantVectorStore
from brain.db.enums import ItemStatus, ItemType
from brain.services.ingest import IngestService


def make_service(
    classifier: StubClassifier, repo: FakeRepository | None = None, store=None
) -> IngestService:
    return IngestService(
        repo=repo or FakeRepository(),
        classifier=classifier,
        embedder=FakeEmbedder(dim=8),
        store=store or QdrantVectorStore(client=QdrantClient(":memory:")),
    )


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
    assert item.actionability.value == "now"
    assert item.confidence == pytest.approx(0.95)
    assert item.status is ItemStatus.FILED  # 0.95 >= 0.9
    assert len(repo.items) == 1
    store_hits = service._store.search(FakeEmbedder(dim=8).embed(item.raw_text), k=5)  # noqa: SLF001
    assert [hit["id"] for hit in store_hits] == [str(item.id)]


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
    service = make_service(StubClassifier(error=RuntimeError("jev down")), repo=repo)

    item = service.create(payload())

    assert item.id in repo.items  # captured
    assert item.type is None
    assert item.status is ItemStatus.INBOX  # stays in inbox, indexed later


def test_classifier_fallback_result_keeps_inbox():
    from brain.ai.classify import FALLBACK_CLASSIFICATION

    service = make_service(StubClassifier(FALLBACK_CLASSIFICATION))

    item = service.create(payload())

    assert item.type is None
    assert item.status is ItemStatus.INBOX


def test_store_failure_never_blocks_capture():
    class BrokenStore:
        def ensure_collection(self, dim: int) -> None:
            raise RuntimeError("qdrant down")

        def upsert(self, item_id: str, vector: list[float], payload: dict) -> None:
            raise RuntimeError("qdrant down")

        def search(self, vector: list[float], k: int) -> list[dict]:
            raise RuntimeError("qdrant down")

    repo = FakeRepository()
    service = IngestService(
        repo=repo,
        classifier=StubClassifier(FULL_CLASSIFICATION),
        embedder=FakeEmbedder(dim=8),
        store=BrokenStore(),  # type: ignore[arg-type]
    )

    item = service.create(payload())

    assert item.id in repo.items  # captured anyway
    assert item.status is ItemStatus.FILED  # classification still applied


def test_reindex_indexes_every_item_and_survives_empty_store():
    repo = FakeRepository()
    store = QdrantVectorStore(client=QdrantClient(":memory:"))
    service = make_service(StubClassifier(FULL_CLASSIFICATION), repo=repo, store=store)

    first = service.create(payload(raw_text="first note"), classify=False)
    second = service.create(payload(raw_text="second note"), classify=False)
    repo.items.pop(first.id)  # simulate an item that missed indexing earlier
    repo.add(first)

    indexed = service.reindex(batch_size=1)

    assert indexed == 2
    vector = FakeEmbedder(dim=8).embed("second note")
    hits = store.search(vector, k=5)
    assert str(second.id) in [hit["id"] for hit in hits]
