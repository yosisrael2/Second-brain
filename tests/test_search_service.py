"""SearchService tests: semantic search, rerank ordering, keyword fallback."""


from qdrant_client import QdrantClient
from tests.fakes import FakeRepository

from brain.ai.embedder import EmbedderError, FakeEmbedder
from brain.ai.vectorstore import QdrantVectorStore
from brain.db.models import MemoryItem
from brain.services.search import SearchService


class StubReranker:
    """Reranker stub sorting candidates by a marker word, or failing."""

    def __init__(self, error: Exception | None = None) -> None:
        self._error = error
        self.last_candidates: list[dict] | None = None

    def rerank(self, question, candidates, top_n):
        self.last_candidates = list(candidates)
        if self._error is not None:
            raise self._error
        def rank(candidate: dict) -> int:
            return 0 if question.lower() in candidate.get("text", "").lower() else 1

        return sorted(candidates, key=rank)[:top_n]


class BrokenStore:
    def ensure_collection(self, dim: int) -> None:
        raise RuntimeError("qdrant down")

    def upsert(self, item_id: str, vector: list[float], payload: dict) -> None:
        raise RuntimeError("qdrant down")

    def search(self, vector: list[float], k: int) -> list[dict]:
        raise RuntimeError("qdrant down")


class BrokenEmbedder:
    def embed(self, text: str) -> list[float]:
        raise EmbedderError("no key")


def seed(repo: FakeRepository, *texts: str) -> list[MemoryItem]:
    items = []
    for text in texts:
        item = MemoryItem(raw_text=text)
        repo.add(item)
        items.append(item)
    return items


def index_into(store: QdrantVectorStore, repo: FakeRepository, dim: int = 8) -> None:
    embedder = FakeEmbedder(dim=dim)
    store.ensure_collection(dim)
    for item in repo.items.values():
        store.upsert(
            str(item.id), embedder.embed(item.raw_text), {"id": str(item.id), "text": item.raw_text}
        )


def make_service(repo: FakeRepository, store=None, reranker=None) -> SearchService:
    return SearchService(
        repo=repo,
        embedder=FakeEmbedder(dim=8),
        store=store or QdrantVectorStore(client=QdrantClient(":memory:")),
        reranker=reranker or StubReranker(),
    )


def test_semantic_search_returns_items_with_scores():
    repo = FakeRepository()
    items = seed(repo, "kubernetes rollout guide", "banana bread recipe", "kubernetes secrets")
    store = QdrantVectorStore(client=QdrantClient(":memory:"))
    index_into(store, repo)
    service = make_service(repo, store=store)

    hits = service.search("kubernetes", limit=2)

    assert len(hits) == 2
    assert all(hit.score is not None for hit in hits)
    returned_ids = {hit.item.id for hit in hits}
    assert returned_ids <= {item.id for item in items}


def test_reranker_reorders_candidates():
    repo = FakeRepository()
    seed(repo, "alpha topic text", "beta other", "alpha followup")
    store = QdrantVectorStore(client=QdrantClient(":memory:"))
    index_into(store, repo)
    service = make_service(repo, store=store, reranker=StubReranker())

    hits = service.search("alpha", limit=3)

    assert hits[0].item.raw_text.startswith("alpha")


def test_embedder_failure_falls_back_to_keyword_search():
    repo = FakeRepository()
    seed(repo, "the quarterly report numbers", "an unrelated note")
    service = SearchService(
        repo=repo,
        embedder=BrokenEmbedder(),  # type: ignore[arg-type]
        store=QdrantVectorStore(client=QdrantClient(":memory:")),
        reranker=StubReranker(),
    )

    hits = service.search("quarterly report", limit=5)

    assert [hit.item.raw_text for hit in hits] == ["the quarterly report numbers"]
    assert all(hit.score is None for hit in hits)  # fallback carries no score


def test_store_failure_falls_back_to_keyword_search():
    repo = FakeRepository()
    seed(repo, "the quarterly report numbers", "an unrelated note")
    service = SearchService(
        repo=repo,
        embedder=FakeEmbedder(dim=8),
        store=BrokenStore(),  # type: ignore[arg-type]
        reranker=StubReranker(),
    )

    hits = service.search("quarterly", limit=5)

    assert len(hits) == 1
    assert hits[0].score is None


def test_deleted_items_are_skipped():
    repo = FakeRepository()
    (kept,) = seed(repo, "quarterly report")
    gone = MemoryItem(raw_text="quarterly report too")
    repo.add(gone)
    store = QdrantVectorStore(client=QdrantClient(":memory:"))
    index_into(store, repo)
    del repo.items[gone.id]
    service = make_service(repo, store=store)

    hits = service.search("quarterly", limit=5)

    assert [hit.item.id for hit in hits] == [kept.id]


def test_offset_pages_through_ranked_results():
    repo = FakeRepository()
    items = seed(repo, "note one alpha", "note two alpha", "note three alpha", "unrelated")
    store = QdrantVectorStore(client=QdrantClient(":memory:"))
    index_into(store, repo)
    reranker = StubReranker()
    service = make_service(repo, store=store, reranker=reranker)

    page_two = service.search("alpha", limit=1, offset=1)

    assert len(page_two) == 1
    assert page_two[0].item.id in {item.id for item in items}
    assert reranker.last_candidates is not None
    assert len(reranker.last_candidates) == 2  # limit + offset fetched before paging
