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

Media (item 47, deferred to PR D's second half after F1): a media message is
kept inside its conversation as a recorded ``[<type>: <filename> — attachment
not read yet]`` marker plus a stable media reference in meta (media_type,
filename, file_length, message_id). D1 downloads NOTHING and never copies the
private CDN url into our store.

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
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Protocol
from uuid import UUID
from zoneinfo import ZoneInfo

from brain import ops
from brain.db.models import MemoryItem, WhatsAppPollState
from brain.repositories.items import ItemsRepository
from brain.repositories.memory import MemoryRepository
from brain.repositories.whatsapp import WhatsAppStateRepository
from brain.services.dedupe import content_fingerprint

logger = logging.getLogger(__name__)

SOURCE_WHATSAPP = "whatsapp"
DEFAULT_GAP_MINUTES = 60
DEFAULT_OVERLAP_MINUTES = 5
_NOT_READ_YET = " — attachment not read yet"
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
        zone_name: str,
        gap_minutes: int = DEFAULT_GAP_MINUTES,
        overlap_minutes: int = DEFAULT_OVERLAP_MINUTES,
        backfill_since: datetime | None = None,
        commit: bool = False,
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

    def poll(self, *, now: datetime) -> list[ChannelPollResult]:
        """Poll every allowlisted channel once; per-channel failures are logged, not raised."""
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
            remaining = fresh[idx:]
            for group in group_conversations(remaining, gap_minutes=self._gap_minutes):
                item = self._create_conversation(name, jid, group)
                open_id = item.id
                set_open = True
                created += 1
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
        )

    def _open_item(self, state: WhatsAppPollState) -> MemoryItem | None:
        if state.open_item_id is None:
            return None
        return self._items.get(state.open_item_id)

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
        return item

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
