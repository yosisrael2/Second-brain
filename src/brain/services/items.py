"""Memory item use cases (Service layer). Framework-free business logic."""

from __future__ import annotations

import uuid
from typing import Protocol

from brain import schemas
from brain.db.enums import ItemStatus, ItemType
from brain.db.models import MemoryItem


class ItemNotFound(Exception):
    """Raised when a memory item id does not exist."""

    def __init__(self, item_id: uuid.UUID) -> None:
        super().__init__(str(item_id))
        self.item_id = item_id


class ItemsRepositoryProto(Protocol):
    """Interface the service needs from a repository (Dependency Inversion)."""

    def add(self, item: MemoryItem) -> MemoryItem: ...

    def get(self, item_id: uuid.UUID) -> MemoryItem | None: ...

    def list(
        self,
        *,
        item_type: ItemType | None,
        project: str | None,
        status: ItemStatus | None,
        limit: int,
        offset: int,
    ) -> list[MemoryItem]: ...

    def search(self, *, query: str, limit: int, offset: int) -> list[MemoryItem]: ...

    def delete(self, item: MemoryItem) -> None: ...

    def commit(self) -> None: ...

    def refresh(self, item: MemoryItem) -> None: ...


class ItemsService:
    """Orchestrates memory item use cases on top of an ItemsRepository."""

    def __init__(self, repo: ItemsRepositoryProto) -> None:
        self._repo = repo

    def create(self, data: schemas.ItemCreate) -> MemoryItem:
        """Create a new memory item from the given payload."""
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
        self._repo.commit()
        return item

    def get(self, item_id: uuid.UUID) -> MemoryItem:
        """Return the item with the given id."""
        return self._require(item_id)

    def update(self, item_id: uuid.UUID, data: schemas.ItemUpdate) -> MemoryItem:
        """Apply a partial update and return the refreshed item."""
        item = self._require(item_id)
        for field, value in data.model_dump(exclude_unset=True).items():
            setattr(item, field, value)
        self._repo.commit()  # commit flushes the pending UPDATE
        self._repo.refresh(item)  # reload server-generated updated_at
        return item

    def list(
        self,
        *,
        item_type: ItemType | None,
        project: str | None,
        status: ItemStatus | None,
        limit: int,
        offset: int,
    ) -> list[MemoryItem]:
        """List items newest-first, optionally filtered by type/project/status."""
        return self._repo.list(
            item_type=item_type,
            project=project,
            status=status,
            limit=limit,
            offset=offset,
        )

    def search(self, *, query: str, limit: int, offset: int) -> list[MemoryItem]:
        """Full-text keyword search over raw_text, best match first."""
        return self._repo.search(query=query, limit=limit, offset=offset)

    def delete(self, item_id: uuid.UUID) -> None:
        """Delete the item with the given id."""
        item = self._require(item_id)
        self._repo.delete(item)
        self._repo.commit()

    def _require(self, item_id: uuid.UUID) -> MemoryItem:
        item = self._repo.get(item_id)
        if item is None:
            raise ItemNotFound(item_id)
        return item
