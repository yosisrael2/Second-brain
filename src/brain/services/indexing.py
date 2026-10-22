"""Item indexing: embed + upsert / remove vectors for memory items."""

from __future__ import annotations

import logging
import uuid
from collections.abc import Sequence
from typing import Protocol

from brain import ops
from brain.ai.embedder import Embedder
from brain.ai.vectorstore import VectorStore
from brain.db.models import MemoryItem

logger = logging.getLogger(__name__)

# Probe text embedded once before a destructive reindex purge, so an
# unreachable embedder fails before the collection is dropped.
_PROBE_TEXT = "reindex readiness probe"


class ItemIndexerProto(Protocol):
    """Vector-index seam used by IngestService and ItemsService (Strategy).

    Implementations raise :class:`brain.ai.embedder.EmbedderError` or
    :class:`brain.ai.vectorstore.VectorStoreError`; callers own the
    never-block-capture fallback policy.
    """

    def index(self, item: MemoryItem) -> None:
        """Embed and upsert the item's vector + payload."""
        ...

    def index_batch(self, items: Sequence[MemoryItem]) -> int:
        """Embed and upsert many items with one embed call and one upsert.

        Returns the number of items indexed. Vectors are identical to the
        per-item path; only the round-trips are batched.
        """
        ...

    def probe(self) -> None:
        """Verify the embedder is reachable; raises EmbedderError otherwise.

        Callers run destructive steps (collection purge) only after this.
        """
        ...

    def remove(self, item_id: uuid.UUID) -> None:
        """Delete the item's vector (missing ids are tolerated)."""
        ...

    def purge(self) -> None:
        """Drop all vectors (full rebuild start; also resets caching)."""
        ...


class QdrantItemIndexer:
    """Default ItemIndexerProto implementation over Qdrant.

    Calls ``ensure_collection`` at most once per instance (mi2); the vector
    payload carries the fields the search/ask paths need (id, text, source,
    project).
    """

    def __init__(self, embedder: Embedder, store: VectorStore) -> None:
        self._embedder = embedder
        self._store = store
        self._ensured = False

    def index(self, item: MemoryItem) -> None:
        """Embed and upsert the item's vector + payload.

        The once-per-process collection check is deliberately racy: two
        concurrent first indexes may both call ``ensure_collection`` — the
        store call is idempotent, so the worst case is one redundant call.
        """
        with ops.span("ingest", "embed", edge="ingest->embedder") as span:
            vector = self._embedder.embed(item.raw_text)
            span.detail = {"item_id": str(item.id), "chars": len(item.raw_text)}
        if not self._ensured:
            self._store.ensure_collection(len(vector))
            self._ensured = True
        with ops.span("ingest", "upsert", edge="embedder->qdrant"):
            self._store.upsert(str(item.id), vector, self._payload_for(item))

    def index_batch(self, items: Sequence[MemoryItem]) -> int:
        """Embed and upsert a whole DB batch in one embed + one upsert call.

        Produces the exact vectors and payloads the per-item ``index`` would
        (batch embedding preserves order), so the final Qdrant state is
        identical — just ~2 fewer round-trips per item.
        """
        if not items:
            return 0
        with ops.span("ingest", "embed_batch", edge="ingest->embedder") as span:
            vectors = self._embedder.embed_batch([item.raw_text for item in items])
            span.detail = {"items": len(items)}
        if not self._ensured:
            self._store.ensure_collection(len(vectors[0]))
            self._ensured = True
        with ops.span("ingest", "upsert_many", edge="embedder->qdrant"):
            self._store.upsert_many(
                [
                    (str(item.id), vector, self._payload_for(item))
                    for item, vector in zip(items, vectors, strict=True)
                ]
        )
        return len(items)

    def probe(self) -> None:
        """One throwaway embed call; raises EmbedderError if the embedder is down.

        ``IngestService.reindex`` probes before purging so a failed rebuild
        never starts by dropping the collection.
        """
        self._embedder.embed(_PROBE_TEXT)

    def remove(self, item_id: uuid.UUID) -> None:
        """Delete the item's vector (missing ids are tolerated)."""
        self._store.delete(str(item_id))

    def purge(self) -> None:
        """Drop all vectors and reset collection caching."""
        self._store.delete_collection()
        self._ensured = False

    def close(self) -> None:
        """Lifecycle hook for the app-lifetime factory (deps.close_shared_clients).

        The indexer owns no connections of its own — the injected embedder and
        store are closed separately by their own factories.
        """

    @staticmethod
    def _payload_for(item: MemoryItem) -> dict[str, object]:
        return {
            "id": str(item.id),
            "text": item.raw_text,
            "source": item.source,
            "project": item.project,
        }
