"""Search use cases: semantic vector search with keyword fallback (Service layer)."""

from __future__ import annotations

import logging
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from brain.ai.embedder import Embedder, EmbedderError
from brain.ai.vectorstore import VectorStore, VectorStoreError
from brain.db.models import MemoryItem
from brain.services.items import ItemsRepositoryProto

logger = logging.getLogger(__name__)


class RerankerProto(Protocol):
    """Interface SearchService needs from a reranker (Strategy)."""

    def rerank(
        self, question: str, candidates: Sequence[Mapping[str, Any]], top_n: int
    ) -> list[Mapping[str, Any]]: ...


@dataclass(frozen=True)
class SearchHit:
    """A memory item with its relevance score (None for keyword fallback)."""

    item: MemoryItem
    score: float | None


class SearchService:
    """Semantic search: embed the query, search Qdrant, rerank, load items.

    Falls back to Postgres keyword search when the embedder or the vector
    store is unavailable, so search never hard-fails on infrastructure.
    """

    def __init__(
        self,
        repo: ItemsRepositoryProto,
        embedder: Embedder,
        store: VectorStore,
        reranker: RerankerProto,
    ) -> None:
        self._repo = repo
        self._embedder = embedder
        self._store = store
        self._reranker = reranker

    def search(self, query: str, *, limit: int, offset: int = 0) -> list[SearchHit]:
        """Return up to ``limit`` items best matching ``query``."""
        try:
            vector = self._embedder.embed(query)
            raw_hits = self._store.search(vector, k=limit + offset)
        except (EmbedderError, VectorStoreError):
            logger.warning(
                "search: semantic search unavailable; falling back to keywords", exc_info=True
            )
            return self._keyword_fallback(query, limit=limit, offset=offset)
        candidates = [
            {
                "id": str(raw_hit["id"]),
                "text": raw_hit["payload"].get("text", ""),
                "vector_score": raw_hit["score"],
            }
            for raw_hit in raw_hits
        ]
        ranked = self._reranker.rerank(query, candidates, top_n=limit + offset)
        return self._load_items(ranked[offset : offset + limit])

    def _keyword_fallback(self, query: str, *, limit: int, offset: int) -> list[SearchHit]:
        """Postgres full-text search, used when semantic search is down."""
        items = self._repo.search(query=query, limit=limit, offset=offset)
        return [SearchHit(item=item, score=None) for item in items]

    def _load_items(self, ranked: Sequence[Mapping[str, Any]]) -> list[SearchHit]:
        """Resolve ranked candidate ids to stored items, skipping deleted ones."""
        hits: list[SearchHit] = []
        for candidate in ranked:
            item = self._repo.get(uuid.UUID(str(candidate["id"])))
            if item is not None:
                hits.append(SearchHit(item=item, score=_as_score(candidate.get("vector_score"))))
        return hits


def _as_score(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
