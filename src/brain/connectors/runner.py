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
                            re-enqueue enrichment — unless the USER edited the
                            text since the framework wrote it (detected via
                            the stored ``connector_text_sha``): their edit
                            survives; only the version marker moves.
- deleted=True           -> NEVER hard-delete and NEVER write content: only
                            the marks move (meta ``source_deleted=true``;
                            hidden via the PR-B self-supersede mechanism, set
                            only when no real relation strike exists; proposed
                            extraction children hide with the parent, kept
                            children are flagged). If the source un-deletes
                            later, exactly those marks are reversed.
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

import hashlib
import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
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
from brain.connectors.state import QUARANTINE_AFTER
from brain.db.models import MemoryItem
from brain.services.dedupe import content_fingerprint

logger = logging.getLogger(__name__)

_BACKOFF_BASE_S = 5.0
_BACKOFF_CAP_S = 600.0
# Attachments larger than the core's cap are recorded and skipped (the same
# policy as the email poller) — never a record failure.
from brain.services.attachments import MAX_FILE_BYTES  # noqa: E402


def _text_sha(text: str) -> str:
    """sha256 of the framework-written text (user-edit marker; NOT the text)."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def backoff_seconds(
    attempt: int, *, base_s: float = _BACKOFF_BASE_S, cap_s: float = _BACKOFF_CAP_S
) -> float:
    """Exponential backoff after ``attempt`` consecutive retryable failures."""
    return min(base_s * (2 ** max(attempt, 0)), cap_s)


@dataclass(frozen=True)
class SyncSummary:
    """One run's outcome (also the ops heartbeat payload)."""

    connector: str
    state: str = "ok"  # ok | auth_error | retry_error | error | backoff
    fetched: int = 0
    created: int = 0
    # create() returned an EXISTING item via the content-fingerprint dedupe
    # (ingest's deduplicated snapshot): the ledger points at the existing
    # item, nothing new was captured. Counted separately so ops/cockpit
    # captures are not overstated (worker_6 found the overcount on #59).
    deduped: int = 0
    updated: int = 0
    deleted: int = 0
    skipped: int = 0
    errors: int = 0
    reset: bool = False
    cursor_saved: bool = False
    error: str | None = None
    # state == "backoff" only: seconds until the persisted backoff window
    # (backoff_seconds(attempt) since the last retryable error) lets the
    # connector fetch again. None for every other state.
    retry_in_s: float | None = None


class _IngestProto(Protocol):
    def create(self, data: Any, classify: bool = True) -> MemoryItem: ...


class _ItemsRepoProto(Protocol):
    def get(self, item_id: Any) -> MemoryItem | None: ...

    def commit(self) -> None: ...

    def refresh(self, item: MemoryItem) -> None: ...

    def children_of(self, item_id: Any) -> Sequence[MemoryItem]: ...


class _StateRepoProto(Protocol):
    def get_or_create(self, name: str) -> Any: ...

    def attempt(self, name: str) -> int:
        """The persisted consecutive-retryable-failure counter."""

        ...

    def record_success(self, name: str, *, cursor: str | None, at: datetime) -> None: ...

    def record_error(self, name: str, *, error: str, at: datetime, retryable: bool) -> None: ...


