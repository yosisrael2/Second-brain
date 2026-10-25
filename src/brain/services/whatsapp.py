"""WhatsApp source (PR D1): poll the bridge store, group conversations, capture.

Data flow: launchd runs the WhatsApp bridge (~/git/whatsapp-mcp), which writes
``store/messages.db`` (SQLite: chats + messages). This module READS that store
read-only (``file:...?mode=ro`` + busy timeout — the bridge writes
concurrently and we never write to it, never call /api/send, never touch the
whatsmeow credential store ``whatsapp.db``).

Allowlist (design delta item 34): only chats whose display name is a channel
entity in memory (``entities.kind == 'channel'``) are ingested; any other chat
is invisible to the poller.

Conversations (item 35): messages are grouped per channel by a TIME GAP ONLY
(default 60 minutes, configurable). The bridge schema has NO reply-to /
quoted-id column, so reply chains are NOT available — grouping never infers
replies from message text. The conversation, not the single message, is the
source item: one ``memory_items`` row with ``source='whatsapp'`` whose
raw_text names the channel and carries a per-message ``[dd/mm/yyyy, hh:mm]
Sender: text`` line (WhatsApp-paste format, re-parseable by
brain.services.paste). While the conversation is still open (the gap has not
passed since its last message), new messages UPDATE that same item in place:
lines are appended and meta/fingerprint follow. After the gap, a new
conversation (and item) starts.

Media (item 47, wired in F3): when the attachments core and a media client
are wired, every fresh media message in an ALLOWLISTED channel is downloaded
via the bridge's local REST ``POST /api/download`` (read-only from our side:
never ``/api/send``), captured as its own attachment item (source=
'whatsapp', parent = the conversation item) and read through the daily
budget. The conversation line's "attachment not read yet" marker is then
replaced with a link to the attachment item. Without the wiring (or after a
failed download) the marker stays — media is still recorded, just not read.
Oversized files (> 20 MB) are skipped BEFORE any download.

Timestamps (item 41): the bridge stores Go time.Time strings carrying the
machine's local offset (+03:00/+02:00 across DST). Every timestamp is parsed
to an aware UTC instant; wall-clock display converts to the configured
``Settings.timezone`` via ZoneInfo — never a fixed offset. The per-chat
cursor is stored as a UTC instant in OUR database; polls re-read a small
overlap window (must be < the gap) and dedupe on message ids, so restarts
never duplicate conversations.

Triage/extraction (item 36) and reconciliation (item 37) are NOT part of D1:
captured conversations carry no classification calls; they stay untyped in
the inbox for PR D2.
"""

from __future__ import annotations

import logging
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Protocol
from uuid import UUID
from zoneinfo import ZoneInfo

from brain import ops
from brain.db.enums import ItemStatus
from brain.db.models import MemoryItem, WhatsAppPollState
from brain.repositories.items import ItemsRepository
from brain.repositories.memory import MemoryRepository
from brain.repositories.whatsapp import WhatsAppStateRepository
from brain.services.dedupe import content_fingerprint
from brain.services.triage import TrierProto

logger = logging.getLogger(__name__)

SOURCE_WHATSAPP = "whatsapp"
DEFAULT_GAP_MINUTES = 60
DEFAULT_OVERLAP_MINUTES = 5
_NOT_READ_YET = " — attachment not read yet"
# A media message renders as `[dd/mm/yyyy, hh:mm] Sender: [{type}: {file}{_NOT_READ_YET}]`
# when it carries NO caption (see _message_line). This is the ONLY line shape
# that means "unread media with nothing readable": a caption before the bracket
# or any plain text fails the match, and a READ marker (attachment <id>) fails
# it too, so the skip releases the moment the media is read.
_UNREAD_MEDIA_LINE = re.compile(
    r"^\[\d{2}/\d{2}/\d{4}, \d{2}:\d{2}\] .{1,80}?: \[[^:\]]+: [^\]]*attachment not read yet\]$"
)


