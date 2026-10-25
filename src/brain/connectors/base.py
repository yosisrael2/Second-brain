"""Connector contract: the dataclasses, ABC and errors every source implements.

Shared contract of the `connectors` ticket — the names here are BINDING for
all adapter workers (gmail/gcal/gdrive/gtasks); changes go through the
manager only. See the ticket's connectors-contract.md for the full semantics.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, ClassVar


class ConnectorError(Exception):
    """Base class for connector failures.

    ``status`` carries the HTTP status when the error came from a response
    (None otherwise) — e.g. an adapter catches a retryable with status 410
    and returns reset=True for a full resync.
    """

    def __init__(self, message: str, *, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class ConnectorAuthError(ConnectorError):
    """Authentication/authorization failed — needs user re-consent.

    The runner stops the connector and reports; it does not retry.
    """


class ConnectorRetryableError(ConnectorError):
    """Transient failure (429 / 5xx / network) — the runner backs off."""


@dataclass(frozen=True)
class AttachmentRef:
    """A reference to one attachment; bytes are fetched lazily via
    :meth:`Connector.fetch_attachment` (never inlined into ``text``)."""

    ref_id: str  # connector-specific handle passed back to fetch_attachment
    filename: str
    mime_type: str
    size: int | None = None


@dataclass(frozen=True)
class SourceRecord:
    """One normalized record a connector yields for a sync batch.

    ``text`` is rendered plain text that becomes the item's ``raw_text``;
    ``meta`` must be JSON-safe (it lands in ``memory_items.meta``).
    """

    source: str  # connector name: "gmail" | "gcal" | "gdrive" | "gtasks"
    external_id: str  # stable id within the source (message/event/file/task id)
    version: str | None  # etag / updated / modifiedTime — change detection
    title: str
    text: str  # rendered plain text that becomes the item's raw_text
    occurred_at: datetime | None  # tz-aware; event start / email date / task due
    url: str | None = None
    meta: dict[str, Any] = field(default_factory=dict)  # JSON-safe only
    attachments: tuple[AttachmentRef, ...] = ()
    deleted: bool = False  # cancelled event / trashed file / deleted task


@dataclass(frozen=True)
class FetchResult:
    """One batch of records plus the cursor to resume from.

    The runner persists ``next_cursor`` ONLY after the whole batch succeeded
    (at-least-once + idempotent by external_id/version). ``reset=True`` means
    the source invalidated the cursor and this batch is a full resync.
    """

    records: list[SourceRecord]
    next_cursor: str | None
    reset: bool = False
    # Adapter-observed counters for this batch (e.g. gcal's "beyond_window":
    # instances dropped because they start beyond the future window). The
    # runner may surface them in ops detail; tests assert on them directly.
    stats: dict[str, int] = field(default_factory=dict)


class Connector(ABC):
    """A pluggable data source. Adding a source = one subclass + one register."""

    # == SourceRecord.source == the items.source value for records it yields.
    # ClassVar in the contract; adapters set it as a plain class attribute
    # (``name = "gcal"``), which mypy treats as an override-compatible value.
    name: ClassVar[str]

    @abstractmethod
    def fetch(self, cursor: str | None, *, limit: int) -> FetchResult:
        """Fetch the next batch of records after ``cursor``."""
        ...

    def fetch_attachment(self, record: SourceRecord, ref: AttachmentRef) -> bytes:
        """Fetch one attachment's bytes. Sources without attachments skip this."""
        raise NotImplementedError

    def close(self) -> None:
        """Release any held resources (HTTP sessions, credentials)."""
        return None
