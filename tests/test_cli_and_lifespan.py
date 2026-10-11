"""CLI (brain reindex) and app-lifespan tests."""

from datetime import UTC, datetime
from functools import lru_cache
from typing import Any

from fastapi.testclient import TestClient
from qdrant_client import QdrantClient
from tests.fakes import FakeRepository

import brain.api.deps as deps
from brain.ai.embedder import FakeEmbedder, OpenAIEmbedder
from brain.ai.vectorstore import QdrantVectorStore
from brain.cli import main
from brain.db.models import MemoryItem
from brain.main import create_app


def _repo_with(*texts: str) -> FakeRepository:
    repo = FakeRepository()
    for text in texts:
        repo.add(MemoryItem(raw_text=text, created_at=datetime.now(UTC)))
    return repo


def _reindex_with(repo, embedder, store, batch_size):
    from brain.cli import _reindex

    return _reindex(batch_size=batch_size, repo=repo, embedder=embedder, store=store)


def test_cli_reindex_indexes_all_items():
    repo = _repo_with("note one", "note two", "note three")
    inner_store = QdrantVectorStore(client=QdrantClient(":memory:"))
    store = _Unclosable(inner_store)  # keep the in-memory client searchable after close()
    embedder = _Unclosable(FakeEmbedder(dim=8))

    exit_code = _reindex_with(repo=repo, embedder=embedder, store=store, batch_size=2)

    assert exit_code == 0
    assert store.closed and embedder.closed  # CLI released both clients
    vector = FakeEmbedder(dim=8).embed("note two")
    hits = inner_store.search(vector, k=10)
    assert len(hits) == 3  # every item indexed, paged 2 at a time


class _Unclosable:
    """Delegating wrapper that records close() without closing the wrapped object."""

    def __init__(self, inner: object) -> None:
        self._inner = inner
        self.closed = False

    def close(self) -> None:
        self.closed = True

    def __getattr__(self, name: str) -> object:
        return getattr(self._inner, name)


def test_cli_main_parses_batch_size(monkeypatch):
    seen: dict = {}

    def fake_reindex(batch_size: int, **_: object) -> int:
        seen["batch_size"] = batch_size
        return 0

    monkeypatch.setattr("brain.cli._reindex", fake_reindex)

    exit_code = main(["reindex", "--batch-size", "7"])

    assert exit_code == 0
    assert seen["batch_size"] == 7


def test_cli_reindex_missing_embedding_key_fails_cleanly(capsys):
    repo = _repo_with("note")
    store = QdrantVectorStore(client=QdrantClient(":memory:"))
    broken = OpenAIEmbedder(api_key=None)  # no key: embed() raises EmbedderError

    exit_code = _reindex_with(repo=repo, embedder=broken, store=store, batch_size=10)

    assert exit_code == 1
    err = capsys.readouterr().err
    assert "reindex failed" in err
    assert "PARTIAL" in err and "rerun 'brain reindex'" in err


def test_cli_rejects_unknown_command():
    try:
        main(["frobnicate"])
        failed = False
    except SystemExit as exc:
        failed = exc.code != 0
    assert failed


def test_app_lifespan_closes_shared_clients(monkeypatch):
    closed: list[str] = []

    class FakeClient:
        def __init__(self, name: str) -> None:
            self._name = name

        def close(self) -> None:
            closed.append(self._name)

    def cached(name: str) -> Any:
        @lru_cache
        def factory() -> FakeClient:
            return FakeClient(name)

        return factory

    monkeypatch.setattr(deps, "_jev_client", cached("jev"))
    monkeypatch.setattr(deps, "_item_indexer", cached("indexer"))
    monkeypatch.setattr(deps, "_llm_client", cached("llm"))
    monkeypatch.setattr(deps, "_embedder", cached("embedder"))
    monkeypatch.setattr(deps, "_vector_store", cached("qdrant"))

    # Simulate first use during the app's lifetime: each factory has a cached instance.
    for factory in (
        deps._jev_client,
        deps._item_indexer,
        deps._llm_client,
        deps._embedder,
        deps._vector_store,
    ):
        factory()

    with TestClient(create_app()):
        pass

    assert sorted(closed) == ["embedder", "indexer", "jev", "llm", "qdrant"]


def test_shutdown_does_not_build_uncached_clients(monkeypatch):
    """close_shared_clients must not construct clients that were never used."""

    def explosive() -> FakeEmbedder:
        raise AssertionError("shutdown built a client that was never used")

    monkeypatch.setattr(deps, "_jev_client", lru_cache(explosive))
    monkeypatch.setattr(deps, "_item_indexer", lru_cache(explosive))
    monkeypatch.setattr(deps, "_llm_client", lru_cache(explosive))
    monkeypatch.setattr(deps, "_embedder", lru_cache(explosive))
    monkeypatch.setattr(deps, "_vector_store", lru_cache(explosive))

    deps.close_shared_clients()  # no exception: nothing cached, nothing built


def test_item_indexer_is_app_lifetime(monkeypatch):
    """The cached factory returns one indexer (ensure_collection once per process)."""
    deps._item_indexer.cache_clear()  # noqa: SLF001 - test isolation
    monkeypatch.setattr(deps, "_embedder", lambda: FakeEmbedder(dim=8))
    monkeypatch.setattr(
        deps, "_vector_store", lambda: QdrantVectorStore(client=QdrantClient(":memory:"))
    )

    first = deps.get_item_indexer()
    second = deps.get_item_indexer()

    assert first is second
    deps._item_indexer.cache_clear()  # noqa: SLF001 - do not leak fakes into other tests
