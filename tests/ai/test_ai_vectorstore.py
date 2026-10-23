"""Tests for brain.ai.vectorstore using an in-memory Qdrant (no network)."""

import pytest
from qdrant_client import QdrantClient

from brain.ai.vectorstore import QdrantVectorStore, VectorStoreError


@pytest.fixture(name="store")
def store_fixture() -> QdrantVectorStore:
    return QdrantVectorStore(collection="test_items", client=QdrantClient(":memory:"))


def test_ensure_collection_is_idempotent(store):
    store.ensure_collection(dim=4)
    store.ensure_collection(dim=4)  # second call must not raise

    assert store._client.collection_exists("test_items")  # noqa: SLF001


def test_ensure_collection_rejects_dim_mismatch(store):
    store.ensure_collection(dim=4)

    with pytest.raises(VectorStoreError, match="brain reindex") as excinfo:
        store.ensure_collection(dim=384)

    assert "vector size 4" in str(excinfo.value)
    assert "384" in str(excinfo.value)


def test_upsert_and_search_roundtrip(store):
    store.ensure_collection(dim=4)
    store.upsert("near", [1.0, 0.0, 0.0, 0.0], {"text": "nearest"})
    store.upsert("far", [0.0, 1.0, 0.0, 0.0], {"text": "farther"})

    results = store.search([0.9, 0.1, 0.0, 0.0], k=2)

    assert [item["id"] for item in results] == ["near", "far"]
    assert results[0]["payload"]["text"] == "nearest"
    assert isinstance(results[0]["score"], float)


def test_search_respects_k(store):
    store.ensure_collection(dim=4)
    for index in range(5):
        store.upsert(f"item-{index}", [float(index), 0.0, 0.0, 0.0], {})

    results = store.search([0.0, 0.0, 0.0, 0.0], k=3)

    assert len(results) == 3


def test_upsert_same_id_overwrites(store):
    store.ensure_collection(dim=2)
    store.upsert("dup", [1.0, 0.0], {"version": 1})
    store.upsert("dup", [1.0, 0.0], {"version": 2})

    results = store.search([1.0, 0.0], k=1)

    assert len(results) == 1
    assert results[0]["payload"]["version"] == 2


def test_string_ids_are_mapped_to_deterministic_uuid_points(store):
    store.ensure_collection(dim=2)
    store.upsert("my-item", [1.0, 0.0], {"title": "t"})

    first = store.search([1.0, 0.0], k=1)[0]
    second = store.search([1.0, 0.0], k=1)[0]

    assert first["id"] == "my-item"  # original id preserved via payload
    assert first["score"] == second["score"]


def test_integer_ids_are_used_directly(store):
    store.ensure_collection(dim=2)
    store.upsert(42, [1.0, 0.0], {"title": "t"})

    results = store.search([1.0, 0.0], k=1)

    assert results[0]["id"] == 42


def test_negative_integer_ids_are_rejected(store):
    with pytest.raises(VectorStoreError, match="non-negative"):
        store.upsert(-1, [1.0, 0.0], {})


def test_missing_url_and_client_raises_clear_error(monkeypatch):
    monkeypatch.delenv("QDRANT_URL", raising=False)

    # a *_test collection: the live-collection construction guard (PR #43)
    # must not mask the missing-URL error this test is about
    with pytest.raises(VectorStoreError, match="QDRANT_URL"):
        QdrantVectorStore(collection="brain_items_test")


def test_satisfies_vector_store_protocol():
    from brain.ai.vectorstore import VectorStore

    store: VectorStore = QdrantVectorStore(client=QdrantClient(":memory:"))
    assert hasattr(store, "ensure_collection")
