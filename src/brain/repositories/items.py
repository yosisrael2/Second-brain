"""Repository for MemoryItem persistence (Repository pattern)."""

from __future__ import annotations

import uuid

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from brain.db.enums import FTS_LANGUAGE, ItemStatus, ItemType
from brain.db.models import MemoryItem


class ItemsRepository:
    """All data access for MemoryItem; one instance per request.

    Each mutating method flushes and refreshes but leaves the transaction
    open; callers commit via :meth:`commit` so multi-step use cases stay
    atomic.
    """

    def __init__(self, db: Session) -> None:
        self._db = db

    def add(self, item: MemoryItem) -> MemoryItem:
        """Persist a new item and return it with server-generated fields."""
        self._db.add(item)
        self._flush_and_refresh(item)
        return item

    def get(self, item_id: uuid.UUID) -> MemoryItem | None:
        """Return the item with the given id, or None if absent."""
        return self._db.get(MemoryItem, item_id)

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
        stmt = select(MemoryItem).order_by(MemoryItem.created_at.desc())
        if item_type is not None:
            stmt = stmt.where(MemoryItem.type == item_type)
        if project is not None:
            stmt = stmt.where(MemoryItem.project == project)
        if status is not None:
            stmt = stmt.where(MemoryItem.status == status)
        return list(self._db.scalars(stmt.limit(limit).offset(offset)))

    def search(self, *, query: str, limit: int, offset: int) -> list[MemoryItem]:
        """Full-text search over raw_text via Postgres FTS, best match first."""
        tsquery = func.websearch_to_tsquery(FTS_LANGUAGE, query)
        stmt = (
            select(MemoryItem)
            .where(MemoryItem.search_vector.op("@@")(tsquery))
            .order_by(
                func.ts_rank(MemoryItem.search_vector, tsquery).desc(),
                MemoryItem.created_at.desc(),
            )
            .limit(limit)
            .offset(offset)
        )
        return list(self._db.scalars(stmt))

    def delete(self, item: MemoryItem) -> None:
        """Remove the given item."""
        self._db.delete(item)
        self._db.flush()

    def commit(self) -> None:
        """Commit the current transaction."""
        self._db.commit()

    def refresh(self, item: MemoryItem) -> None:
        """Reload server-generated fields (e.g. updated_at) from the DB."""
        self._db.refresh(item)

    def _flush_and_refresh(self, item: MemoryItem) -> None:
        self._db.flush()
        self._db.refresh(item)
