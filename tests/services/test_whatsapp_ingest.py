"""WhatsApp ingestion (PR D1) tests.

Offline by construction: the bridge store is a SYNTHETIC SQLite fixture built
with the exact schema the Go bridge creates (sqlite3 .schema of
~/git/whatsapp-mcp/whatsapp-bridge/store/messages.db). No live bridge, no
MCP, no network, no media download.

Spec: design delta items 33-36, 41, 47.
- Allowlist = memory entities of kind 'channel' (item 34); non-allowlisted
  chats are never ingested.
- Conversations are grouped by TIME GAP ONLY (item 35; the bridge schema has
  NO reply-to/quoted-id column, so reply chains are unavailable — see the
  module docstring of brain.services.whatsapp).
- One source item per conversation, updated in place while open.
- Media messages become "attachment not read yet" markers (item 47 lands
  later, after F1).
- Timestamps convert to Settings.timezone via ZoneInfo (item 41); DST test
  across Sun 25 Oct 2026.
"""

from __future__ import annotations

import sqlite3
import uuid as uuid_mod
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy import text as sa_text

from brain.config import get_settings
from brain.db.models import MemoryItem
from brain.db.session import SessionLocal
from brain.main import create_app
from brain.repositories.items import ItemsRepository
from brain.repositories.memory import MemoryRepository
from brain.repositories.whatsapp import WhatsAppStateRepository
from brain.services.dedupe import content_fingerprint
from brain.services.memory import MemoryService
from brain.services.whatsapp import (
    DEFAULT_GAP_MINUTES,
    BridgeMessage,
    BridgeStore,
    WhatsAppPoller,
    group_conversations,
    render_conversation,
)

ZONE = "Asia/Jerusalem"
_JERUSALEM = ZoneInfo(ZONE)

CHAT_A_JID = "jid-a@g.us"

BRIDGE_SCHEMA = """
CREATE TABLE chats (
    jid TEXT PRIMARY KEY,
    name TEXT,
    last_message_time TIMESTAMP
);
CREATE TABLE messages (
    id TEXT,
    chat_jid TEXT,
    sender TEXT,
    content TEXT,
    timestamp TIMESTAMP,
    is_from_me BOOLEAN,
    media_type TEXT,
    filename TEXT,
    url TEXT,
    media_key BLOB,
    file_sha256 BLOB,
    file_enc_sha256 BLOB,
    file_length INTEGER,
    PRIMARY KEY (id, chat_jid),
    FOREIGN KEY (chat_jid) REFERENCES chats(jid)
);
"""


