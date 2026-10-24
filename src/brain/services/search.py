"""Search use cases: semantic vector search with keyword fallback (Service layer)."""

from __future__ import annotations

import hashlib
import logging
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol

from brain import ops
from brain.ai.embedder import Embedder, EmbedderError
from brain.ai.vectorstore import VectorStore, VectorStoreError
from brain.db.models import MemoryItem
from brain.repositories.items import is_hidden_by_default
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

    def search(
        self,
        query: str,
        *,
        limit: int,
        offset: int = 0,
        extra_items: Sequence[MemoryItem] = (),
    ) -> list[SearchHit]:
        """Return up to ``limit`` items best matching ``query``.

        Hybrid retrieval: semantic (vector) hits fused with full-text keyword
        hits by reciprocal-rank fusion — exact words (e.g. Hebrew names the
        embedder may blur) pull their items up without abandoning semantics.
        Falls back to keyword-only when the embedder or vector store is
        unavailable, so search never hard-fails on infrastructure.

        ``extra_items`` (memory-layer recall legs, e.g. PR 0d kid channels)
        join the candidate pool BEFORE the rerank, so the reranker orders
        them fairly against the semantic hits; empty extras keep the pool
        byte-identical to the pre-0d behaviour.
        """
        try:
            with ops.span("search", "embed", edge="search->embedder"):
                vector = self._embedder.embed(query)
            with ops.span("search", "qdrant", edge="search->qdrant"):
                raw_hits = self._store.search(vector, k=limit + offset)
        except (EmbedderError, VectorStoreError):
            logger.warning(
                "search: semantic search unavailable; falling back to keywords", exc_info=True
            )
            ops.record_event(
                "search", "keyword_fallback", ok=True,
                detail={"reason": "semantic unavailable", "query_chars": len(query)},
            )
            fallback = self._keyword_fallback(query, limit=limit, offset=offset)
            if not extra_items:
                return fallback
            seen: set[uuid.UUID] = {hit.item.id for hit in fallback}
            extras = [
                SearchHit(item=item, score=None)
                for item in extra_items
                if item.id not in seen
            ]
            for hit in extras:
                seen.add(hit.item.id)
            return fallback + extras
        candidates = [
            {
                "id": str(raw_hit["id"]),
                "text": raw_hit["payload"].get("text", ""),
                "vector_score": raw_hit["score"],
            }
            for raw_hit in raw_hits
        ]
        if extra_items:
            candidate_ids = {candidate["id"] for candidate in candidates}
            for item in extra_items:
                key = str(item.id)
                if key not in candidate_ids:
                    candidate_ids.add(key)
                    candidates.append(
                        {"id": key, "text": item.raw_text, "vector_score": None}
                    )
        with ops.span("search", "rerank", edge="search->rerank") as span:
            pre_ids = [c["id"] for c in candidates]
            span.detail = {
                "extras_in": len(extra_items),
                "pre_rerank_size": len(pre_ids),
                "pre_rerank_hash": hashlib.sha1(",".join(pre_ids).encode()).hexdigest()[:12],
            }
            ranked = self._reranker.rerank(query, candidates, top_n=limit + offset)
        fused = self._fuse_with_keywords(query, ranked, limit=limit + offset)
        return self._load_items(fused[offset : offset + limit])

    _RRF_K = 60

    def _fuse_with_keywords(
        self, query: str, ranked: list[Mapping[str, Any]], *, limit: int
    ) -> list[dict[str, Any]]:
        """Reciprocal-rank fusion of the semantic ranking with keyword hits.

        Both lists contribute rank points (1/(K + rank)); items found by both
        paths bubble up. Keyword-only items keep vector_score None. FTS
        failures are non-fatal (the semantic list alone still answers).
        """
        try:
            items = self._repo.search(query=query, limit=limit, offset=0)
        except Exception:  # noqa: BLE001  (FTS is a recall booster, not a gate)
            logger.debug("search: keyword fusion unavailable", exc_info=True)
            return [dict(hit) for hit in ranked]
        keyword_ids = [str(item.id) for item in items]
        if not keyword_ids:
            return [dict(hit) for hit in ranked]
        fused: dict[str, dict[str, Any]] = {}
        for rank, candidate in enumerate(ranked):
            entry = fused.setdefault(
                str(candidate.get("id")), {**candidate, "rrf": 0.0, "vector_rank": None}
            )
            entry["rrf"] += 1 / (self._RRF_K + rank + 1)
            entry["vector_rank"] = rank
        for rank, item_id in enumerate(keyword_ids):
            entry = fused.setdefault(item_id, {"id": item_id, "rrf": 0.0, "vector_rank": None})
            entry["rrf"] += 1 / (self._RRF_K + rank + 1)
        ordered = sorted(fused.values(), key=lambda entry: entry["rrf"], reverse=True)
        return ordered[:limit]

    def by_occurrence(self, start: datetime, end: datetime, *, limit: int = 30) -> list[SearchHit]:
        """Every item occurring inside [start, end), soonest first (no score)."""
        items = self._repo.by_occurrence(start=start, end=end, limit=limit)
        return [SearchHit(item=item, score=None) for item in items]

    def _keyword_fallback(self, query: str, *, limit: int, offset: int) -> list[SearchHit]:
        """Postgres full-text search, used when semantic search is down."""
        items = self._repo.search(query=query, limit=limit, offset=offset)
        return [SearchHit(item=item, score=None) for item in items]

    def _load_items(self, ranked: Sequence[Mapping[str, Any]]) -> list[SearchHit]:
        """Resolve ranked candidate ids to stored items, skipping unusable ones.

        Candidates whose payload id is not a UUID (corrupt/legacy payload) and
        ids no longer in Postgres are skipped; rank order is preserved. A
        candidate id already resolved is skipped too — stores must hold one
        point per item, but stale collections can violate that and the user
        must never see the same source twice.
        """
        by_id = {
            item.id: item
            for item in self._repo.get_many(_parse_uuids(ranked))
            # PR B (item 20/D4) + D-recon (D4): superseded (struck-through)
            # and confirmed-cancelled items are hidden from search by default
            # — the vector store still holds their points until reindexing,
            # so filter here with the SHARED predicate (never a local copy).
            if not is_hidden_by_default(item)
        }
        hits: list[SearchHit] = []
        seen: set[uuid.UUID] = set()
        for candidate in ranked:
            parsed = candidate_id(candidate)
            item = by_id.get(parsed) if parsed is not None else None
            if item is not None and parsed not in seen:
                seen.add(parsed)  # type: ignore[arg-type]
                hits.append(SearchHit(item=item, score=_as_score(candidate.get("vector_score"))))
        return hits


def candidate_id(candidate: Mapping[str, Any]) -> uuid.UUID | None:
    """Parse a candidate's payload id; None when it is not a UUID."""
    try:
        return uuid.UUID(str(candidate.get("id")))
    except (TypeError, ValueError):
        return None


def _parse_uuids(ranked: Sequence[Mapping[str, Any]]) -> list[uuid.UUID]:
    ids: list[uuid.UUID] = []
    for candidate in ranked:
        parsed = candidate_id(candidate)
        if parsed is not None:
            ids.append(parsed)
    return ids


def _as_score(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
