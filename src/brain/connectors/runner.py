"""ConnectorRunner: the generic sync loop every data source shares.

SEMANTICS (the connectors contract; adapters depend on these verbatim):

- new external_id        -> create an item via the standard capture path
                            (IngestService.create: classify, index, enqueue),
                            source = connector name, meta gets the framework
                            keys {"connector", "external_id", "version",
                            "url"} merged under the record's own meta.
- known id, same version -> skip (idempotent; re-running never duplicates).
- known id, new version  -> update the item in place (text, occurred_at,
                            meta), recompute the fingerprint, re-index,
                            re-enqueue enrichment. The user's own fields
                            (status, project, importance, ...) are never
                            touched — only what the source owns.
- deleted=True           -> NEVER hard-delete. The item stays, meta gains
                            ``source_deleted=true`` and it is hidden from
                            retrieval exactly like PR-B-superseded items:
                            ``superseded_by`` is set to the item's own id,
                            which every list/search/occurrence path already
                            filters. If the source un-deletes it later, the
                            marks are cleared.
- cursor                 -> persisted ONLY after the whole batch succeeded.
                            Any record failure leaves the cursor untouched,
                            so the next run re-fetches the window and skips
                            the records it already wrote (at-least-once,
                            idempotent by external_id/version).
- reset=True             -> the source invalidated the cursor (full resync):
                            the runner just upserts what came back. It NEVER
                            infers deletions from records absent from a
                            batch — deletion is always explicit via
                            ``record.deleted``.
- errors                 -> ConnectorAuthError stops the run immediately
                            (user must re-consent; no retry loop). A
                            ConnectorRetryableError records a backoff
                            attempt (``backoff_seconds``); any other
                            ConnectorError is terminal for the run.
- heartbeat              -> one ops heartbeat per run per connector
                            (component ``connector:<name>``), the same
                            pattern as the email poller's, so /ops/health
                            and the cockpit can show it.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol

from brain import ops, schemas
from brain.connectors.base import (
    Connector,
    ConnectorAuthError,
    ConnectorError,
    ConnectorRetryableError,
    SourceRecord,
)
from brain.db.models import MemoryItem
from brain.services.dedupe import content_fingerprint

logger = logging.getLogger(__name__)

_BACKOFF_BASE_S = 5.0
_BACKOFF_CAP_S = 600.0
# Attachments larger than the core's cap are recorded and skipped (the same
# policy as the email poller) — never a record failure.
from brain.services.attachments import MAX_FILE_BYTES  # noqa: E402


def backoff_seconds(
    attempt: int, *, base_s: float = _BACKOFF_BASE_S, cap_s: float = _BACKOFF_CAP_S
) -> float:
    """Exponential backoff after ``attempt`` consecutive retryable failures."""
    return min(base_s * (2 ** max(attempt, 0)), cap_s)


@dataclass(frozen=True)
class SyncSummary:
    """One run's outcome (also the ops heartbeat payload)."""

    connector: str
    state: str = "ok"  # ok | auth_error | retry_error | error
    fetched: int = 0
    created: int = 0
    updated: int = 0
    deleted: int = 0
    skipped: int = 0
    errors: int = 0
    reset: bool = False
    cursor_saved: bool = False
    error: str | None = None


class _IngestProto(Protocol):
    def create(self, data: Any, classify: bool = True) -> MemoryItem: ...


class _ItemsRepoProto(Protocol):
    def get(self, item_id: Any) -> MemoryItem | None: ...

    def commit(self) -> None: ...

    def refresh(self, item: MemoryItem) -> None: ...


class _StateRepoProto(Protocol):
    def get_or_create(self, name: str) -> Any: ...

    def record_success(self, name: str, *, cursor: str | None, at: datetime) -> None: ...

    def record_error(
        self, name: str, *, error: str, at: datetime, retryable: bool
    ) -> None: ...


class _RecordRepoProto(Protocol):
    def get(self, source: str, external_id: str) -> Any: ...

    def put(self, source: str, external_id: str, version: str | None, item_id: Any) -> None: ...


class _IndexerProto(Protocol):
    def index(self, item: MemoryItem) -> None: ...


class _EnrichmentProto(Protocol):
    def enqueue(self, item: MemoryItem) -> Any: ...