class _RecordRepoProto(Protocol):
    def get(self, source: str, external_id: str) -> Any: ...

    def put(self, source: str, external_id: str, version: str | None, item_id: Any) -> None: ...

    def mark_failure(
        self, source: str, external_id: str, version: str | None, error: str
    ) -> bool: ...

    def clear_failure(self, source: str, external_id: str) -> None: ...


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
        # Enforce the PERSISTED backoff (the counter record_error/record_success
        # already maintain): a connector still inside its retryable-failure
        # window is not fetched at all — the pass just reports the skip and
        # moves on. Without this, every scheduler pass (launchd agent, --drain,
        # a loop) re-polls a sick source at full rate no matter how often it
        # has just failed. The window only DELAYS: past it, the run proceeds.
        attempt = self._state.attempt(name)
        if attempt > 0 and state.last_error is not None and state.last_run_at is not None:
            now = self._now()
            window = backoff_seconds(attempt)
            remaining = window - (now - state.last_run_at).total_seconds()
            if remaining > 0:
                logger.info(
                    "connector %s: backing off %.1fs more (attempt %d, last error: %s)",
                    name,
                    remaining,
                    attempt,
                    state.last_error,
                )
                return SyncSummary(
                    connector=name,
                    state="backoff",
                    retry_in_s=remaining,
                )
        try:
            result = self._connector.fetch(state.cursor, limit=limit)
        except ConnectorAuthError as exc:
            return self._fail(name, "auth_error", f"auth (needs re-consent): {exc}")
        except ConnectorRetryableError as exc:
            return self._fail(name, "retry_error", f"retryable: {exc}", retryable=True)
        except ConnectorError as exc:
            return self._fail(name, "error", str(exc))

        created = updated = deleted = skipped = deduped = errors = 0
        for record in result.records:
            try:
                outcome = self._handle(record)
            except Exception as exc:  # noqa: BLE001 — one bad record holds the cursor
                detail = f"{type(exc).__name__}: {exc}"[:200]
                if self._records.mark_failure(name, record.external_id, record.version, detail):
                    # 3rd consecutive failure: QUARANTINED. Not an error any
                    # more — the batch's cursor may advance past it; the
                    # record stays in the ledger until the version bumps.
                    skipped += 1
                    ops.record_event(
                        f"connector:{name}",
                        "quarantine",
                        ok=False,
                        detail={
                            "external_id": record.external_id,
                            "error": detail,
                            "fail_count": QUARANTINE_AFTER,
                        },
                    )
                    logger.warning(
                        "connectors: %s record %s quarantined after %d failures: %s",
                        name,
                        record.external_id,
                        QUARANTINE_AFTER,
                        detail,
                    )
                else:
                    errors += 1
                    ops.record_event(
                        f"connector:{name}",
                        "record",
                        ok=False,
                        detail={"external_id": record.external_id, "error": detail},
                    )
                    logger.warning(
                        "connectors: %s record %s failed: %s",
                        name,
                        record.external_id,
                        exc,
                        exc_info=True,
                    )
                continue
            if outcome == "created":
                created += 1
            elif outcome == "deduped":
                deduped += 1
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
            deduped=deduped,
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
        """Apply one record; returns created|deduped|updated|deleted|skipped."""
        name = self._connector.name
        row = self._records.get(name, record.external_id)
        item = self._repo.get(row.item_id) if row is not None and row.item_id else None
        if row is not None and row.item_id is None and row.fail_count == 0:
            # USER-DELETED: the item was deleted out-of-band and the ledger's
            # SET NULL kept the row. Deleting is a user decision — never
            # re-capture this record. (A poison record also has item_id NULL
            # but fail_count > 0 — it falls through to the retry below.)
            return "skipped"

        if row is not None and row.status == "failed":
            # QUARANTINED: not attempted, not an error — the cursor advances.
            if record.version == row.version:
                return "skipped"
            # The source bumped the version: one more chance.
            self._records.clear_failure(name, record.external_id)
            row = self._records.get(name, record.external_id)

        # ONE clamp site: ItemCreate rejects text longer than the core's
        # raw_text limit, so a long record would fail ingest, hold the cursor
        # and retry forever. Both the create and the update path use the
        # clamped text from here on.
        record, truncated_from = self._clamp_text(record)

        if row is None or item is None:
            # Unknown record — or a race (the item vanished between the two
            # reads): the sync treats it as new. The ledger put below upserts,
            # so no duplicate row can appear.
            if record.deleted:
                return "skipped"  # never create items for already-deleted records
            item = self._ingest.create(
                schemas.ItemCreate(
                    raw_text=record.text,
                    source=name,
                    occurred_at=record.occurred_at,
                    meta=self._framework_meta(record, truncated_from=truncated_from),
                )
            )
            self._capture_attachments(record, item)
            self._records.put(name, record.external_id, record.version, item.id)
            if item.deduplicated:
                # ingest's fingerprint dedupe returned the EXISTING item (the
                # snapshot carries its id + the deduplicated flag): the ledger
                # above already points at it. NOT a new capture — count it as
                # its own outcome so created stays honest.
                return "deduped"
            return "created"

        stored_meta = dict(item.meta or {})
        already_deleted = bool(stored_meta.get("source_deleted"))
        if record.version == row.version and record.deleted == already_deleted:
            return "skipped"

        if record.deleted:
            # DELETION touches ONLY the marks + ledger version. Delete
            # payloads carry no content (Drive: text=''; cancelled events:
            # id+status) — writing them would wipe the user's item, re-index
            # empty text and burn an enrichment slot on a hidden item.
            if already_deleted:
                self._records.put(name, record.external_id, record.version, item.id)
                return "skipped"
            return self._apply_deletion(record, item, row)

        # Content change or un-delete: update what the source owns, in place
        # — unless the USER edited the text since the framework wrote it.
        # Detection: the framework stores a sha256 of its last-written text;
        # a hash mismatch means the user owns the text now (same rule as the
        # extraction re-run: user-acted items are never overwritten). Only
        # the version marker moves, so this bump is not re-reported forever.
        user_edited = stored_meta.get("connector_text_sha") != _text_sha(item.raw_text)
        new_meta = {
            **stored_meta,
            **self._framework_meta(record, truncated_from=truncated_from),
        }
        if already_deleted:
            # Source un-deleted: reversal applies regardless of who owns the
            # text — a hidden item the source restored must be visible again.
            new_meta = {k: v for k, v in new_meta.items() if k != "source_deleted"}
            self._reverse_deleted_marks(item)
        item.meta = new_meta
        if not user_edited:
            item.raw_text = record.text
            item.occurred_at = record.occurred_at
            item.fingerprint = content_fingerprint(item.raw_text, item.occurred_at, item.source)
        self._repo.commit()
        self._repo.refresh(item)
        self._records.put(name, record.external_id, record.version, item.id)
        if not user_edited:
            # The content actually changed: re-index + re-enqueue. A user-
            # edited item only got a marker refresh — nothing to index.
            self._index(item)
            self._enqueue(item)
            return "updated"
        return "skipped"

    def _apply_deletion(self, record: SourceRecord, item: MemoryItem, row: Any) -> str:
        """Mark ONE record deleted: meta mark + hide + ledger version. Nothing
        else — no content write, no re-index, no enrichment.

        Hiding reuses the PR-B mechanism (superseded_by): every default
        listing/search/occurrence path and the semantic leg (services/search.py
        _load_items) already filter it. The self-strike is set ONLY when the
        item is not already struck by a REAL relation (superseded_by = a newer
        item) — overwriting it would break relation undo; on un-delete only
        the framework's own self-strike is cleared.

        Children (review bug 4): the parent's PROPOSED extraction children
        hide with the same mechanism; KEPT children stay visible (the user
        acted) but get meta parent_source_deleted=true. Un-delete reverses
        exactly that.
        """
        item.meta = {
            **dict(item.meta or {}),
            "version": record.version,
            "source_deleted": True,
        }
        if item.superseded_by is None:
            item.superseded_by = item.id
        for child in self._repo.children_of(item.id):
            if child.child_state == "dropped":
                continue  # never askable anyway
            child_meta = dict(child.meta or {})
            if child.child_state == "proposed":
                if child.superseded_by is None:
                    child.superseded_by = child.id
            child_meta["parent_source_deleted"] = True
            child.meta = child_meta
        self._repo.commit()
        self._repo.refresh(item)
        self._records.put(self._connector.name, record.external_id, record.version, item.id)
        return "deleted"

    def _reverse_deleted_marks(self, item: MemoryItem) -> None:
        """Undo exactly what :meth:`_apply_deletion` did to ``item`` and its
        children — only the framework's OWN marks (a real relation's strike
        and the user's actions are never touched)."""
        if item.superseded_by == item.id:
            item.superseded_by = None
        for child in self._repo.children_of(item.id):
            child_meta = dict(child.meta or {})
            if "parent_source_deleted" not in child_meta:
                continue  # we never touched this child
            child_meta.pop("parent_source_deleted")
            if child.child_state == "proposed" and child.superseded_by == child.id:
                child.superseded_by = None
            child.meta = child_meta

    def _clamp_text(self, record: SourceRecord) -> tuple[SourceRecord, int | None]:
        """Clamp record.text to the core's raw_text limit (the ONE clamp site).

        Returns the (possibly replaced) record and the original length when
        truncation happened (None otherwise). The sha marker is computed on
        the CLAMPED text by _framework_meta, so a long item does not look
        user-edited on every later sync.
        """
        limit = schemas.RAW_TEXT_MAX_LENGTH
        if len(record.text) <= limit:
            return record, None
        return replace(record, text=record.text[:limit]), len(record.text)

    def _framework_meta(
        self, record: SourceRecord, truncated_from: int | None = None
    ) -> dict[str, Any]:
        """The one place item meta is built (SIMPLIFY target: single site).

        ``connector_text_sha`` is the sha256 of the last text the framework
        wrote — the update path compares hashes to detect a USER edit (never
        overwrite the item's text again) without duplicating the full text
        into JSONB.
        """
        meta: dict[str, Any] = {
            "connector": self._connector.name,
            "external_id": record.external_id,
            "version": record.version,
            "url": record.url,
            "connector_text_sha": _text_sha(record.text),
        }
        if truncated_from is not None:
            meta["text_truncated"] = True
            meta["original_length"] = truncated_from
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

    def _fail(self, name: str, state: str, message: str, *, retryable: bool = False) -> SyncSummary:
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
                    "deduped": summary.deduped,
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
            logger.warning("connectors: enrichment enqueue failed for item %s (%s)", item.id, exc)
