"""Ingestion use cases: capture, classify, route, and index (Service layer)."""

from __future__ import annotations

import logging
from typing import Any, Protocol

from brain import schemas
from brain.ai.classify import route
from brain.ai.embedder import Embedder
from brain.ai.vectorstore import VectorStore
from brain.db.models import MemoryItem
from brain.services.items import ItemsRepositoryProto

logger = logging.getLogger(__name__)

# Vector payload keys stored alongside each point in Qdrant.
_PAYLOAD_ID = "id"
_PAYLOAD_TEXT = "text"
_PAYLOAD_SOURCE = "source"
_PAYLOAD_PROJECT = "project"


class ClassifierProto(Protocol):
    """Interface IngestService needs from a classifier (Strategy)."""

    def classify(self, text: str) -> dict[str, Any]: ...


class IngestService:
    """Captures memory items; Jev/Qdrant failures never block capture.

    Create persists the item first, then (optionally) fills classification
    fields, then indexes the text in the vector store. Any classifier or
    indexing failure is logged and skipped: the item stays in the inbox and
    is picked up later by ``brain reindex``.
    """

    def __init__(
        self,
        repo: ItemsRepositoryProto,
        classifier: ClassifierProto,
        embedder: Embedder,
        store: VectorStore,
    ) -> None:
        self._repo = repo
        self._classifier = classifier
        self._embedder = embedder
        self._store = store

    def create(self, data: schemas.ItemCreate, classify: bool = True) -> MemoryItem:
        """Create a memory item, classify it on request, and index it."""
        item = MemoryItem(
            raw_text=data.raw_text,
            source=data.source,
            type=data.type,
            project=data.project,
            importance=data.importance,
            actionability=data.actionability,
            confidence=data.confidence,
            status=data.status,
        )
        self._repo.add(item)
        if classify:
            self._apply_classification(item)
        self._index(item)
        self._repo.commit()
        return item

    def reindex(self, batch_size: int = 500) -> int:
        """(Re)index every stored item; returns the number indexed.

        Intended for the ``brain reindex`` CLI: rebuilds the vector store
        from Postgres so items that missed indexing (Jev/Qdrant down, new
        deployment) become searchable.
        """
        offset = 0
        indexed = 0
        dim: int | None = None
        while True:
            batch = self._repo.list(
                item_type=None,
                project=None,
                status=None,
                limit=batch_size,
                offset=offset,
            )
            if not batch:
                break
            for item in batch:
                vector = self._embedder.embed(item.raw_text)
                if dim is None:
                    dim = len(vector)
                    self._store.ensure_collection(dim)
                self._store.upsert(str(item.id), vector, self._payload_for(item))
                indexed += 1
            offset += len(batch)
        return indexed

    def _apply_classification(self, item: MemoryItem) -> None:
        """Fill unclassified fields from the classifier; never raises."""
        if item.type is not None:
            return  # caller classified the item explicitly; respect that
        try:
            result = self._classifier.classify(item.raw_text)
        except Exception:
            logger.exception("ingest: classifier failed; item %s stays in inbox", item.id)
            return
        if result["type"] is None:
            return  # classifier fallback: nothing to apply
        item.type = result["type"]
        item.project = result["project"]
        item.importance = result["importance"]
        item.actionability = result["actionability"]
        item.confidence = result["confidence"]
        item.status = route(result["confidence"])

    def _index(self, item: MemoryItem) -> None:
        """Embed and upsert the item; failures defer to ``reindex``."""
        try:
            vector = self._embedder.embed(item.raw_text)
            self._store.ensure_collection(len(vector))
            self._store.upsert(str(item.id), vector, self._payload_for(item))
        except Exception:
            logger.warning(
                "ingest: indexing failed for item %s; it will be indexed by 'brain reindex'",
                item.id,
                exc_info=True,
            )

    @staticmethod
    def _payload_for(item: MemoryItem) -> dict[str, Any]:
        return {
            _PAYLOAD_ID: str(item.id),
            _PAYLOAD_TEXT: item.raw_text,
            _PAYLOAD_SOURCE: item.source,
            _PAYLOAD_PROJECT: item.project,
        }
