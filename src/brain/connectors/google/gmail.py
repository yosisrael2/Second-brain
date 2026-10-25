"""Gmail adapter (connectors ticket, C5): the ONE live Gmail sync path.

Replaces the F2 ``EmailPoller`` (deleted): the launchd agent's
``brain gmail-poll`` and ``brain connectors sync email`` both drive THIS
connector through :class:`~brain.connectors.runner.ConnectorRunner`.

Design (per the approved dedupe decision + the connectors contract):

- name = ``"email"``: items.source keeps ``email`` for the existing data
  (the registry aliases ``gmail`` -> ``email``).
- Cursor = the Gmail **historyId**. The first sync (no cursor) seeds the
  idempotency ledger from the old seen table (``email_messages``) and does a
  query-based listing; later syncs walk ``users.history.list`` for the
  configured label. An expired historyId (404) is the cursor-invalid case:
  the fetch falls back to the query listing and returns ``reset=True``.
- Dedupe: Gmail messages are immutable, so records carry ``version=None``
  (the runner treats stored-NULL == incoming-None as unchanged). Already-
  ingested messages never re-ingest: the lazy seed copies the old ledger
  into ``connector_records`` (idempotent, ON CONFLICT DO NOTHING), and the
  ingest-time content fingerprint is the backstop for re-deliveries under a
  new gmail id.
- Labels: every list call goes through :func:`brain.ai.gmail.gmail_list_params`
  (or history.list with ``labelId``) — the label filter is present by
  construction; a message that lost the polled label is a DELETED record.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any, ClassVar

from brain.ai.gmail import (
    GMAIL_SCOPE,
    GmailAuthError,
    GmailError,
    GmailMessage,
    RestGmailClient,
)
from brain.connectors import alias, register
from brain.connectors.base import (
    AttachmentRef,
    ConnectorAuthError,
    ConnectorError,
    ConnectorRetryableError,
    FetchResult,
    SourceRecord,
)
from brain.connectors.google.base import GoogleConnector

logger = logging.getLogger(__name__)


def record_from_message(msg: GmailMessage, *, deleted: bool = False) -> SourceRecord:
    """Map one parsed Gmail message onto a SourceRecord (pure).

    ``text`` is the SAME paste-parse render the old poller captured through
    (:func:`brain.services.paste.parse` with kind hint ``email``): RTL marks
    stripped, forward preambles skipped, header lines preserved — so items
    keep their pre-port shape and the ingest fingerprint matches.
    """
    from zoneinfo import ZoneInfo  # noqa: PLC0415 (lazy: config never imports ai)

    from brain.config import get_settings  # noqa: PLC0415 (lazy: config never imports ai)
    from brain.services.paste import parse

    parsed = parse(
        msg.body_text, kind_hint="email", tz=ZoneInfo(get_settings().timezone)
    )
    chunk = parsed.chunks[0]
    # The old poller's rule: the message instant wins only when the body
    # carries no parsed date of its own.
    occurred_at = msg.internal_date if chunk.occurred_at is None else chunk.occurred_at
    return SourceRecord(
        source="email",
        external_id=msg.id,
        version=None,  # immutable: change detection is the historyId cursor
        title=msg.subject or "",
        text=chunk.raw_text,
        occurred_at=occurred_at,
        url=None,
        meta={
            **chunk.meta,
            "gmail_id": msg.id,
            "rfc_message_id": msg.rfc_message_id,
            "from": msg.sender,
            "subject": msg.subject,
            "attachments": [
                {
                    "attachment_id": a.attachment_id,
                    "filename": a.filename,
                    "mime_type": a.mime_type,
                    "size": a.size,
                }
                for a in msg.attachments
            ],
        },
        attachments=tuple(
            AttachmentRef(
                ref_id=a.attachment_id, filename=a.filename, mime_type=a.mime_type, size=a.size
            )
            for a in msg.attachments
        ),
        deleted=deleted,
    )


def seed_connector_records_from_email_ledger(session: Any) -> int:
    """Copy the old poller's seen-ledger into the connector idempotency ledger.

    One idempotent INSERT..SELECT: every processed message becomes a
    ``connector_records`` row (source='email', external_id=gmail_id,
    version=NULL, item_id) unless the row already exists — so the first
    connector run never re-ingests mail the old poller captured. Called on
    every full resync (cursor=None / reset); re-seeding inserts 0.
    """
    from sqlalchemy import literal, select
    from sqlalchemy.dialects.postgresql import insert as pg_insert

    from brain.db.models import ConnectorRecord, EmailMessage

    stmt = (
        pg_insert(ConnectorRecord)
        .from_select(
            ["source", "external_id", "version", "item_id"],
            select(
                literal("email"),
                EmailMessage.gmail_id,
                literal(None),
                EmailMessage.item_id,
            )
            .where(EmailMessage.gmail_id.isnot(None))
            .distinct(),
        )
        .on_conflict_do_nothing(index_elements=["source", "external_id"])
        .returning(ConnectorRecord.source)
    )
    result = session.execute(stmt)
    session.commit()
    return len(result.all())


def _item_with_rfc(session: Any, rfc_message_id: str) -> Any:
    """The oldest email item carrying this RFC Message-ID (or None)."""
    from sqlalchemy import select

    from brain.db.models import MemoryItem

    return (
        session.execute(
            select(MemoryItem)
            .where(MemoryItem.source == "email")
            .where(MemoryItem.meta["rfc_message_id"].as_string() == rfc_message_id)
            .order_by(MemoryItem.created_at.asc(), MemoryItem.id.asc())
            .limit(1)
        )
        .scalars()
        .first()
    )


def _ledger_row(session: Any, gmail_id: str) -> Any:
    """The ledger row for this gmail id, or None."""
    from sqlalchemy import select

    from brain.db.models import ConnectorRecord

    return (
        session.execute(
            select(ConnectorRecord).where(
                ConnectorRecord.source == "email",
                ConnectorRecord.external_id == gmail_id,
            )
        )
        .scalars()
        .first()
    )


def rfc_backstop_put(session: Any, rfc_message_id: str, gmail_id: str) -> bool:
    """The Message-ID backstop (second-pass review, finding 2).

    The ledger keys on gmail_id; the ingest fingerprint also hashes the
    occurred_at MINUTE, so the same Message-ID re-delivered under a NEW
    gmail id (Gmail re-delivers with a fresh id) escapes both when the
    internal_date differs even slightly. Before create: when an existing
    email item already carries this rfc_message_id, point the new gmail id
    at that item in the ledger (idempotent put) and tell the caller to drop
    the record. Returns True when the backstop caught the message.

    Guard (review round 3, B2): applies ONLY when this gmail_id has NO
    ledger row yet — a known id belongs to the runner (version updates);
    the backstop must never swallow a legitimate re-sync of an
    already-ledgered message.
    """

    from brain.db.models import ConnectorRecord

    if _ledger_row(session, gmail_id) is not None:
        return False  # a known id: the runner owns it (updates/version)
    existing = _item_with_rfc(session, rfc_message_id)
    if existing is None:
        return False
    session.add(
        ConnectorRecord(
            source="email", external_id=gmail_id, version=None, item_id=existing.id
        )
    )
    session.commit()
    return True


def rfc_backstop_report(session: Any, rfc_message_id: str, gmail_id: str) -> bool:
    """READ-ONLY dry-run twin of :func:`rfc_backstop_put` (review B1):
    report what a real sync would ledger, write NOTHING, and never drop the
    record — the dry-run listing must show every record the source yields.

    Same ledger-row-first guard as the write twin (review follow-up 2): a
    known id would be skipped by the real sync's ledger fast-path, so it
    must never get a would-ledger line (noise implying pending work that
    does not exist)."""
    if _ledger_row(session, gmail_id) is not None:
        return False
    existing = _item_with_rfc(session, rfc_message_id)
    if existing is None:
        return False
    print(
        f"  backstop: would ledger {gmail_id} -> existing item "
        f"{existing.id} (same Message-ID {rfc_message_id})"
    )
    return False


def heartbeat_email_summary(summary: Any) -> None:
    """Transitional cockpit heartbeat: the email panel reads component
    'email'; the runner writes 'connector:email'. One adapter here keeps the
    cockpit unchanged until the Sources panel (C6) replaces the panel."""
    from brain import ops  # noqa: PLC0415

    state = {
        "ok": "ok",
        "auth_error": "needs re-auth",
        "retry_error": "error",
        "error": "error",
    }.get(summary.state, summary.state)
    detail = {
        "state": state,
        "listed": summary.fetched,
        "new": summary.created,
        "skipped": summary.skipped,
        "errors": summary.errors,
        "reset": summary.reset,
    }
    try:
        ops.heartbeat("email", detail)
        ops.record_event("email", "poll", ok=state == "ok", detail=dict(detail))
    except Exception:  # noqa: BLE001 (ops must never break polling)
        logger.debug("connectors: email heartbeat raised", exc_info=True)


@register
class GmailConnector(GoogleConnector):
    """Gmail label -> SourceRecords (the email source on the framework)."""

    name: ClassVar[str] = "email"  # == items.source for the existing data
    SCOPES: ClassVar[tuple[str, ...]] = (GMAIL_SCOPE,)
    API_NAME: ClassVar[str] = "gmail"
    API_VERSION: ClassVar[str] = "v1"

    def __init__(
        self,
        *,
        token_loader: Callable[[], str],
        service_factory: Callable[[str, list[str], str, str], Any],
        user_id: str = "me",
        settings: Any = None,
        legacy_seed: Callable[[], int] | None = None,
        is_known: Callable[[str], bool] | None = None,
        rfc_backstop: Callable[[str, str], bool] | None = None,
        client_overrides: dict[str, Any] | None = None,
    ) -> None:
        """``legacy_seed`` runs on every full resync (lazy ledger migration);
        ``is_known`` answers "is this gmail id already ledgered?" so the
        query path can skip the GET for known ids (the old fast path);
        ``rfc_backstop(rfc_message_id, gmail_id)`` is the Message-ID
        backstop: True = the re-delivery was ledgered against the existing
        item, drop the record (never a second item for one Message-ID).
        ``client_overrides`` injects the read client's test seams (sleep/
        clock/max_retries for the pacing + rate-limit retry)."""
        super().__init__(
            token_loader=token_loader, service_factory=service_factory, user_id=user_id
        )
        from brain.config import get_settings  # noqa: PLC0415

        self._settings: Any = settings or get_settings()
        self._legacy_seed = legacy_seed
        self._is_known = is_known
        self._rfc_backstop = rfc_backstop
        self._client_overrides: dict[str, Any] = dict(client_overrides or {})
        self._client: RestGmailClient | None = None

    # --- plumbing -------------------------------------------------------------

    def _gmail(self) -> RestGmailClient:
        """The shared read client over the (cached) service object, wired
        with the configured pacing (quota hygiene) and the test seams."""
        if self._client is None:
            self._client = RestGmailClient(
                service=self._service(),
                min_interval_s=getattr(self._settings, "gmail_min_call_interval_s", None),
                **self._client_overrides,
            )
        return self._client

    def _label_id(self) -> str:
        label = (getattr(self._settings, "gmail_label_id", "") or "").strip()
        if not label:
            raise ConnectorError(
                "GMAIL_LABEL_ID unset — set the immutable label id to poll"
            )
        return label

    @staticmethod
    def _boundary(fn: Callable[[], Any]) -> Any:
        """Map the client's Gmail errors onto the connector taxonomy (the
        status table itself lives once in GoogleConnector._translate_error)."""
        try:
            return fn()
        except GmailAuthError as exc:
            raise ConnectorAuthError(str(exc)) from exc
        except GmailError as exc:
            status = getattr(exc, "status", None)
            if status is not None and (status == 429 or status >= 500):
                raise ConnectorRetryableError(str(exc), status=status) from exc
            raise ConnectorError(str(exc), status=status) from exc

    # --- fetch ------------------------------------------------------------------

    def fetch(self, cursor: str | None, *, limit: int) -> Any:
        if cursor is None:
            self._seed("first sync (no cursor)")
            return self._query_batch(limit)
        try:
            return self._history_batch(cursor, limit)
        except ConnectorError as exc:
            if getattr(exc, "status", None) != 404:
                raise
            # The cursor-invalid case (Gmail: expired historyId -> 404):
            # fall back to the query listing and declare a full resync.
            logger.info(
                "connectors: email history %s expired (%s) — full resync", cursor, exc
            )
            self._seed("history reset")
            return self._query_batch(limit, reset=True)

    def _seed(self, why: str) -> None:
        if self._legacy_seed is None:
            return
        try:
            seeded = self._legacy_seed()
        except Exception as exc:  # noqa: BLE001 (a seed failure must not kill the sync)
            logger.warning("connectors: email legacy seed failed (%s)", exc)
            return
        if seeded:
            logger.info("connectors: email seeded %d legacy ledger row(s) (%s)", seeded, why)

    # --- query listing (first sync / reset) -------------------------------------

    def _query_batch(self, limit: int, *, reset: bool = False) -> Any:

        label = self._label_id()
        client = self._gmail()
        query = getattr(self._settings, "gmail_query", "") or ""
        # The resume point is taken BEFORE the listing: a message that arrives
        # while pages are being read sits AFTER this historyId, so the next
        # sync's history walk re-covers it (re-delivery is idempotent through
        # the ledger). Taken after the listing, the saved cursor would be past
        # arrivals the listing never saw — silently lost mail.
        resume_history_id = self._current_history_id()
        # Bounded listing (quota hygiene): a page of max(limit, 50) stubs —
        # never the whole label just to fill a small --limit.
        page_size = min(500, max(limit, 50))
        records: list[Any] = []
        truncated = False
        page_token: str | None = None
        while True:
            try:
                stubs, page_token = self._boundary(
                    lambda pt=page_token: client.list_messages_page(
                        query=query, label_ids=[label], page_token=pt, max_results=page_size,
                    )
                )
            except ConnectorError as exc:
                if reset and getattr(exc, "status", None) == 404:
                    # history expired AND the label is gone: terminal, not a loop.
                    raise ConnectorError(f"label lookup failed during resync: {exc}") from exc
                raise
            for stub in stubs:
                if len(records) >= limit:
                    truncated = True  # more stubs remain: drop the cursor
                    break
                if self._is_known is not None and self._is_known(stub.id):
                    continue  # fast path: the ledger knows this id — no GET
                try:
                    record = self._record(stub.id)
                except ConnectorError as exc:
                    if getattr(exc, "status", None) == 404:
                        # Listed then hard-deleted before the GET: gone is gone.
                        logger.info("connectors: email message %s vanished (404)", stub.id)
                        continue
                    raise
                if self._backstop_catches(record):
                    continue  # re-delivery under a new id: ledgered, no new item
                records.append(record)
            if truncated or page_token is None:
                break
        next_cursor: str | None = None
        if not truncated:
            # The whole listing fit: the PRE-listing historyId is a safe
            # resume point — the next sync's history walk re-reports anything
            # that arrived during the listing, and the ledger dedupes it.
            next_cursor = resume_history_id
        # more=True when the batch hit the limit: there are more records the
        # source yields, and the resume cursor (None when truncated) cannot
        # express it. The --drain loop and adopt._fetch_all keep going on it.
        return FetchResult(records=records, next_cursor=next_cursor, reset=reset, more=truncated)

    def _current_history_id(self) -> str | None:
        client = self._gmail()
        return self._boundary(lambda: client.profile_history_id())

    # --- history (incremental) ----------------------------------------------------

    def _history_batch(self, start_history_id: str, limit: int) -> Any:

        label = self._label_id()
        client = self._gmail()
        pending: list[tuple[str, bool]] = []  # (gmail_id, deleted) last-state-wins
        emitted_entry: str | None = None  # last FULLY consumed entry id

        def _history_pages() -> list[dict[str, Any]]:
            # Materialize INSIDE the error boundary: the generator body (the
            # API calls) must execute where the Gmail->connector mapping runs.
            return list(client.list_history(start_history_id=start_history_id, label_id=label))

        for page in self._boundary(_history_pages):
            for entry in page.get("history") or []:
                # The limit stops BETWEEN entries only: one coalesced Gmail
                # entry can carry many messages (dragging a folder into the
                # label), so stopping mid-entry would drop the entry's tail
                # AND advance the cursor past it — silent message loss. The
                # batch may therefore exceed the limit by one entry.
                for removed in entry.get("labelsRemoved") or []:
                    if label in (removed.get("labelIds") or []):
                        mid = str((removed.get("message") or {}).get("id"))
                        pending.append((mid, True))
                for added in entry.get("labelsAdded") or []:
                    if label in (added.get("labelIds") or []):
                        mid = str((added.get("message") or {}).get("id"))
                        pending.append((mid, False))
                for _kind in ("messagesDeleted", "messagesTrashed"):
                    for m in entry.get(_kind) or []:
                        # Gmail's History schema WRAPS the message in
                        # 'message' (same as labelsAdded/labelsRemoved):
                        # {"messagesDeleted": [{"message": {"id": ...}}]}.
                        wrapped = (m or {}).get("message") or {}
                        pending.append((str(wrapped.get("id")), True))
                for m in entry.get("messagesAdded") or []:
                    wrapped = (m or {}).get("message") or {}
                    pending.append((str(wrapped.get("id")), False))
                if entry.get("id"):
                    emitted_entry = str(entry.get("id"))
                if len(pending) >= limit:
                    break  # the entry above is fully consumed — safe checkpoint
            if len(pending) >= limit:
                break
        # Last state wins per message (added after removed -> present again).
        final: dict[str, bool] = {}
        for mid, deleted in pending:
            final[mid] = deleted
        records = []
        for mid, deleted in final.items():
            record = self._record(mid, deleted=deleted)
            if self._backstop_catches(record):
                continue  # re-delivery under a new id: ledgered, no new item
            records.append(record)
        return FetchResult(
            records=records,
            next_cursor=emitted_entry or start_history_id,
            reset=False,
        )

    def _backstop_catches(self, record: Any) -> bool:
        """True = the record is a Message-ID re-delivery the ledger now
        covers; the record must NOT reach the runner (no second item).
        Tombstones and rfc-less records pass through untouched; a backstop
        failure is logged and swallowed — it must never kill the sync."""
        if self._rfc_backstop is None or record.deleted:
            return False
        rfc = record.meta.get("rfc_message_id")
        if not rfc:
            return False
        try:
            caught = self._rfc_backstop(str(rfc), str(record.external_id))
        except Exception as exc:  # noqa: BLE001 (backstop must never kill the sync)
            logger.warning(
                "connectors: email Message-ID backstop failed for %s (%s)",
                record.external_id,
                exc,
            )
            return False
        if caught:
            logger.info(
                "connectors: email %s re-delivered under a new id with the same "
                "Message-ID %s — ledgered against the existing item",
                record.external_id,
                rfc,
            )
        return caught

    # --- one message -> record ------------------------------------------------------

    def _record(self, message_id: str, *, deleted: bool = False) -> Any:
        from brain.connectors.base import ConnectorError  # noqa: PLC0415

        client = self._gmail()
        try:
            msg = self._boundary(lambda: client.get_message(message_id))
        except ConnectorError as exc:
            if deleted and getattr(exc, "status", None) == 404:
                # Hard-deleted before we could read it: mark the item deleted
                # with an empty record (it is hidden either way; the ledger
                # bump stops the churn).
                return _tombstone(message_id)
            raise
        # Defence in depth (kept from the old poller): get_message can fetch
        # ANY id — only messages still carrying the polled label may be
        # reported; a removed one is a deletion of the source.
        if not deleted and self._label_id() not in msg.label_ids:
            logger.info("connectors: email %s no longer carries the label", message_id)
            return _tombstone(message_id)
        return record_from_message(msg, deleted=deleted)

    # --- attachments ------------------------------------------------------------------

    def fetch_attachment(self, record: Any, ref: Any) -> bytes:
        client = self._gmail()
        return self._boundary(
            lambda: client.get_attachment(str(record.external_id), str(ref.ref_id))
        )

    def close(self) -> None:
        """Release the cached google service (one service per connector).

        Duck-typed close: the real googleapiclient Resource has .close();
        test fakes may not. The RestGmailClient wrapper shares this service,
        so closing it here is THE single release point — never double-close
        via the client."""
        service = self._service_instance
        if service is None:
            return
        close = getattr(service, "close", None)
        if close is not None:
            close()
        self._service_instance = None  # released: a later use rebuilds
        self._client = None  # the wrapper held the closed service


def _tombstone(message_id: str) -> Any:
    from brain.connectors.base import SourceRecord  # noqa: PLC0415

    return SourceRecord(
        source="email",
        external_id=message_id,
        version=None,
        title="",
        text="",
        occurred_at=None,
        meta={"gmail_id": message_id, "tombstone": True},
        deleted=True,
    )


# The contract: items.source stays "email" for the existing data; the
# registry also answers to "gmail" (the docs/UI name) via the alias.
alias("gmail", "email")
