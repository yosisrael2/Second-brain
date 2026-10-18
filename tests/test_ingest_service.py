"""IngestService tests: capture never blocked, classification, indexing."""

import pytest
from qdrant_client import QdrantClient
from tests.fakes import FULL_CLASSIFICATION, FakeRepository, StubClassifier

from brain import schemas
from brain.ai.classify import FALLBACK_CLASSIFICATION
from brain.ai.embedder import EmbedderError, FakeEmbedder
from brain.ai.jev import JevError
from brain.ai.vectorstore import QdrantVectorStore, VectorStoreError
from brain.db.enums import Actionability, ItemStatus, ItemType
from brain.db.models import MemoryItem
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


def test_create_never_overwrites_explicit_status():
    classifier = StubClassifier(FULL_CLASSIFICATION)  # conf 0.95 would route to filed
    service = make_service(classifier)

    item = service.create(payload(status="needs_review"))

    assert item.status is ItemStatus.NEEDS_REVIEW  # caller's value kept
    assert item.type is ItemType.TASK  # classification still fills the rest
    assert item.importance == 4


def test_create_never_overwrites_explicit_importance():
    classifier = StubClassifier(FULL_CLASSIFICATION)  # importance 4
    service = make_service(classifier)

    item = service.create(payload(importance=2))

    assert item.importance == 2  # caller's value kept
    assert item.type is ItemType.TASK
    assert item.actionability is Actionability.NOW  # unsent field still filled


def test_create_never_overwrites_explicit_actionability_and_confidence():
    classifier = StubClassifier(FULL_CLASSIFICATION)  # NOW / 0.95
    service = make_service(classifier)

    item = service.create(payload(actionability="later", confidence=0.5))

    assert item.actionability is Actionability.LATER  # caller's values kept
    assert item.confidence == pytest.approx(0.5)
    assert item.type is ItemType.TASK
    assert item.status is ItemStatus.FILED  # status not sent -> routed by Jev confidence


def test_create_never_overwrites_explicit_null_project():
    classifier = StubClassifier({**FULL_CLASSIFICATION, "project": "payments"})
    service = make_service(classifier)

    item = service.create(payload(project=None))

    assert item.project is None  # explicitly sent null is respected


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


class _CountingEmbedder:
    """Embedder spy counting single vs batched embed calls (delegates to inner)."""

    def __init__(self, inner: object) -> None:
        self._inner = inner
        self.single_calls = 0
        self.batch_calls = 0
        self.batch_sizes: list[int] = []

    def embed(self, text: str) -> list[float]:
        self.single_calls += 1
        return self._inner.embed(text)  # type: ignore[attr-defined]

    def embed_batch(self, texts: list[str]) -> list[list[float]]:
        self.batch_calls += 1
        self.batch_sizes.append(len(texts))
        return self._inner.embed_batch(texts)  # type: ignore[attr-defined]

    def close(self) -> None:
        self._inner.close()  # type: ignore[attr-defined]


class _CountingStore:
    """VectorStore spy counting upsert shapes (delegates to an inner store)."""

    def __init__(self, inner: QdrantVectorStore) -> None:
        self._inner = inner
        self.upsert_single_calls = 0
        self.upsert_many_calls = 0
        self.upsert_many_sizes: list[int] = []
        self.delete_collection_calls = 0

    def ensure_collection(self, dim: int) -> None:
        self._inner.ensure_collection(dim)

    def upsert(self, item_id: str, vector: list[float], payload: dict) -> None:
        self.upsert_single_calls += 1
        self._inner.upsert(item_id, vector, payload)

    def upsert_many(self, points: list[tuple[str, list[float], dict]]) -> None:
        self.upsert_many_calls += 1
        self.upsert_many_sizes.append(len(points))
        for item_id, vector, payload in points:
            self._inner.upsert(item_id, vector, payload)

    def delete(self, item_id: str) -> None:
        self._inner.delete(item_id)

    def delete_collection(self) -> None:
        self.delete_collection_calls += 1
        self._inner.delete_collection()

    def search(self, vector: list[float], k: int = 5) -> list[dict]:
        return self._inner.search(vector, k)

    def close(self) -> None:
        self._inner.close()


def test_reindex_uses_one_embed_and_one_upsert_per_db_batch():
    """Reindex batching: one embed call + one upsert call per DB batch.

    With 5 items and batch_size=2 there must be exactly 3 embed calls
    (sizes 2, 2, 1) and 3 upserts (sizes 2, 2, 1) — plus the single-probe
    embed before the purge — never one embed+upsert pair per item.
    """
    repo = FakeRepository()
    for i in range(5):
        repo.add(MemoryItem(raw_text=f"note number {i}"))
    store = _CountingStore(QdrantVectorStore(client=QdrantClient(":memory:")))
    embedder = _CountingEmbedder(FakeEmbedder(dim=8))
    indexer = QdrantItemIndexer(embedder, store)
    service = IngestService(repo, StubClassifier(FULL_CLASSIFICATION), indexer)

    indexed = service.reindex(batch_size=2)

    assert indexed == 5
    assert embedder.batch_calls == 3  # one embed per DB batch
    assert embedder.batch_sizes == [2, 2, 1]
    assert embedder.single_calls == 1  # the pre-purge probe only
    assert store.upsert_many_calls == 3  # one upsert per DB batch
    assert store.upsert_many_sizes == [2, 2, 1]
    assert store.upsert_single_calls == 0
    assert store.delete_collection_calls == 1
    # Final state identical to the per-item path: every item is retrievable.
    probe = FakeEmbedder(dim=8)
    hits = store._inner.search(probe.embed("note number 3"), k=10)
    assert len(hits) == 5


def test_reindex_embedder_failure_before_purge_leaves_collection_untouched():
    """An EmbedderError in reindex must hit before the collection is dropped."""
    repo = FakeRepository()
    good = FakeEmbedder(dim=8)
    store = QdrantVectorStore(client=QdrantClient(":memory:"))
    existing = repo.add(MemoryItem(raw_text="already indexed note"))
    store.ensure_collection(8)
    store.upsert(
        str(existing.id), good.embed("already indexed note"), {"id": str(existing.id)}
    )

    class BrokenEmbedder:
        def embed(self, text: str) -> list[float]:
            raise EmbedderError("embedder down")

        def embed_batch(self, texts: list[str]) -> list[list[float]]:
            raise EmbedderError("embedder down")

        def close(self) -> None: ...

    indexer = QdrantItemIndexer(BrokenEmbedder(), store)  # type: ignore[arg-type]
    service = IngestService(repo, StubClassifier(FULL_CLASSIFICATION), indexer)

    with pytest.raises(EmbedderError):
        service.reindex(batch_size=10)

    # The collection was NOT purged: the pre-existing vector is still there.
    hits = store.search(good.embed("already indexed note"), k=5)
    assert [hit["id"] for hit in hits] == [str(existing.id)]


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
