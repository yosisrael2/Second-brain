"""Item indexing: embed + upsert / remove vectors for memory items."""

from __future__ import annotations

import logging
import uuid
from typing import Protocol

from brain.ai.embedder import Embedder
from brain.ai.vectorstore import VectorStore
from brain.db.models import MemoryItem

logger = logging.getLogger(__name__)


class ItemIndexerProto(Protocol):
    """Vector-index seam used by IngestService and ItemsService (Strategy).

    Implementations raise :class:`brain.ai.embedder.EmbedderError` or
    :class:`brain.ai.vectorstore.VectorStoreError`; callers own the
    never-block-capture fallback policy.
    """

    def index(self, item: MemoryItem) -> None:
        """Embed and upsert the item's vector + payload."""
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
        vector = self._embedder.embed(item.raw_text)
        if not self._ensured:
            self._store.ensure_collection(len(vector))
            self._ensured = True
        self._store.upsert(str(item.id), vector, self._payload_for(item))

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