class ConnectorRunner:
    """Runs one connector: fetch a batch, apply the semantics, persist the cursor.

    The runner owns the loop, the ledger and the failure policy; connectors
    only produce :class:`~brain.connectors.base.FetchResult` batches.
    """

    def __init__(
        self,
        connector: Connector,
        *,
        ingest: _IngestProto,
        repo: _ItemsRepoProto,
        state: _StateRepoProto,
        records: _RecordRepoProto,
        indexer: _IndexerProto | None = None,
        enrichment: Any | None = None,
        attachments: Any | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._connector = connector
        self._ingest = ingest
        self._repo = repo
        self._state = state
        self._records = records
        self._indexer = indexer
        self._enrichment = enrichment
        self._attachments = attachments
        self._now = clock or (lambda: datetime.now(UTC))

    def run(self, *, limit: int = 200) -> SyncSummary:
        name = self._connector.name
        state = self._state.get_or_create(name)
        try:
            result = self._connector.fetch(state.cursor, limit=limit)
        except ConnectorAuthError as exc:
            return self._fail(name, "auth_error", f"auth (needs re-consent): {exc}")
        except ConnectorRetryableError as exc:
            return self._fail(name, "retry_error", f"retryable: {exc}", retryable=True)
        except ConnectorError as exc:
            return self._fail(name, "error", str(exc))

        created = updated = deleted = skipped = errors = 0
        for record in result.records:
            try:
                outcome = self._handle(record)
            except Exception as exc:  # noqa: BLE001 — one bad record holds the cursor
                errors += 1
                logger.warning(
                    "connectors: %s record %s failed: %s",
                    name,
                    record.external_id,
                    exc,
                    exc_info=True,
                )
                ops.record_event(
                    f"connector:{name}",
                    "record",
                    ok=False,
                    detail={
                        "external_id": record.external_id,
                        "error": f"{type(exc).__name__}: {exc}"[:200],
                    },
                )
                continue
            if outcome == "created":
                created += 1
            elif outcome == "updated":
                updated += 1
            elif outcome == "deleted":
                deleted += 1
            else:
                skipped += 1

        summary = SyncSummary(
            connector=name,
            fetched=len(result.records),
            created=created,
            updated=updated,
            deleted=deleted,
            skipped=skipped,
            errors=errors,
            reset=result.reset,
        )
        if errors == 0:
            # Whole batch succeeded: only now may the cursor move.
            self._state.record_success(name, cursor=result.next_cursor, at=self._now())
            summary = SyncSummary(**{**summary.__dict__, "cursor_saved": True})
        else:
            self._state.record_error(
                name,
                error=f"batch incomplete: {errors} record(s) failed",
                at=self._now(),
                retryable=True,
            )
        self._heartbeat(summary)
        return summary

    # --- per-record semantics -------------------------------------------------

    def _handle(self, record: SourceRecord) -> str:
        """Apply one record; returns created|updated|deleted|skipped."""
        name = self._connector.name
        row = self._records.get(name, record.external_id)
        item = self._repo.get(row.item_id) if row is not None else None
        if row is not None and item is None:
            # The item was deleted out-of-band (CASCADE removed the ledger
            # row already; a stale in-session row is dropped here): re-sync
            # treats this record as new.
            row = None

        if row is None or item is None:
            # Unknown record, or the item was deleted out-of-band (the
            # ledger's CASCADE would normally have removed the row too): the
            # sync treats it as new.
            if record.deleted:
                return "skipped"  # never create items for already-deleted records
            item = self._ingest.create(
                schemas.ItemCreate(
                    raw_text=record.text,
                    source=name,
                    occurred_at=record.occurred_at,
                    meta=self._framework_meta(record),
                )
            )
            self._capture_attachments(record, item)
            self._records.put(name, record.external_id, record.version, item.id)
            return "created"

        stored_meta = dict(item.meta or {})
        already_deleted = bool(stored_meta.get("source_deleted"))
        if record.version == row.version and record.deleted == already_deleted:
            return "skipped"

        # Changed (or un-deleted): update what the source owns, in place.
        item.raw_text = record.text
        item.occurred_at = record.occurred_at
        item.meta = {**stored_meta, **self._framework_meta(record)}
        item.fingerprint = content_fingerprint(item.raw_text, item.occurred_at, item.source)
        if record.deleted and not already_deleted:
            item.meta = {**item.meta, "source_deleted": True}
            # PR B mechanism: self-supersede hides the item from every default
            # listing/search/occurrence path (repositories/items.py) and from
            # the semantic leg (services/search.py _load_items).
            item.superseded_by = item.id
        elif not record.deleted and already_deleted:
            item.meta = {k: v for k, v in item.meta.items() if k != "source_deleted"}
            item.superseded_by = None
        self._repo.commit()
        self._repo.refresh(item)
        self._records.put(name, record.external_id, record.version, item.id)
        if self._indexer is not None:
            self._index(item)
        if self._enrichment is not None:
            self._enqueue(item)
        return "deleted" if record.deleted else "updated"

    def _framework_meta(self, record: SourceRecord) -> dict[str, Any]:
        """The one place item meta is built (SIMPLIFY target: single site)."""
        meta: dict[str, Any] = {
            "connector": self._connector.name,
            "external_id": record.external_id,
            "version": record.version,
            "url": record.url,
        }
        meta.update(record.meta)  # the record's own keys win
        return meta

    def _capture_attachments(self, record: SourceRecord, item: MemoryItem) -> None:
        """Fetch + store attachments through the existing attachments pipeline.

        A failed fetch_attachment fails the RECORD (cursor held) — silently
        dropping bytes while advancing the cursor would lose the attachment
        for good. Oversize refs are recorded and skipped instead (same
        policy as the email poller).
        """
        if not record.attachments:
            return

        for ref in record.attachments:
            if ref.size is not None and ref.size > MAX_FILE_BYTES:
                ops.record_event(
                    f"connector:{self._connector.name}",
                    "attachment_skip_too_large",
                    ok=False,
                    detail={
                        "external_id": record.external_id,
                        "filename": ref.filename,
                        "size": ref.size,
                    },
                )
                continue
            content = self._connector.fetch_attachment(record, ref)
            if self._attachments is None:
                # Refs without an attachments core are still recorded in meta
                # (same behavior as the email source pre-F3); never a failure.
                continue
            att = self._attachments.capture(
                parent_item_id=item.id,
                content=content,
                filename=ref.filename,
                mime=ref.mime_type,
                source=self._connector.name,
                source_ref=record.external_id,
            )
            self._attachments.submit(att.id)

    # --- failure + ops ---------------------------------------------------------

    def _fail(
        self, name: str, state: str, message: str, *, retryable: bool = False
    ) -> SyncSummary:
        self._state.record_error(name, error=message, at=self._now(), retryable=retryable)
        summary = SyncSummary(connector=name, state=state, error=message)
        self._heartbeat(summary)
        ops.record_event(f"connector:{name}", "run", ok=False, detail={"error": message[:200]})
        return summary

    def _heartbeat(self, summary: SyncSummary) -> None:
        try:
            ops.heartbeat(
                f"connector:{summary.connector}",
                {
                    "state": summary.state,
                    "created": summary.created,
                    "updated": summary.updated,
                    "deleted": summary.deleted,
                    "skipped": summary.skipped,
                    "errors": summary.errors,
                    "reset": summary.reset,
                    "last_error": summary.error,
                },
            )
        except Exception:  # noqa: BLE001 — ops must never break syncing
            logger.debug("connectors: ops.heartbeat raised", exc_info=True)

    def _index(self, item: MemoryItem) -> None:
        """Re-index after an update; index failures defer to 'brain reindex'."""
        if self._indexer is None:
            return
        try:
            self._indexer.index(item)
        except Exception as exc:  # noqa: BLE001 — never fail the run on indexing
            logger.warning(
                "connectors: re-index failed for item %s (%s); deferred to 'brain reindex'",
                item.id,
                exc,
            )

    def _enqueue(self, item: MemoryItem) -> None:
        if self._enrichment is None:
            return
        try:
            self._enrichment.enqueue(item)
        except Exception as exc:  # noqa: BLE001 — never fail the run on enrichment
            logger.warning(
                "connectors: enrichment enqueue failed for item %s (%s)", item.id, exc
            )
