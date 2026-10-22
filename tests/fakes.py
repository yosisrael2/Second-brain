"""Shared test fakes for the wiring tests (no external services)."""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

from brain.db.enums import Actionability, ItemType
from brain.db.models import MemoryItem


class FakeRepository:
    """In-memory ItemsRepositoryProto implementation."""

    def __init__(self) -> None:
        self.items: dict[uuid.UUID, MemoryItem] = {}

    def add(self, item: MemoryItem) -> MemoryItem:
        if item.id is None:
            item.id = uuid.uuid4()
        now = datetime.now(UTC)
        if item.created_at is None:
            item.created_at = now
        if item.updated_at is None:
            item.updated_at = now
        self.items[item.id] = item
        return item

    def get(self, item_id: uuid.UUID) -> MemoryItem | None:
        return self.items.get(item_id)

    def get_by_fingerprint(self, fingerprint: str) -> MemoryItem | None:
        matches = sorted(
            (item for item in self.items.values() if item.fingerprint == fingerprint),
            key=lambda item: (item.created_at, item.id),
        )
        return matches[0] if matches else None

    def get_many(self, item_ids: Sequence[uuid.UUID]) -> list[MemoryItem]:
        return [self.items[item_id] for item_id in item_ids if item_id in self.items]

    def list(
        self,
        *,
        item_type: Any = None,
        project: str | None = None,
        status: Any = None,
        limit: int,
        offset: int,
    ) -> list[MemoryItem]:
        items = [
            item
            for item in self.items.values()
            if (item_type is None or item.type == item_type)
            and (project is None or item.project == project)
            and (status is None or item.status == status)
        ]
        items.sort(key=lambda item: item.created_at, reverse=True)
        return items[offset : offset + limit]

    def search(self, *, query: str, limit: int, offset: int) -> list[MemoryItem]:
        needle = query.lower()
        matches = [item for item in self.items.values() if needle in item.raw_text.lower()]
        matches.sort(key=lambda item: item.created_at, reverse=True)
        return matches[offset : offset + limit]

    def by_occurrence(self, *, start, end, limit: int) -> list[MemoryItem]:
        """Items whose occurred_at falls inside [start, end), soonest first."""
        matches = [
            item
            for item in self.items.values()
            if item.occurred_at is not None and start <= item.occurred_at < end
        ]
        matches.sort(key=lambda item: (item.occurred_at, item.id))
        return matches[:limit]

    def delete(self, item: MemoryItem) -> None:
        self.items.pop(item.id, None)

    def commit(self) -> None:
        pass

    def refresh(self, item: MemoryItem) -> None:
        pass


class StubClassifier:
    """Classifier stub returning a canned result or raising."""

    def __init__(
        self, result: dict[str, Any] | None = None, error: Exception | None = None
    ) -> None:
        self._result = result
        self._error = error
        self.calls: list[str] = []

    def classify(self, text: str) -> dict[str, Any]:
        self.calls.append(text)
        if self._error is not None:
            raise self._error
        return dict(self._result or {})


FULL_CLASSIFICATION: dict[str, Any] = {
    "type": ItemType.TASK,
    "project": None,
    "importance": 4,
    "actionability": Actionability.NOW,
    "confidence": 0.95,
}
