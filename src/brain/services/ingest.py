"""Ingestion use cases: capture, classify, route, and index (Service layer)."""

from __future__ import annotations

import logging
from typing import Any, Protocol

from brain import schemas
from brain.ai.classify import route
from brain.ai.embedder import EmbedderError
from brain.ai.jev import JevError
from brain.ai.vectorstore import VectorStoreError
from brain.db.models import MemoryItem
from brain.services.indexing import ItemIndexerProto
from brain.services.items import ItemsRepositoryProto

logger = logging.getLogger(__name__)


class ClassifierProto(Protocol):
    """Interface IngestService needs from a classifier (Strategy)."""

    def classify(self, text: str) -> dict[str, Any]: ...


class IngestService:
    """Captures memory items; Jev/index failures never block capture.

    Create persists the item first, then (optionally) fills classification
    fields, then indexes the text in the vector store. Classifier or indexing
    failures (:class:`~brain.ai.jev.JevError`,
    :class:`~brain.ai.embedder.EmbedderError`,
    :class:`~brain.ai.vectorstore.VectorStoreError`) are logged and skipped:
    the item stays in the inbox and is picked up later by ``brain reindex``.
    """

    def __init__(
        self,
        repo: ItemsRepositoryProto,
        classifier: ClassifierProto,
        indexer: ItemIndexerProto,
    ) -> None:
        self._repo = repo
        self._classifier = classifier
        self._indexer = indexer

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
        """Full vector rebuild from Postgres; returns the number indexed.

        Purges the existing collection first, so ghost vectors (items deleted
        or missing from Postgres) are removed. Intended for the ``brain
        reindex`` CLI; raises EmbedderError/VectorStoreError on infrastructure
        failure (the CLI turns that into a clean exit code).
        """
        self._indexer.purge()
        offset = 0
        indexed = 0
        while True:
            batch = self._repo.list(
                item_type=None,
                project=None,
                status=None,
                limit=batch_size,
                offset=offset,
            )
            if not batch:
                return indexed
            for item in batch:
                self._indexer.index(item)
                indexed += 1
            offset += len(batch)

    def _apply_classification(self, item: MemoryItem) -> None:
        """Fill unclassified fields from the classifier; never raises."""
        if item.type is not None:
            return  # caller classified the item explicitly; respect that
        try:
            result = self._classifier.classify(item.raw_text)
        except JevError as exc:
            logger.warning("ingest: classifier unavailable (%s); item stays in inbox", exc)
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
        """Index the item; index failures defer to ``reindex`` (never block)."""
        try:
            self._indexer.index(item)
        except (EmbedderError, VectorStoreError) as exc:
            logger.warning(
                "ingest: indexing failed for item %s (%s); it will be indexed by 'brain reindex'",
                item.id,
                exc,
            )
