"""Repository for MemoryItem persistence (Repository pattern)."""

from __future__ import annotations

import re
import uuid
from collections.abc import Sequence
from datetime import datetime

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from brain.db.enums import FTS_LANGUAGE, ItemStatus, ItemType
from brain.db.models import MemoryItem


def _escape_like(needle: str) -> str:
    """Neutralise LIKE wildcards in user/entity-derived needles."""
    return re.sub(r"([\\%_])", r"\\\1", needle)


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

    def get_by_fingerprint(self, fingerprint: str) -> MemoryItem | None:
        """Return the first item carrying this content fingerprint, or None.

        Oldest first: on (theoretically possible) multiple matches the
        original capture wins.
        """
        stmt = (
            select(MemoryItem)
            .where(MemoryItem.fingerprint == fingerprint)
            .order_by(MemoryItem.created_at.asc(), MemoryItem.id.asc())
            .limit(1)
        )
        return self._db.scalars(stmt).first()

    def get_many(self, item_ids: Sequence[uuid.UUID]) -> Sequence[MemoryItem]:
        """Return the items with the given ids, missing ones silently skipped."""
        if not item_ids:
            return []
        stmt = select(MemoryItem).where(MemoryItem.id.in_(item_ids))
        return list(self._db.scalars(stmt))

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
        stmt = select(MemoryItem).order_by(MemoryItem.created_at.desc(), MemoryItem.id.desc())
        if item_type is not None:
            stmt = stmt.where(MemoryItem.type == item_type)
        if project is not None:
            stmt = stmt.where(MemoryItem.project == project)
        if status is not None:
            stmt = stmt.where(MemoryItem.status == status)
        return list(self._db.scalars(stmt.limit(limit).offset(offset)))

    def search(self, *, query: str, limit: int, offset: int) -> Sequence[MemoryItem]:
        """Full-text search over raw_text via Postgres FTS, best match first."""
        tsquery = func.websearch_to_tsquery(FTS_LANGUAGE, query)
        stmt = (
            select(MemoryItem)
            .where(MemoryItem.search_vector.op("@@")(tsquery))
            .order_by(
                func.ts_rank(MemoryItem.search_vector, tsquery).desc(),
                MemoryItem.created_at.desc(),
                MemoryItem.id.desc(),
            )
            .limit(limit)
            .offset(offset)
        )
        return list(self._db.scalars(stmt))

    def by_occurrence(
        self, *, start: datetime, end: datetime, limit: int
    ) -> Sequence[MemoryItem]:
        """Every item with occurred_at inside [start, end), soonest first.

        Drives temporal retrieval: day/date questions fetch ALL events in the
        resolved range (top-k semantic search cannot express "everything on
        Tuesday"). Items without occurred_at are naturally excluded.
        """
        stmt = (
            select(MemoryItem)
            .where(MemoryItem.occurred_at >= start)
            .where(MemoryItem.occurred_at < end)
            .order_by(MemoryItem.occurred_at.asc(), MemoryItem.id.desc())
            .limit(limit)
        )
        return list(self._db.scalars(stmt))

    def by_text_any(self, needles: Sequence[str], *, limit: int) -> Sequence[MemoryItem]:
        """Items whose raw_text contains ANY needle (case-insensitive), newest first.

        Drives the kid-recall leg of /ask: a captured WhatsApp message keeps
        its chat name in the raw_text header ('[date, time] <chat> / <sender>:
        body'), so the exact channel name as stored in memory matches the
        items captured from that channel. Needles are user/entity data, so
        LIKE wildcards in them are escaped, never interpreted.
        """
        if not needles:
            return []
        clauses = [
            MemoryItem.raw_text.ilike(f"%{_escape_like(needle)}%", escape="\\")
            for needle in needles
        ]
        stmt = (
            select(MemoryItem)
            .where(or_(*clauses))
            .order_by(MemoryItem.created_at.desc(), MemoryItem.id.desc())
            .limit(limit)
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