def _ts(at_utc: datetime) -> str:
    """The timestamp string the Go bridge writes: local wall clock + offset."""
    local = at_utc.astimezone(_JERUSALEM)
    total = int((local.utcoffset() or timedelta(0)).total_seconds())
    sign = "+" if total >= 0 else "-"
    hh, mm = divmod(abs(total) // 60, 60)
    return local.strftime("%Y-%m-%d %H:%M:%S") + f"{sign}{hh:02d}:{mm:02d}"


def _bridge_row(
    id_: str,
    at_utc: datetime,
    *,
    chat: str = CHAT_A_JID,
    sender: str = "Rinat",
    content: str = "text",
    is_from_me: int = 0,
    **extra: Any,
) -> dict[str, Any]:
    return {
        "id": id_,
        "chat_jid": chat,
        "sender": sender,
        "content": content,
        "timestamp": _ts(at_utc),
        "is_from_me": is_from_me,
        "media_type": None,
        "filename": None,
        "url": None,
        "media_key": None,
        "file_sha256": None,
        "file_enc_sha256": None,
        "file_length": None,
        **extra,
    }


def _write_bridge(
    path: Path,
    chats: list[tuple[str, str]],
    messages: list[dict[str, Any]],
) -> Path:
    """Build a synthetic bridge store with the real bridge schema."""
    conn = sqlite3.connect(path)
    try:
        conn.executescript(BRIDGE_SCHEMA)
        conn.executemany(
            "INSERT INTO chats (jid, name, last_message_time) VALUES (?, ?, ?)",
            [(jid, name, None) for jid, name in chats],
        )
        for msg in messages:
            conn.execute(
                "INSERT INTO messages (id, chat_jid, sender, content, timestamp, is_from_me,"
                " media_type, filename, url, media_key, file_sha256, file_enc_sha256, file_length)"
                " VALUES (:id, :chat_jid, :sender, :content, :timestamp, :is_from_me,"
                " :media_type, :filename, :url, :media_key, :file_sha256, :file_enc_sha256,"
                " :file_length)",
                msg,
            )
        conn.commit()
    finally:
        conn.close()
    return path


def _add_row(path: Path, row: dict[str, Any]) -> None:
    conn = sqlite3.connect(path)
    try:
        conn.execute(
            "INSERT INTO messages (id, chat_jid, sender, content, timestamp, is_from_me,"
            " media_type, filename, url, media_key, file_sha256, file_enc_sha256, file_length)"
            " VALUES (:id, :chat_jid, :sender, :content, :timestamp, :is_from_me,"
            " :media_type, :filename, :url, :media_key, :file_sha256, :file_enc_sha256,"
            " :file_length)",
            row,
        )
        conn.commit()
    finally:
        conn.close()


class FakeIndexer:
    """Records indexed items; never touches qdrant."""

    def __init__(self) -> None:
        self.indexed: list[MemoryItem] = []

    def probe(self) -> None:
        return None

    def purge(self) -> int:
        return 0

    def index(self, item: MemoryItem) -> bool:
        self.indexed.append(item)
        return True

    def index_batch(self, items: list[MemoryItem]) -> int:
        self.indexed.extend(items)
        return len(items)

    def delete(self, item_id: uuid_mod.UUID) -> None:
        return None


class SimpleWorld:
    """Helpers over one session: memory service, items repo, state repo."""

    def __init__(self, session: Any) -> None:
        self.session = session
        self.memory = MemoryService(MemoryRepository(session))
        self.memory_repo = MemoryRepository(session)
        self.items = ItemsRepository(session)
        self.state = WhatsAppStateRepository(session)

    def seed_channel(self, name: str) -> None:
        self.memory.set_channel(name, kind="teacher", applies_to="me")

    def item_count(self) -> int:
        return int(self.session.execute(select(func.count()).select_from(MemoryItem)).scalar_one())


@pytest.fixture(name="world")
def world_fixture(database: None):
    """Clean items + whatsapp state + memory per test; the session closes with the test."""
    session = SessionLocal()
    try:
        session.execute(sa_text("DELETE FROM whatsapp_poll_state"))
        session.execute(sa_text("DELETE FROM memory_items"))
        session.execute(sa_text("DELETE FROM memory_facts"))
        session.execute(sa_text("DELETE FROM entity_aliases"))
        session.execute(sa_text("DELETE FROM entities"))
        session.commit()
        yield SimpleWorld(session)
    finally:
        session.rollback()
        session.close()


def _poller(
    world: SimpleWorld,
    bridge: BridgeStore,
    *,
    gap: int = DEFAULT_GAP_MINUTES,
) -> WhatsAppPoller:
    return WhatsAppPoller(
        bridge=bridge,
        state=world.state,
        items=world.items,
        memory=world.memory_repo,
        indexer=FakeIndexer(),
        zone_name=ZONE,
        gap_minutes=gap,
        commit=True,
    )


# -- reader ----------------------------------------------------------------------


def test_reader_returns_messages_as_utc_with_media(tmp_path: Path):
    """Rows come back as aware-UTC instants; media fields survive the read."""
    path = tmp_path / "messages.db"
    _write_bridge(
        path,
        [(CHAT_A_JID, "Chat A")],
        [
            _bridge_row("m1", datetime(2026, 10, 22, 10, 0, tzinfo=UTC), content="hello"),
            _bridge_row(
                "m2",
                datetime(2026, 10, 22, 10, 5, tzinfo=UTC),
                content="see the form",
                media_type="image",
                filename="IMG-1234.jpg",
                url="https://mmg.whatsapp.net/v/t62.7118-24/abc.enc",
                media_key=b"k" * 8,
                file_sha256=b"s" * 8,
                file_enc_sha256=b"e" * 8,
                file_length=2048,
            ),
        ],
    )
    store = BridgeStore(path)
    messages = store.messages_since(CHAT_A_JID, datetime(2026, 10, 1, tzinfo=UTC))
    assert [m.id for m in messages] == ["m1", "m2"]
    assert messages[0].timestamp_utc == datetime(2026, 10, 22, 10, 0, tzinfo=UTC)
    assert messages[1].media_type == "image"
    assert messages[1].filename == "IMG-1234.jpg"
    assert messages[1].file_length == 2048


def test_reader_is_read_only(tmp_path: Path):
    """The bridge store is opened mode=ro: reads never write, writes are refused."""
    path = tmp_path / "messages.db"
    _write_bridge(
        path,
        [(CHAT_A_JID, "Chat A")],
        [_bridge_row("m1", datetime(2026, 10, 22, 10, 0, tzinfo=UTC))],
    )
    before = path.stat().st_mtime
    store = BridgeStore(path)
    store.messages_since(CHAT_A_JID, datetime(2026, 10, 1, tzinfo=UTC))
    store.chats()
    assert path.stat().st_mtime == before
    # The same URI the reader uses refuses writes (evidence mode=ro is real).
    ro = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        with pytest.raises(sqlite3.OperationalError):
            ro.execute("DELETE FROM messages")
    finally:
        ro.close()


# -- grouping (pure functions) -----------------------------------------------------


def _conversation(*specs: tuple[str, datetime]) -> list[BridgeMessage]:
    return [
        BridgeMessage(
            id=id_,
            chat_jid=CHAT_A_JID,
            sender="Rinat",
            content="t",
            timestamp_utc=at,
            is_from_me=False,
            media_type=None,
            filename=None,
            file_length=None,
        )
        for id_, at in specs
    ]


def test_grouping_within_gap_is_one_conversation():
    base = datetime(2026, 10, 22, 10, 0, tzinfo=UTC)
    messages = _conversation(
        ("a", base),
        ("b", base + timedelta(minutes=30)),
        ("c", base + timedelta(minutes=59)),
    )
    groups = group_conversations(messages, gap_minutes=60)
    assert [[m.id for m in g] for g in groups] == [["a", "b", "c"]]


def test_grouping_splits_after_gap_and_includes_boundary():
    """Exactly gap_minutes apart stays together; one second more splits."""
    base = datetime(2026, 10, 22, 10, 0, tzinfo=UTC)
    at_limit = _conversation(("a", base), ("b", base + timedelta(minutes=60)))
    assert [[m.id for m in g] for g in group_conversations(at_limit, gap_minutes=60)] == [
        ["a", "b"]
    ]
    past_limit = _conversation(("a", base), ("b", base + timedelta(minutes=60, seconds=1)))
    assert [[m.id for m in g] for g in group_conversations(past_limit, gap_minutes=60)] == [
        ["a"],
        ["b"],
    ]


def test_grouping_sorts_out_of_order_input():
    base = datetime(2026, 10, 22, 10, 0, tzinfo=UTC)
    messages = _conversation(("b", base + timedelta(minutes=5)), ("a", base))
    groups = group_conversations(messages, gap_minutes=60)
    assert [[m.id for m in g] for g in groups] == [["a", "b"]]


# -- rendering ---------------------------------------------------------------------


def test_render_names_channel_with_sender_and_local_time():
    msgs = _conversation(
        ("a", datetime(2026, 10, 24, 21, 0, tzinfo=UTC)),
        ("b", datetime(2026, 10, 24, 21, 30, tzinfo=UTC)),
    )
    msgs[0] = BridgeMessage(
        id="a",
        chat_jid=CHAT_A_JID,
        sender="Rinat",
        content="שלום",
        timestamp_utc=datetime(2026, 10, 24, 21, 0, tzinfo=UTC),
        is_from_me=False,
        media_type=None,
        filename=None,
        file_length=None,
    )
    msgs[1] = BridgeMessage(
        id="b",
        chat_jid=CHAT_A_JID,
        sender="Dana",
        content="hi",
        timestamp_utc=datetime(2026, 10, 24, 21, 30, tzinfo=UTC),
        is_from_me=False,
        media_type=None,
        filename=None,
        file_length=None,
    )
    text, meta = render_conversation("Chat A", CHAT_A_JID, msgs, zone_name=ZONE, gap_minutes=60)
    assert text.splitlines()[0] == "[WhatsApp · Chat A]"
    assert "[25/10/2026, 00:00] Rinat: שלום" in text
    assert "[25/10/2026, 00:30] Dana: hi" in text
    assert meta["channel"] == "Chat A"
    assert meta["chat_jid"] == CHAT_A_JID
    assert meta["first_message_at"] == "2026-10-24T21:00:00+00:00"
    assert meta["last_message_at"] == "2026-10-24T21:30:00+00:00"
    assert [m["id"] for m in meta["messages"]] == ["a", "b"]
    assert meta["messages"][0]["sender"] == "Rinat"


# -- poller ------------------------------------------------------------------------


def test_allowlist_only_ingests_known_channels(tmp_path: Path, world: SimpleWorld):
    """A chat that is not a channel entity in memory is never ingested (item 34)."""
    world.seed_channel("Chat A")
    path = tmp_path / "messages.db"
    _write_bridge(
        path,
        [(CHAT_A_JID, "Chat A"), ("jid-b@g.us", "Unknown Chat")],
        [
            _bridge_row("a1", datetime(2026, 10, 22, 8, 0, tzinfo=UTC)),
            _bridge_row("b1", datetime(2026, 10, 22, 8, 0, tzinfo=UTC), chat="jid-b@g.us"),
        ],
    )
    poller = _poller(world, BridgeStore(path))
    poller.poll(now=datetime(2026, 10, 22, 8, 5, tzinfo=UTC))  # live edge for both chats
    assert world.item_count() == 0
    _add_row(path, _bridge_row("a2", datetime(2026, 10, 22, 10, 0, tzinfo=UTC), content="for A"))
    _add_row(
        path,
        _bridge_row(
            "b2", datetime(2026, 10, 22, 10, 0, tzinfo=UTC), chat="jid-b@g.us", content="for B"
        ),
    )
    result = poller.poll(now=datetime(2026, 10, 22, 12, 0, tzinfo=UTC))
    assert [r.chat_jid for r in result] == [CHAT_A_JID]
    assert world.item_count() == 1
    item = world.session.query(MemoryItem).one()
    assert item.source == "whatsapp"
    assert item.meta["channel"] == "Chat A"
    assert "for B" not in item.raw_text
    # The non-allowlisted chat never even gets a cursor row.
    assert world.state.get("jid-b@g.us") is None


def test_channel_without_matching_chat_is_skipped(tmp_path: Path, world: SimpleWorld):
    world.seed_channel("Ghost Channel")
    path = tmp_path / "messages.db"
    _write_bridge(path, [(CHAT_A_JID, "Chat A")], [])
    result = _poller(world, BridgeStore(path)).poll(now=datetime(2026, 10, 22, 12, 0, tzinfo=UTC))
    assert result == []
    assert world.item_count() == 0


def test_first_poll_starts_at_live_edge_no_backfill(tmp_path: Path, world: SimpleWorld):
    """A brand-new channel starts at the live edge: history is not dumped (opt-in backfill)."""
    world.seed_channel("Chat A")
    path = tmp_path / "messages.db"
    _write_bridge(
        path,
        [(CHAT_A_JID, "Chat A")],
        [
            _bridge_row("old1", datetime(2026, 10, 20, 9, 0, tzinfo=UTC)),
            _bridge_row("old2", datetime(2026, 10, 20, 9, 1, tzinfo=UTC)),
        ],
    )
    _poller(world, BridgeStore(path)).poll(now=datetime(2026, 10, 22, 12, 0, tzinfo=UTC))
    assert world.item_count() == 0
    state = world.state.get(CHAT_A_JID)
    assert state is not None
    assert state.cursor_utc == datetime(2026, 10, 20, 9, 1, tzinfo=UTC)
    assert state.history_until_utc == datetime(2026, 10, 20, 9, 1, tzinfo=UTC)


def test_new_message_after_live_edge_is_ingested(tmp_path: Path, world: SimpleWorld):
    world.seed_channel("Chat A")
    path = tmp_path / "messages.db"
    _write_bridge(
        path,
        [(CHAT_A_JID, "Chat A")],
        [_bridge_row("old1", datetime(2026, 10, 20, 9, 0, tzinfo=UTC))],
    )
    poller = _poller(world, BridgeStore(path))
    poller.poll(now=datetime(2026, 10, 22, 12, 0, tzinfo=UTC))
    _add_row(path, _bridge_row("new1", datetime(2026, 10, 22, 13, 0, tzinfo=UTC), content="fresh"))
    poller.poll(now=datetime(2026, 10, 22, 14, 0, tzinfo=UTC))
    assert world.item_count() == 1
    item = world.session.query(MemoryItem).one()
    assert "fresh" in item.raw_text
    # The skipped history message is not re-ingested via the overlap window.
    assert "old" not in item.raw_text


def test_cursor_idempotent_across_restarts(tmp_path: Path, world: SimpleWorld):
    """A fresh poller instance (restart) with the same messages creates nothing new."""
    world.seed_channel("Chat A")
    path = tmp_path / "messages.db"
    _write_bridge(
        path,
        [(CHAT_A_JID, "Chat A")],
        [_bridge_row("a1", datetime(2026, 10, 22, 10, 0, tzinfo=UTC), content="one")],
    )
    first = _poller(world, BridgeStore(path))
    first.poll(now=datetime(2026, 10, 22, 10, 2, tzinfo=UTC))  # live edge
    _add_row(path, _bridge_row("a2", datetime(2026, 10, 22, 10, 10, tzinfo=UTC), content="two"))
    # First REAL poll ingests a2 (a1 is history).
    first.poll(now=datetime(2026, 10, 22, 10, 6, tzinfo=UTC))
    assert world.item_count() == 1
    assert world.state.get(CHAT_A_JID).messages_seen == 1
    # "Restart": a brand-new poller with its own bridge connection, cursor from OUR db.
    world.session.expire_all()
    second = _poller(world, BridgeStore(path))
    second.poll(now=datetime(2026, 10, 22, 10, 8, tzinfo=UTC))
    assert world.item_count() == 1
    assert world.state.get(CHAT_A_JID).messages_seen == 1


def test_overlap_window_does_not_duplicate(tmp_path: Path, world: SimpleWorld):
    """Messages in the overlap window are deduped by id: restarts never double-capture."""
    world.seed_channel("Chat A")
    path = tmp_path / "messages.db"
    _write_bridge(
        path,
        [(CHAT_A_JID, "Chat A")],
        [_bridge_row("a1", datetime(2026, 10, 22, 8, 0, tzinfo=UTC))],
    )
    poller = _poller(world, BridgeStore(path))
    poller.poll(now=datetime(2026, 10, 22, 8, 5, tzinfo=UTC))  # live edge
    _add_row(path, _bridge_row("a2", datetime(2026, 10, 22, 10, 3, tzinfo=UTC), content="real"))
    poller.poll(now=datetime(2026, 10, 22, 10, 4, tzinfo=UTC))
    assert world.item_count() == 1
    # A poll minutes later re-reads a1+a2 (overlap) but creates nothing new:
    # a1 is below the history marker, a2 by message id.
    poller.poll(now=datetime(2026, 10, 22, 10, 9, tzinfo=UTC))
    assert world.item_count() == 1
    state = world.state.get(CHAT_A_JID)
    assert state.messages_seen == 1


def test_dst_boundary_one_conversation_correct_local_times(tmp_path: Path, world: SimpleWorld):
    """Item 41: messages on both sides of Sun 25 Oct 2026 keep UTC order and true local times.

    UTC 22:45 = 25 Oct 01:45+03:00 (before the switch); UTC 23:10 = 25 Oct
    01:10+02:00 (after). 25 minutes apart: ONE conversation. The wall clock
    repeats the hour (01:45 -> 01:10) while UTC order moves forward — a fixed
    +03:00 would render the second as 02:10 and flip the local order.
    """
    world.seed_channel("Chat A")
    path = tmp_path / "messages.db"
    _write_bridge(
        path,
        [(CHAT_A_JID, "Chat A")],
        [_bridge_row("anchor", datetime(2026, 10, 24, 20, 0, tzinfo=UTC), content="anchor")],
    )
    poller = _poller(world, BridgeStore(path))
    poller.poll(now=datetime(2026, 10, 24, 20, 5, tzinfo=UTC))  # live edge
    _add_row(
        path,
        _bridge_row("pre", datetime(2026, 10, 24, 22, 45, tzinfo=UTC), content="before switch"),
    )
    _add_row(
        path,
        _bridge_row("post", datetime(2026, 10, 24, 23, 10, tzinfo=UTC), content="after switch"),
    )
    poller.poll(now=datetime(2026, 10, 24, 23, 59, tzinfo=UTC))
    assert world.item_count() == 1
    item = world.session.query(MemoryItem).one()
    assert "[25/10/2026, 01:45] Rinat: before switch" in item.raw_text
    assert "[25/10/2026, 01:10] Rinat: after switch" in item.raw_text
    assert item.raw_text.index("01:45") < item.raw_text.index("01:10")  # UTC order holds
    assert item.occurred_at == datetime(2026, 10, 24, 22, 45, tzinfo=UTC)


def test_conversation_updated_in_place_then_gap_creates_new(tmp_path: Path, world: SimpleWorld):
    """A still-open conversation updates its ONE item; after the gap a new item starts."""
    world.seed_channel("Chat A")
    path = tmp_path / "messages.db"
    _write_bridge(
        path,
        [(CHAT_A_JID, "Chat A")],
        [_bridge_row("z0", datetime(2026, 10, 22, 8, 0, tzinfo=UTC), content="anchor")],
    )
    poller = _poller(world, BridgeStore(path))
    poller.poll(now=datetime(2026, 10, 22, 8, 5, tzinfo=UTC))  # live edge

    _add_row(path, _bridge_row("a1", datetime(2026, 10, 22, 10, 0, tzinfo=UTC), content="first"))
    poller.poll(now=datetime(2026, 10, 22, 10, 5, tzinfo=UTC))
    assert world.item_count() == 1
    item = world.session.query(MemoryItem).one()
    first_id = item.id
    assert "first" in item.raw_text
    assert item.meta["messages"][0]["id"] == "a1"

    _add_row(
        path,
        _bridge_row(
            "a2", datetime(2026, 10, 22, 10, 40, tzinfo=UTC), sender="Dana", content="second"
        ),
    )
    poller.poll(now=datetime(2026, 10, 22, 11, 0, tzinfo=UTC))
    assert world.item_count() == 1
    world.session.expire_all()
    item = world.session.get(MemoryItem, first_id)
    assert item is not None
    assert "first" in item.raw_text and "second" in item.raw_text
    assert [m["id"] for m in item.meta["messages"]] == ["a1", "a2"]
    # The fingerprint follows the updated text (dedupe stays exact).
    assert item.fingerprint == content_fingerprint(item.raw_text, item.occurred_at, "whatsapp")

    _add_row(
        path,
        _bridge_row(
            "a3", datetime(2026, 10, 22, 12, 41, tzinfo=UTC), sender="Dana", content="third"
        ),
    )
    poller.poll(now=datetime(2026, 10, 22, 13, 0, tzinfo=UTC))
    assert world.item_count() == 2
    new_item = world.session.query(MemoryItem).filter(MemoryItem.id != first_id).one()
    assert "third" in new_item.raw_text
    assert "first" not in new_item.raw_text


def test_media_marker_without_download(tmp_path: Path, world: SimpleWorld):
    """Item 47 deferred: a media message becomes a recorded marker, nothing is downloaded."""
    world.seed_channel("Chat A")
    path = tmp_path / "messages.db"
    _write_bridge(
        path,
        [(CHAT_A_JID, "Chat A")],
        [_bridge_row("z0", datetime(2026, 10, 22, 8, 0, tzinfo=UTC), content="anchor")],
    )
    poller = _poller(world, BridgeStore(path))
    poller.poll(now=datetime(2026, 10, 22, 8, 5, tzinfo=UTC))  # live edge
    _add_row(
        path,
        _bridge_row(
            "m1",
            datetime(2026, 10, 22, 10, 0, tzinfo=UTC),
            content="the form",
            media_type="document",
            filename="talmul.pdf",
            url="https://mmg.whatsapp.net/v/t62.7118-24/secret.enc",
            media_key=b"k" * 8,
            file_length=4096,
        ),
    )
    poller.poll(now=datetime(2026, 10, 22, 12, 0, tzinfo=UTC))
    item = world.session.query(MemoryItem).one()
    assert (
        "[22/10/2026, 13:00] Rinat: the form [document: talmul.pdf — attachment not read yet]"
        in item.raw_text
    )
    media = item.meta["messages"][0]["media"]
    assert media == {
        "media_type": "document",
        "filename": "talmul.pdf",
        "file_length": 4096,
        "message_id": "m1",
        "status": "not read yet",
    }
    # The private CDN url is never copied into our store.
    assert "mmg.whatsapp.net" not in str(item.meta)


def test_ops_endpoint_reports_cursor_and_counts(tmp_path: Path, world: SimpleWorld):
    """/ops/whatsapp: last poll time + per-channel cursor and counts."""
    world.seed_channel("Chat A")
    path = tmp_path / "messages.db"
    _write_bridge(
        path,
        [(CHAT_A_JID, "Chat A")],
        [_bridge_row("z0", datetime(2026, 10, 22, 8, 0, tzinfo=UTC))],
    )
    poller = _poller(world, BridgeStore(path))
    poller.poll(now=datetime(2026, 10, 22, 8, 5, tzinfo=UTC))  # live edge
    _add_row(path, _bridge_row("a1", datetime(2026, 10, 22, 10, 0, tzinfo=UTC)))
    poller.poll(now=datetime(2026, 10, 22, 10, 2, tzinfo=UTC))
    world.session.commit()

    get_settings.cache_clear()
    try:
        client = TestClient(create_app())
        payload = client.get("/ops/whatsapp").json()
    finally:
        get_settings.cache_clear()
    assert payload["last_poll_at"] is not None
    assert len(payload["channels"]) == 1
    channel = payload["channels"][0]
    assert channel["channel"] == "Chat A"
    assert channel["chat_jid"] == CHAT_A_JID
    assert channel["cursor_at"] == "2026-10-22T10:00:00+00:00"
    assert channel["messages_seen"] == 1
    assert channel["conversations_created"] == 1
    assert channel["conversations_updated"] == 0