def is_unread_media_only(raw_text: str) -> bool:
    """True when every message line is an UNREAD media marker — no caption,
    no text: there is nothing readable to triage yet. Labelling a filename is
    how media-only conversations landed NOISE on zero content.

    Scaffolding (the header line, blank lines) is ignored; any readable line
    (plain text, or a caption before the marker) makes this False — a
    captioned image is a MIXED conversation and gets triaged at capture.
    Pure over the rendered text (no meta needed), so both triage paths — the
    poller's _triage_item AND the backfill command, which calls the trier
    directly — can check it before invoking any model.
    """
    saw_message = False
    for line in raw_text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("[WhatsApp · "):
            continue
        saw_message = True
        if not _UNREAD_MEDIA_LINE.match(stripped):
            return False
    return saw_message


def user_acted_on_item(item: MemoryItem) -> bool:
    """True when the user touched this item — the post-read re-triage must
    never overwrite their state (it fires only on machine-triaged inbox items).

    Signals (db/models.py): status left INBOX (filed/needs_review; default
    INBOX on capture), superseded_by set (user versioning via supersedes/
    corrects relations), child_state kept/dropped (user review actions on
    extracted children), or a triage label WITHOUT a triage method —
    apply_triage (repositories/items.py) always writes label+method together,
    so a label with no method was written by something outside triage.
    """
    if item.status != ItemStatus.INBOX:
        return True
    if item.superseded_by is not None:
        return True
    if item.child_state in ("kept", "dropped"):
        return True
    return item.triage_label is not None and item.triage_method is None
# Offsets are bounded by ±14h (± Leo threshold); a lower SQL bound this far
# below the wanted window keeps the string comparison safe across DST changes
# (bridge timestamps carry varying offsets, so raw string comparison cannot
# be trusted near the switch).
_MAX_OFFSET = timedelta(hours=14)


class PollerError(Exception):
    """Raised for configuration errors of the WhatsApp poller."""


# --- bridge store (read-only) --------------------------------------------------


def _parse_bridge_timestamp(value: str, naive_zone: ZoneInfo) -> datetime:
    """Parse a bridge TIMESTAMP string into an aware UTC instant.

    Go writes ``YYYY-MM-DD HH:MM:SS±HH:MM``; Python's ``fromisoformat`` reads
    it directly. A naive value (should not happen) is interpreted in the
    configured zone, never as a fixed offset (item 41).
    """
    parsed = datetime.fromisoformat(value.strip())
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=naive_zone)
    return parsed.astimezone(UTC)


@dataclass(frozen=True)
class BridgeMessage:
    """One row of the bridge ``messages`` table (content untouched)."""

    id: str
    chat_jid: str
    sender: str
    content: str
    timestamp_utc: datetime
    is_from_me: bool
    media_type: str | None
    filename: str | None
    file_length: int | None


class BridgeStore:
    """Read-only access to the bridge's messages.db (never writes)."""

    def __init__(self, path: str | Path, *, naive_zone: str = "UTC") -> None:
        self._path = Path(path).expanduser()
        self._naive_zone = ZoneInfo(naive_zone)

    @property
    def path(self) -> Path:
        """The store path (safe to display; never the contents)."""
        return self._path

    def _connect(self) -> sqlite3.Connection:
        # mode=ro: SQLite refuses writes at the connection level; timeout is
        # the busy timeout — the bridge writes concurrently (WAL/rollback).
        conn = sqlite3.connect(f"file:{self._path}?mode=ro", uri=True, timeout=5.0)
        conn.execute("PRAGMA busy_timeout = 5000")
        return conn

    def chats(self) -> dict[str, str]:
        """jid -> display name for every chat the bridge knows."""
        conn = self._connect()
        try:
            rows = conn.execute("SELECT jid, name FROM chats").fetchall()
        finally:
            conn.close()
        return {jid: name or "" for jid, name in rows}

    def resolve_allowlist(self, names: set[str]) -> list[tuple[str, str]]:
        """(jid, name) pairs for chats whose name is an allowlisted channel.

        Deterministic (sorted by jid). Several jids may share a display name;
        each becomes its own cursor. Unmatched allowlist entries are skipped.
        """
        allow = set(names)
        matches = [(jid, name) for jid, name in self.chats().items() if name in allow]
        return sorted(matches)

    def messages_since(self, chat_jid: str, since_utc: datetime) -> list[BridgeMessage]:
        """Messages strictly after ``since_utc`` (aware UTC), oldest first.

        The SQL lower bound is padded by the maximum timezone offset because
        the stored strings carry varying offsets; exact filtering happens in
        Python on parsed instants.
        """
        floor = (since_utc - _MAX_OFFSET).strftime("%Y-%m-%d %H:%M:%S")
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT id, chat_jid, sender, content, timestamp, is_from_me,"
                " media_type, filename, file_length FROM messages"
                " WHERE chat_jid = ? AND timestamp >= ?"
                " ORDER BY timestamp ASC, id ASC",
                (chat_jid, floor),
            ).fetchall()
        finally:
            conn.close()
        messages = [
            BridgeMessage(
                id=str(row[0]),
                chat_jid=str(row[1]),
                sender=str(row[2] or ""),
                content=str(row[3] or ""),
                timestamp_utc=_parse_bridge_timestamp(str(row[4]), self._naive_zone),
                is_from_me=bool(row[5]),
                media_type=row[6],
                filename=row[7],
                file_length=row[8],
            )
            for row in rows
        ]
        return [m for m in messages if m.timestamp_utc > since_utc]

    def latest_timestamp(self, chat_jid: str) -> datetime | None:
        """The chat's newest message instant (UTC), or None for an empty chat."""
        conn = self._connect()
        try:
            rows = [
                str(row[0])
                for row in conn.execute(
                    "SELECT timestamp FROM messages WHERE chat_jid = ?", (chat_jid,)
                ).fetchall()
            ]
        finally:
            conn.close()
        if not rows:
            return None
        return max(_parse_bridge_timestamp(value, self._naive_zone) for value in rows)


