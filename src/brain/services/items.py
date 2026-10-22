"""Memory item use cases (Service layer). Framework-free business logic."""

from __future__ import annotations

import logging
import uuid
from collections.abc import Mapping, Sequence
from typing import Protocol

from brain import schemas
from brain.ai.embedder import EmbedderError
from brain.ai.vectorstore import VectorStoreError
from brain.db.enums import ItemStatus, ItemType
from brain.db.models import MemoryItem
from brain.services.indexing import ItemIndexerProto

logger = logging.getLogger(__name__)

# Payload fields mirrored into the vector index; changing any of them on
# PATCH triggers a re-index so vectors never go stale.
_INDEXED_FIELDS = frozenset({"raw_text", "source", "project"})


class ItemNotFound(Exception):
    """Raised when a memory item id does not exist."""

    def __init__(self, item_id: uuid.UUID) -> None:
        super().__init__(str(item_id))
        self.item_id = item_id


class ItemsRepositoryProto(Protocol):
    """Interface the service needs from a repository (Dependency Inversion)."""

    def add(self, item: MemoryItem) -> MemoryItem: ...

    def get(self, item_id: uuid.UUID) -> MemoryItem | None: ...

    def get_by_fingerprint(self, fingerprint: str) -> MemoryItem | None: ...

    def get_many(self, item_ids: Sequence[uuid.UUID]) -> Sequence[MemoryItem]: ...

    def list(
        self,
        *,
        item_type: ItemType | None,
        project: str | None,
        status: ItemStatus | None,
        limit: int,
        offset: int,
    ) -> Sequence[MemoryItem]: ...

    def search(self, *, query: str, limit: int, offset: int) -> Sequence[MemoryItem]: ...

    def delete(self, item: MemoryItem) -> None: ...

    def commit(self) -> None: ...

    def refresh(self, item: MemoryItem) -> None: ...


class ItemsService:
    """Orchestrates memory item use cases on top of an ItemsRepository.

    ``indexer`` is optional for backwards compatibility; when present, deletes
    remove the item's vector and PATCHes touching indexed fields re-index it.
    Index failures never fail the request: they are logged and the item is
    picked up by the next ``brain reindex``.

    Creation lives in :class:`~brain.services.ingest.IngestService` (capture +
    classify + index in one use case); this service covers the rest.
    """

    def __init__(self, repo: ItemsRepositoryProto, indexer: ItemIndexerProto | None = None) -> None:
        self._repo = repo
        self._indexer = indexer

    def get(self, item_id: uuid.UUID) -> MemoryItem:
        """Return the item with the given id."""
        return self._require(item_id)

    def update(self, item_id: uuid.UUID, data: schemas.ItemUpdate) -> MemoryItem:
        """Apply a partial update and return the refreshed item."""
        item = self._require(item_id)
        changes = data.model_dump(exclude_unset=True)
        for field, value in changes.items():
            setattr(item, field, value)
        self._repo.commit()  # commit flushes the pending UPDATE
        self._repo.refresh(item)  # reload server-generated updated_at
        self._reindex_if_indexed_fields_changed(item, changes)
        return item

    def _reindex_if_indexed_fields_changed(
        self, item: MemoryItem, changes: Mapping[str, object]
    ) -> None:
        """Re-index the item when a PATCH touches vector payload fields."""
        if self._indexer is None or not _INDEXED_FIELDS.intersection(changes):
            return
        try:
            self._indexer.index(item)
        except (EmbedderError, VectorStoreError) as exc:
            logger.warning(
                "items: re-indexing failed for item %s (%s); deferred to 'brain reindex'",
                item.id,
                exc,
            )

    def list(
        self,
        *,
        item_type: ItemType | None,
        project: str | None,
        status: ItemStatus | None,
        limit: int,
        offset: int,
    ) -> Sequence[MemoryItem]:
        """List items newest-first, optionally filtered by type/project/status."""
        return self._repo.list(
            item_type=item_type,
            project=project,
            status=status,
            limit=limit,
            offset=offset,
        )

    def search(self, *, query: str, limit: int, offset: int) -> Sequence[MemoryItem]:
        """Full-text keyword search over raw_text, best match first."""
        return self._repo.search(query=query, limit=limit, offset=offset)

    def delete(self, item_id: uuid.UUID) -> None:
        """Delete the item with the given id (and its vector, when indexed)."""
        item = self._require(item_id)
        self._repo.delete(item)
        self._repo.commit()
        if self._indexer is None:
            return
        try:
            self._indexer.remove(item_id)
        except VectorStoreError as exc:
            logger.warning(
                "items: vector removal failed for item %s (%s); deferred to 'brain reindex'",
                item_id,
                exc,
            )

    def _require(self, item_id: uuid.UUID) -> MemoryItem:
        item = self._repo.get(item_id)
        if item is None:
            raise ItemNotFound(item_id)
        return item
