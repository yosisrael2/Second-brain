"""FakeConnector + test doubles the connector adapters reuse in their tests.

Everything here is network-free and database-free unless the caller passes
real repos: the runner tests wire these fakes into the REAL IngestService on
the test database, which is what makes them evidence.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

from brain.connectors.base import (
    AttachmentRef,
    Connector,
    ConnectorError,
    FetchResult,
    SourceRecord,
)


class FakeConnector(Connector):
    """Scripted connector: queued errors, then queued batches, then empty.

    ``name`` defaults to "gcal" — a name inside the memory_items.source
    domain — so runner tests can write real rows; adapters pass their own.
    """

    name = "gcal"  # ClassVar default; a ``name=`` argument shadows it per instance

    def __init__(
        self,
        *,
        name: str | None = None,
        batches: list[FetchResult] | None = None,
        errors: list[Exception] | None = None,
        attachments: dict[tuple[str, str], bytes] | None = None,
    ) -> None:
        if name is not None and name != self.name:
            # Per-instance shadow of the ClassVar (fakes only): plain dict
            # assignment keeps ruff B010 and mypy both quiet.
            self.__dict__["name"] = name
        self.cursors: list[str | None] = []
        self.limits: list[int] = []
        self.closed = False
        self._batches = list(batches or [])
        self._errors = list(errors or [])
        self._attachments = dict(attachments or {})

    def fetch(self, cursor: str | None, *, limit: int) -> FetchResult:
        self.cursors.append(cursor)
        self.limits.append(limit)
        if self._errors:
            raise self._errors.pop(0)
        if self._batches:
            return self._batches.pop(0)
        return FetchResult(records=[], next_cursor=cursor)

    def fetch_attachment(self, record: SourceRecord, ref: AttachmentRef) -> bytes:
        try:
            return self._attachments[(record.external_id, ref.ref_id)]
        except KeyError:
            raise ConnectorError(
                f"no fake bytes for {record.external_id}/{ref.ref_id}"
            ) from None

    def close(self) -> None:
        self.closed = True


def make_record(
    external_id: str = "r1",
    *,
    version: str | None = "v1",
    title: str = "Record r1",
    text: str = "body of r1",
    occurred_at: datetime | None = datetime(2026, 10, 1, 9, 0, tzinfo=UTC),
    url: str | None = None,
    meta: dict[str, Any] | None = None,
    attachments: tuple[AttachmentRef, ...] = (),
    deleted: bool = False,
    source: str = "gcal",
) -> SourceRecord:
    return SourceRecord(
        source=source,
        external_id=external_id,
        version=version,
        title=title,
        text=text,
        occurred_at=occurred_at,
        url=url,
        meta=dict(meta or {}),
        attachments=attachments,
        deleted=deleted,
    )


@dataclass
class RecordingIndexer:
    """Stands in for the vector indexer; records what it was asked to do."""

    indexed: list[uuid.UUID] = field(default_factory=list)
    removed: list[uuid.UUID] = field(default_factory=list)

    def index(self, item: Any) -> None:
        self.indexed.append(item.id)

    def remove(self, item_id: uuid.UUID) -> None:
        self.removed.append(item_id)

    def probe(self) -> None:
        return None

    def purge(self) -> None:
        return None

    def index_batch(self, items: Any) -> int:
        self.indexed.extend(item.id for item in items)
        return len(items)


@dataclass
class RecordingEnrichment:
    """Records enqueue() calls (the runner must re-enqueue on update)."""

    enqueued: list[uuid.UUID] = field(default_factory=list)

    def enqueue(self, item: Any) -> None:
        self.enqueued.append(item.id)


@dataclass
class RecordingAttachments:
    """Stands in for the attachments core (capture + submit recording)."""

    captures: list[dict[str, Any]] = field(default_factory=list)
    submitted: list[uuid.UUID] = field(default_factory=list)

    def capture(self, **kwargs: Any) -> Any:
        """Record the call and return an Attachment-like row (``.id``)."""
        self.captures.append(kwargs)
        return SimpleNamespace(id=uuid.uuid4())

    def submit(self, attachment_id: uuid.UUID) -> None:
        self.submitted.append(attachment_id)