# --- grouping + rendering (pure) -------------------------------------------------


def group_conversations(
    messages: list[BridgeMessage], *, gap_minutes: int = DEFAULT_GAP_MINUTES
) -> list[list[BridgeMessage]]:
    """Split ordered messages into conversations by TIME GAP ONLY (item 35).

    The bridge schema has no reply-to/quoted-id column, so reply chains are
    unavailable; a new conversation starts when the gap since the previous
    message exceeds ``gap_minutes`` (exactly gap_minutes stays together).
    Input order does not matter: messages are sorted by (instant, id).
    """
    gap = timedelta(minutes=gap_minutes)
    ordered = sorted(messages, key=lambda m: (m.timestamp_utc, m.id))
    groups: list[list[BridgeMessage]] = []
    for message in ordered:
        if groups and message.timestamp_utc - groups[-1][-1].timestamp_utc <= gap:
            groups[-1].append(message)
        else:
            groups.append([message])
    return groups


def _message_line(message: BridgeMessage, zone: ZoneInfo) -> str:
    """One rendered line: [dd/mm/yyyy, hh:mm] Sender: body (WhatsApp-paste format)."""
    local = message.timestamp_utc.astimezone(zone)
    body = message.content or ""
    if message.media_type:
        body = f"{body} [{message.media_type}: {message.filename}{_NOT_READ_YET}]".strip()
    return f"[{local.strftime('%d/%m/%Y, %H:%M')}] {message.sender}: {body}"


def _message_meta(message: BridgeMessage, zone: ZoneInfo) -> dict[str, Any]:
    media: dict[str, Any] | None = None
    if message.media_type:
        media = {
            "media_type": message.media_type,
            "filename": message.filename,
            "file_length": message.file_length,
            "message_id": message.id,
            "status": "not read yet",
        }
    return {
        "id": message.id,
        "sender": message.sender,
        "at": message.timestamp_utc.isoformat(),
        "at_local": message.timestamp_utc.astimezone(zone).isoformat(),
        "is_from_me": message.is_from_me,
        "media": media,
    }


def render_conversation(
    channel: str,
    chat_jid: str,
    messages: list[BridgeMessage],
    *,
    zone_name: str,
    gap_minutes: int = DEFAULT_GAP_MINUTES,
) -> tuple[str, dict[str, Any]]:
    """Render a conversation as (raw_text, meta) for one source item.

    raw_text names the channel in a header line and carries one
    ``[dd/mm/yyyy, hh:mm] Sender: text`` line per message in the configured
    zone (item 41). meta records the channel, the chat jid, the conversation
    window and every message (ids, senders, instants, media references).
    """
    zone = ZoneInfo(zone_name)
    ordered = sorted(messages, key=lambda m: (m.timestamp_utc, m.id))
    lines = [f"[WhatsApp · {channel}]"]
    lines.extend(_message_line(message, zone) for message in ordered)
    meta = {
        "kind": "whatsapp_conversation",
        "channel": channel,
        "chat_jid": chat_jid,
        "gap_minutes": gap_minutes,
        "first_message_at": ordered[0].timestamp_utc.isoformat(),
        "last_message_at": ordered[-1].timestamp_utc.isoformat(),
        "messages": [_message_meta(message, zone) for message in ordered],
    }
    return "\n".join(lines), meta


class ItemIndexerProto(Protocol):
    """The bit of the indexer the poller needs (failures never block capture)."""

    def index(self, item: MemoryItem) -> bool: ...


class MediaDownloadError(Exception):
    """The bridge media download failed, returned a bad payload, or the
    returned path is outside the bridge store root — refused, never stored."""


class WhatsAppMediaClient:
    """F3 (item 47): fetch media through the bridge's local REST download.

    The REAL bridge contract (whatsapp-bridge/main.go ``/api/download``): it
    downloads the media to its own store dir and responds application/json
    ``DownloadMediaResponse {success, message, filename, path}`` — NEVER the
    bytes (HTTP 500 + ``success:false`` on failure). This client parses that
    JSON, requires ``success``, then VALIDATES the path: realpath must lie
    inside the bridge store root (derived from ``WHATSAPP_BRIDGE_DB``'s
    directory) and must be a regular file; symlinks resolving outside are
    refused. Only then are the bytes read. Anything else raises
    ``MediaDownloadError`` — refused, never stored.

    Read-only from our side, structurally: the client exposes ONLY
    ``download`` — there is no send capability on this class at all, and the
    private CDN url / media keys from the store are never copied anywhere.
    """

    def __init__(
        self,
        base_url: str = "http://127.0.0.1:8085",
        timeout_s: float = 30.0,
        store_root: str | Path | None = None,
        transport: Any | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._timeout_s = timeout_s
        self._store_root = Path(store_root).expanduser().resolve() if store_root else None
        self._transport = transport

    def download(self, message_id: str, chat_jid: str) -> bytes:
        import httpx  # noqa: PLC0415

        client = httpx.Client(transport=self._transport) if self._transport else httpx.Client()
        try:
            response = client.post(
                f"{self._base_url}/api/download",
                json={"message_id": message_id, "chat_jid": chat_jid},
                timeout=self._timeout_s,
            )
            try:
                payload = response.json()
            except ValueError:
                payload = None
            if isinstance(payload, dict):
                # The real bridge answers failure as 500 + {success: false,
                # message}; prefer its message over a bare HTTP error.
                if not payload.get("success"):
                    message = str(payload.get("message") or "unknown error")
                    raise MediaDownloadError(f"bridge download failed: {message[:200]}")
            elif response.is_error:
                response.raise_for_status()  # non-JSON HTTP error
            else:
                raise MediaDownloadError("bridge download returned non-JSON response")
            path = payload.get("path")
            if not path:
                raise MediaDownloadError("bridge response carries no 'path'")
            resolved = Path(str(path)).expanduser().resolve()
            if self._store_root is None:
                raise MediaDownloadError(
                    "no bridge store root configured — refusing to read downloads"
                )
            if resolved != self._store_root and self._store_root not in resolved.parents:
                raise MediaDownloadError(
                    f"refusing path outside the bridge store root: {str(path)[:200]}"
                )
            if not resolved.is_file():
                raise MediaDownloadError(
                    f"downloaded media is not a regular file: {str(path)[:200]}"
                )
            return resolved.read_bytes()
        finally:
            client.close()


# --- poller -----------------------------------------------------------------------


@dataclass(frozen=True)
class ChannelPollResult:
    """Outcome of one poll for one chat."""

    chat_jid: str
    channel: str
    seen: int
    created: int
    updated: int
    cursor_utc: datetime | None
    # F3: (message, parent conversation item id) for every fresh media message.
    media: list[tuple[BridgeMessage, UUID]] = field(default_factory=list)


class WhatsAppPoller:
    """Reads the bridge store into per-conversation source items.

    Idempotence: the per-chat cursor (UTC instant, persisted in
    ``whatsapp_poll_state``) plus an overlap window (``overlap_minutes``, must
    be strictly smaller than the gap) is re-read on every poll; message ids
    already present in the open conversation's item are skipped, so a restart
    (fresh poller instance reading the cursor from our DB) never duplicates
    conversations. A brand-new channel starts at the live edge — history is
    NOT backfilled (opt-in via the CLI ``--since``).

    Classification/extraction is deliberately NOT called (item 36 lands in
    PR D2); conversations are stored untyped in the inbox.
    """

    def __init__(
        self,
        *,
        bridge: BridgeStore,
        state: WhatsAppStateRepository,
        items: ItemsRepository,
        memory: MemoryRepository,
        indexer: ItemIndexerProto | None = None,
        trier: TrierProto | None = None,
        zone_name: str,
        gap_minutes: int = DEFAULT_GAP_MINUTES,
        overlap_minutes: int = DEFAULT_OVERLAP_MINUTES,
        backfill_since: datetime | None = None,
        commit: bool = False,
        attachments: Any | None = None,
        media_client: Any | None = None,
    ) -> None:
        if overlap_minutes >= gap_minutes:
            raise PollerError(
                f"overlap ({overlap_minutes}m) must be smaller than the gap ({gap_minutes}m); "
                "otherwise closed conversations could be re-read and duplicated"
            )
        self._bridge = bridge
        self._state = state
        self._items = items
        self._memory = memory
        self._indexer = indexer
        self._trier = trier
        self._zone = ZoneInfo(zone_name)
        self._zone_name = zone_name
        self._gap = timedelta(minutes=gap_minutes)
        self._overlap = timedelta(minutes=overlap_minutes)
        self._gap_minutes = gap_minutes
        # First-sight cursor override (CLI --since): ingest history from this
        # instant instead of the live edge. Applied once per channel; the
        # persisted cursor takes over afterwards.
        self._backfill_since = backfill_since
        self._commit = commit
        # F3 (item 47): optional attachments core + bridge media client. Both
        # must be present for media downloads; without them media messages
        # keep the plain "not read yet" marker (previous behaviour).
        self._attachments = attachments
        self._media = media_client

    def poll(self, *, now: datetime) -> list[ChannelPollResult]:
        """Poll every allowlisted channel once; per-channel failures are logged, not raised."""
        # Fresh kid names once per pass (never per conversation — no extra
        # query in the hot loop). The refresh is savepoint-scoped inside the
        # trier, so a DB hiccup cannot poison this pass's transaction; on
        # failure the pass carries on with the LAST-KNOWN names.
        refresh = getattr(self._trier, "refresh", None)
        if callable(refresh):
            try:
                refresh()
            except Exception as exc:  # noqa: BLE001 — a failed refresh never stops the pass
                logger.warning(
                    "whatsapp: kid-name refresh failed (%s); using last-known names", exc
                )
                ops.record_event(
                    "triage",
                    "refresh_failed",
                    ok=False,
                    detail={"error": type(exc).__name__, "reason": str(exc)[:120]},
                )
        started = datetime.now(UTC)
        allow = {
            entity.name for entity in self._memory.list_entities(kinds=["channel"]) if entity.name
        }
        results: list[ChannelPollResult] = []
        totals = {"seen": 0, "created": 0, "updated": 0}
        for jid, name in self._bridge.resolve_allowlist(allow):
            try:
                result = self._poll_channel(jid, name or jid, now=now)
            except Exception as exc:  # noqa: BLE001 — one bad channel never stops the poll
                logger.warning("whatsapp: poll failed for %s (%s)", jid, exc)
                if self._commit:
                    # Discard this channel's partial writes so a later
                    # channel's commit cannot persist them half-done.
                    self._items.rollback()
                ops.record_event(
                    "whatsapp",
                    "poll_channel_error",
                    ok=False,
                    detail={"chat_jid": jid, "error": type(exc).__name__},
                )
                continue
            results.append(result)
            totals["seen"] += result.seen
            totals["created"] += result.created
            totals["updated"] += result.updated
        if self._commit:
            self._items.commit()
        # F3 (item 47): media collection runs AFTER the conversation items are
        # committed — the attachment row carries a real FK to its parent. Only
        # allowlisted channels are in ``results`` by construction, so media in
        # any other chat is never fetched.
        if self._attachments is not None and self._media is not None:
            self._collect_media(results)
        latency_ms = round((datetime.now(UTC) - started).total_seconds() * 1000, 1)
        ops.record_event(
            "whatsapp",
            "poll",
            ok=True,
            latency_ms=latency_ms,
            detail={**totals, "channels": len(results)},
        )
        return results

    def _poll_channel(self, jid: str, name: str, *, now: datetime) -> ChannelPollResult:
        """One channel: read the overlap window, update/create conversations, advance the cursor.

        All writes (items + cursor) share one session and commit together, so
        a crash anywhere simply leaves the old cursor: the next poll re-reads
        the same window and dedupes on message ids.
        """
        state = self._state.get_or_create(jid, channel_name=name)
        if state.cursor_utc is None:
            # Brand-new channel: live edge (no history backfill) unless an
            # explicit --since backfill was requested. history_until_utc marks
            # everything at/below the edge as deliberately skipped so the
            # overlap window never re-fetches it.
            edge = self._backfill_since or self._bridge.latest_timestamp(jid)
            self._state.record_poll(
                state,
                now=now,
                cursor_utc=edge,
                history_until_utc=None if self._backfill_since else edge,
            )
            return ChannelPollResult(
                chat_jid=jid, channel=name, seen=0, created=0, updated=0, cursor_utc=edge
            )

        fetched = self._bridge.messages_since(jid, state.cursor_utc - self._overlap)
        open_item = self._open_item(state)
        known_ids = self._known_message_ids(open_item)
        history_until = state.history_until_utc
        fresh = [
            message
            for message in fetched
            if message.id not in known_ids
            and (history_until is None or message.timestamp_utc > history_until)
        ]

        created = 0
        updated = 0
        cursor = state.cursor_utc
        open_id: UUID | None = state.open_item_id
        set_open = False
        media_refs: list[tuple[BridgeMessage, UUID]] = []
        if fresh:
            cursor = fresh[-1].timestamp_utc
            # Leading messages whose chain of gaps stays within the limit
            # continue the open conversation (update in place); the rest
            # start new conversations.
            idx = 0
            if open_item is not None:
                open_last = self._open_last_instant(open_item)
                if open_last is not None:
                    previous = open_last
                    while idx < len(fresh) and fresh[idx].timestamp_utc - previous <= self._gap:
                        previous = fresh[idx].timestamp_utc
                        idx += 1
                if idx:
                    self._append_to_conversation(open_item, fresh[:idx])
                    updated = 1
                    media_refs.extend(
                        (message, open_item.id) for message in fresh[:idx] if message.media_type
                    )
            remaining = fresh[idx:]
            for group in group_conversations(remaining, gap_minutes=self._gap_minutes):
                item = self._create_conversation(name, jid, group)
                open_id = item.id
                set_open = True
                created += 1
                media_refs.extend((message, item.id) for message in group if message.media_type)
        self._state.record_poll(
            state,
            now=now,
            cursor_utc=cursor,
            last_message_at=fresh[-1].timestamp_utc if fresh else None,
            seen=len(fresh),
            created=created,
            updated=updated,
            open_item_id=open_id,
            set_open_item=set_open,
        )
        return ChannelPollResult(
            chat_jid=jid,
            channel=name,
            seen=len(fresh),
            created=created,
            updated=updated,
            cursor_utc=cursor,
            media=media_refs,
        )

    def _open_item(self, state: WhatsAppPollState) -> MemoryItem | None:
        if state.open_item_id is None:
            return None
        return self._items.get(state.open_item_id)

    # --- F3 media ------------------------------------------------------------

    def _collect_media(self, results: list[ChannelPollResult]) -> None:
        """Fetch, capture and link every fresh media message (item 47).

        Per-message failures are recorded, never raised: a broken download
        must not lose the conversation or stop the other channels.
        """
        touched: set[UUID] = set()
        for result in results:
            for message, parent_id in result.media:
                attachment_item_id = self._handle_media(
                    message, parent_id, self._attachments, self._media
                )
                if attachment_item_id is not None:
                    touched.add(parent_id)
                    self._replace_media_marker(parent_id, message, attachment_item_id)
            if touched:
                self._items.commit()
                touched.clear()
        try:
            self._attachments.drain()
        except Exception as exc:  # noqa: BLE001 — reading never fails the poll
            ops.record_event(
                "whatsapp",
                "attachment_drain_error",
                ok=False,
                detail={"error": f"{type(exc).__name__}: {exc}"[:200]},
            )

    def _handle_media(
        self, message: BridgeMessage, parent_id: UUID, attachments: Any, media: Any
    ) -> UUID | None:
        """Download + capture one media message; returns the attachment item id."""
        from brain.services.attachments import MAX_FILE_BYTES

        if message.file_length is not None and message.file_length > MAX_FILE_BYTES:
            ops.record_event(
                "whatsapp",
                "media_skip_too_large",
                ok=False,
                detail={
                    "message_id": message.id,
                    "filename": message.filename,
                    "size": message.file_length,
                },
            )
            return None
        try:
            content = media.download(message.id, message.chat_jid)
        except Exception as exc:  # noqa: BLE001 — recorded, never silent
            ops.record_event(
                "whatsapp",
                "media_download_error",
                ok=False,
                detail={
                    "message_id": message.id,
                    "error": f"{type(exc).__name__}: {exc}"[:200],
                },
            )
            return None
        try:
            filename = message.filename or f"{message.media_type}-{message.id}"
            att = attachments.capture(
                parent_item_id=parent_id,
                content=content,
                filename=filename,
                mime=_guess_mime(message.media_type, filename),
                source="whatsapp",
                source_ref=message.id,
            )
            attachments.submit(att.id)
            return att.item_id
        except Exception as exc:  # noqa: BLE001 — recorded, never silent
            ops.record_event(
                "whatsapp",
                "media_capture_error",
                ok=False,
                detail={
                    "message_id": message.id,
                    "error": f"{type(exc).__name__}: {exc}"[:200],
                },
            )
            return None

    def _replace_media_marker(
        self, parent_id: UUID, message: BridgeMessage, attachment_item_id: UUID
    ) -> None:
        """Swap the "attachment not read yet" marker for the attachment link."""
        item = self._items.get(parent_id)
        if item is None:
            return
        needle = f"{message.media_type}: {message.filename}{_NOT_READ_YET}"
        replacement = f"{message.media_type}: {message.filename} — attachment {attachment_item_id}"
        if needle in item.raw_text:
            item.raw_text = item.raw_text.replace(needle, replacement, 1)
        meta = dict(item.meta or {})
        messages = [dict(m) for m in meta.get("messages", [])]
        for entry in messages:
            media = entry.get("media")
            if isinstance(media, dict) and media.get("message_id") == message.id:
                media["status"] = "saved"
                media["attachment_item_id"] = str(attachment_item_id)
                entry["media"] = media
        meta["messages"] = messages
        item.meta = meta
        if _NOT_READ_YET not in item.raw_text and not user_acted_on_item(item):
            # Fully read: the conversation now carries its real text (the
            # swapped-in attachment links are readable content). Re-triage
            # through the normal update path — the v3 rules hold inside
            # (_triage_item: relevant is never downgraded to noise, and a
            # blocked downgrade is recorded). Fires ONCE, when the LAST
            # unread marker is replaced; user-acted items are never touched.
            self._triage_item(item)

    @staticmethod
    def _known_message_ids(item: MemoryItem | None) -> set[str]:
        if item is None:
            return set()
        return {str(entry.get("id")) for entry in item.meta.get("messages", [])}

    def _open_last_instant(self, open_item: MemoryItem | None) -> datetime | None:
        if open_item is None:
            return None
        raw = open_item.meta.get("last_message_at")
        if not raw:
            return None
        return datetime.fromisoformat(str(raw))

    def _append_to_conversation(self, item: MemoryItem, messages: list[BridgeMessage]) -> None:
        """Append messages to the open conversation's item (update in place)."""
        new_lines = [_message_line(message, self._zone) for message in messages]
        new_meta = [_message_meta(message, self._zone) for message in messages]
        item.raw_text = item.raw_text.rstrip("\n") + "\n" + "\n".join(new_lines)
        item.meta = {
            **item.meta,
            "last_message_at": messages[-1].timestamp_utc.isoformat(),
            "messages": [*item.meta.get("messages", []), *new_meta],
        }
        item.fingerprint = content_fingerprint(item.raw_text, item.occurred_at, SOURCE_WHATSAPP)
        self._index_item(item)
        self._triage_item(item)

    def _create_conversation(
        self, channel: str, chat_jid: str, messages: list[BridgeMessage]
    ) -> MemoryItem:
        raw_text, meta = render_conversation(
            channel, chat_jid, messages, zone_name=self._zone_name, gap_minutes=self._gap_minutes
        )
        item = MemoryItem(
            raw_text=raw_text,
            source=SOURCE_WHATSAPP,
            occurred_at=messages[0].timestamp_utc,
            meta=meta,
        )
        item.fingerprint = content_fingerprint(raw_text, item.occurred_at, SOURCE_WHATSAPP)
        self._items.add(item)
        self._index_item(item)
        self._triage_item(item)
        return item

    def _triage_item(self, item: MemoryItem) -> None:
        """D2a (item 36): best-effort relevance triage; never blocks the poll.

        Runs on conversation CREATE and on every UPDATE (a conversation can
        turn relevant). A trier failure (exception or None) leaves the item
        untriaged and is recorded as an ops event — never silent, never
        fatal to the poll.

        v3 review rules: (1) NO mid-poll commit here — the poll's single
        commit point persists the triage columns together with the poller's
        own state/cursor bookkeeping, so a later failure in the same poll
        rolls the conversation back with the rest of the channel's writes.
        (2) On update, RELEVANT may never become NOISE (appending chatter
        must not hide something real); noise->relevant stays allowed. The
        blocked downgrade is recorded, never silent.
        """
        if self._trier is None:
            return
        if is_unread_media_only(item.raw_text):
            # Nothing readable yet (only unread media markers): labelling a
            # filename is how media-only conversations landed NOISE on zero
            # content. The item stays untriaged (still listed) and is
            # re-triaged when the last marker is replaced (see
            # _replace_media_marker). A skip is not a failure — no ops event.
            return
        prior = item.triage_label
        try:
            result = self._trier(item)
        except Exception as exc:  # noqa: BLE001 — triage never blocks capture
            ops.record_event(
                "triage", "failed", ok=False,
                detail={"error": type(exc).__name__, "reason": str(exc)[:120]},
            )
            return
        if result is None:
            ops.record_event(
                "triage", "failed", ok=False,
                detail={"error": "no decision"},
            )
            return
        if prior == "relevant" and result.label == "noise":
            # Asymmetry rule (v3 review): hiding something real is worse than
            # showing chatter — keep the stronger label on update.
            ops.record_event(
                "triage", "downgrade_blocked", ok=True,
                detail={
                    "item_id": str(item.id),
                    "kept": "relevant",
                    "rejected": "noise",
                    "version": result.version,
                },
            )
            return
        item.triage_label = result.label
        item.triage_confidence = result.confidence
        item.triage_method = result.method
        item.triage_version = result.version

    def _index_item(self, item: MemoryItem) -> None:
        """Best-effort vector indexing; failures defer to 'brain reindex'."""
        if self._indexer is None:
            return
        try:
            self._indexer.index(item)
        except Exception as exc:  # noqa: BLE001 — indexing never blocks capture
            logger.warning(
                "whatsapp: indexing failed for item %s (%s); it will be indexed by 'brain reindex'",
                item.id,
                exc,
            )


_WHATSAPP_MIME: dict[str, str] = {
    "image": "image/jpeg",
    "video": "video/mp4",
    "audio": "audio/ogg",
}


def _guess_mime(media_type: str | None, filename: str) -> str | None:
    """A conservative mime from the bridge's media type + filename suffix."""
    ext = Path(filename).suffix.lower()
    if media_type == "document":
        return "application/pdf" if ext == ".pdf" else None
    mime = _WHATSAPP_MIME.get(media_type or "")
    if mime == "image/jpeg" and ext in {".png", ".webp"}:
        return f"image/{ext.lstrip('.')}"
    return mime


def bridge_group_options(
    store: BridgeStore, *, allowlisted: set[str]
) -> list[dict[str, str]]:
    """Bridge-visible WhatsApp GROUP chats that are not allowlisted yet —
    the '+ Add group' options (Memory Management screen).

    READ-ONLY by construction: the only bridge access is ``store.chats()``
    (a SELECT over the store opened mode=ro). The bridge's REST API (send /
    download) is never touched here. Options are deduped by display name
    (the allowlist keys on names — several jids may share one) and sorted.
    """
    seen: dict[str, str] = {}
    for jid, name in sorted(store.chats().items()):
        if not name or not jid.endswith("@g.us"):
            continue  # groups only — direct chats are not allowlist material
        if name in allowlisted or name in seen:
            continue
        seen[name] = jid
    return [{"jid": jid, "name": name} for name, jid in sorted(seen.items())]
