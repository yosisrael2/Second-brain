"""F3 (item 47): WhatsApp media in ALLOWLISTED channels -> the attachments core.

Only allowlisted channels are ever polled, so media in any other chat is
never fetched. Media bytes come from the bridge's local REST download
(POST {base}/api/download {message_id, chat_jid}) — read-only from our side:
the client has no send capability at all. The conversation's
"attachment not read yet" marker is replaced with a link to the attachment
item. Budget/limits/sandbox are the attachments core's (already proven).
"""

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from tests.services.test_whatsapp_ingest import (  # noqa: F401
    CHAT_A_JID,
    SimpleWorld,
    _add_row,
    _bridge_row,
    _write_bridge,
    world_fixture,
)
from tests.test_attachments import (  # noqa: F401
    FakeQuery,
    _make_service,
    svc_env_fixture,
)

from brain.services.whatsapp import BridgeStore, WhatsAppPoller


class FakeBridgeHTTP:
    """The REAL /api/download contract over a mocked transport.

    Behaves like whatsapp-bridge/main.go: POST {message_id, chat_jid} ->
    downloads the media into store/<chat_jid>/<filename> and answers
    application/json {success, message, filename, path} (success:false on
    failure). The poller test then drives the REAL WhatsAppMediaClient,
    so the JSON parsing + store-path validation run for real.
    """

    def __init__(
        self,
        store_root: Any,
        blobs: dict[str, bytes] | None = None,
        fail: bool = False,
    ) -> None:
        import httpx

        self.store_root = store_root
        self._blobs = blobs or {}
        self._fail = fail
        self.calls: list[dict[str, Any]] = []

        def handler(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content)
            self.calls.append(body)
            if self._fail:
                return httpx.Response(
                    500, json={"success": False, "message": "Failed to download media: expired"}
                )
            message_id = body["message_id"]
            chat_jid = body["chat_jid"]
            blob = self._blobs[message_id]
            chat_dir = Path(self.store_root) / chat_jid
            chat_dir.mkdir(parents=True, exist_ok=True)
            media_path = chat_dir / f"{message_id}.bin"
            media_path.write_bytes(blob)
            return httpx.Response(
                200,
                json={
                    "success": True,
                    "message": "Successfully downloaded image media",
                    "filename": media_path.name,
                    "path": str(media_path),
                },
            )

        self.transport = httpx.MockTransport(handler)

    def client(self) -> Any:
        from brain.services.whatsapp import WhatsAppMediaClient

        return WhatsAppMediaClient(
            "http://127.0.0.1:8085", store_root=self.store_root, transport=self.transport
        )


class FakeIndexer:
    def __init__(self) -> None:
        self.indexed: list[Any] = []

    def index(self, item: Any) -> bool:
        self.indexed.append(item.id)
        return True


class FakeAttachmentRecorder:
    """No-DB recorder for wiring assertions."""

    def __init__(self) -> None:
        self.captured: list[dict[str, Any]] = []

    def capture(self, parent_item_id, content, filename, mime, source, source_ref=None):
        self.captured.append({"parent": parent_item_id, "filename": filename, "source": source})

        class _A:
            id = "att-1"
            item_id = "own-1"

        return _A()

    def submit(self, attachment_id: Any) -> None: ...

    def drain(self) -> int:
        return 0


class _RealAttachments:
    """The REAL AttachmentService over a tmp store, wrapped for the poller."""

    def __init__(self, svc: Any) -> None:
        self.svc = svc
        self.captured: list[dict[str, Any]] = []

    def capture(self, parent_item_id, content, filename, mime, source, source_ref=None):
        self.captured.append({"parent": parent_item_id, "filename": filename, "source": source})
        return self.svc.capture(parent_item_id, content, filename, mime, source, source_ref)

    def submit(self, attachment_id: Any) -> None:
        self.svc.submit(attachment_id)

    def drain(self) -> int:
        return self.svc.drain()


def _media_poller(
    world: SimpleWorld, bridge: BridgeStore, media: FakeBridgeHTTP, att: Any
) -> WhatsAppPoller:
    return WhatsAppPoller(
        bridge=bridge,
        state=world.state,
        items=world.items,
        memory=world.memory_repo,
        indexer=FakeIndexer(),
        zone_name="Asia/Jerusalem",
        commit=True,
        attachments=att,
        media_client=media.client(),
    )


def _media_row(mid: str, at: datetime, filename: str = "pic.jpg", length: int = 2048):
    return _bridge_row(
        mid,
        at,
        sender="Rina",
        content="",
        media_type="image",
        filename=filename,
        file_length=length,
    )


class TestWhatsappMedia:
    def test_media_in_allowlisted_channel_downloaded_and_captured(self, world, svc_env, tmp_path):
        from brain.db.models import Attachment, MemoryItem
        from brain.db.session import SessionLocal

        world.seed_channel("Chat A")
        path = tmp_path / "messages.db"
        _write_bridge(
            path,
            [(CHAT_A_JID, "Chat A")],
            [_bridge_row("z0", datetime(2026, 10, 22, 8, 0, tzinfo=UTC), content="anchor")],
        )
        media = FakeBridgeHTTP(tmp_path / "store", {"m1": b"\x89PNG fake image bytes"})
        att = _RealAttachments(
            _make_service(tmp_path, FakeQuery(reply="hat list\nDESCRIPTION: a note"))
        )
        poller = _media_poller(world, BridgeStore(path), media, att)
        poller.poll(now=datetime(2026, 10, 22, 8, 5, tzinfo=UTC))  # live edge
        _add_row(path, _media_row("m1", datetime(2026, 10, 22, 10, 0, tzinfo=UTC)))
        poller.poll(now=datetime(2026, 10, 22, 12, 0, tzinfo=UTC))

        assert media.calls == [{"message_id": "m1", "chat_jid": CHAT_A_JID}]
        assert att.captured and att.captured[0]["source"] == "whatsapp"
        assert att.captured[0]["filename"] == "pic.jpg"
        with SessionLocal() as session:
            row = session.query(Attachment).one()
            assert row.source == "whatsapp"
            assert row.status == "done"
            assert "hat list" in (row.text or "")
            parent_id, own_id = row.parent_item_id, row.item_id
        with SessionLocal() as session:
            parent = session.get(MemoryItem, parent_id)
            assert "attachment not read yet" not in parent.raw_text
            assert str(own_id) in parent.raw_text, "marker replaced by the attachment link"

    def test_media_in_non_allowlisted_chat_never_fetched(self, world, svc_env, tmp_path):
        world.seed_channel("Chat A")
        path = tmp_path / "messages.db"
        _write_bridge(
            path,
            [(CHAT_A_JID, "Chat A"), ("other@g.us", "Random Chat")],
            [_bridge_row("z0", datetime(2026, 10, 22, 8, 0, tzinfo=UTC), content="anchor")],
        )
        media = FakeBridgeHTTP(tmp_path / "store", {"m1": b"bytes"})
        att = FakeAttachmentRecorder()
        poller = _media_poller(world, BridgeStore(path), media, att)
        poller.poll(now=datetime(2026, 10, 22, 8, 5, tzinfo=UTC))
        _add_row(
            path,
            _bridge_row(
                "m1",
                datetime(2026, 10, 22, 10, 0, tzinfo=UTC),
                chat="other@g.us",
                sender="Rina",
                content="",
                media_type="image",
                filename="x.jpg",
                file_length=2048,
            ),
        )
        poller.poll(now=datetime(2026, 10, 22, 12, 0, tzinfo=UTC))
        assert media.calls == [], "never-allowlisted chats are never fetched"
        assert att.captured == []

    def test_budget_respected_across_media(self, world, svc_env, tmp_path):
        from brain.db.models import Attachment
        from brain.db.session import SessionLocal

        world.seed_channel("Chat A")
        path = tmp_path / "messages.db"
        _write_bridge(
            path,
            [(CHAT_A_JID, "Chat A")],
            [_bridge_row("z0", datetime(2026, 10, 22, 8, 0, tzinfo=UTC), content="anchor")],
        )
        media = FakeBridgeHTTP(tmp_path / "store", {"m1": b"\x89PNG one", "m2": b"\x89PNG two"})
        att = _RealAttachments(_make_service(tmp_path, FakeQuery(reply="txt"), daily_budget=1))
        poller = _media_poller(world, BridgeStore(path), media, att)
        poller.poll(now=datetime(2026, 10, 22, 8, 5, tzinfo=UTC))
        _add_row(
            path, _media_row("m1", datetime(2026, 10, 22, 10, 0, tzinfo=UTC), filename="a.jpg")
        )
        _add_row(
            path, _media_row("m2", datetime(2026, 10, 22, 10, 1, tzinfo=UTC), filename="b.jpg")
        )
        poller.poll(now=datetime(2026, 10, 22, 12, 0, tzinfo=UTC))

        with SessionLocal() as session:
            rows = session.query(Attachment).all()
        assert len(rows) == 2  # both captured
        done = [r for r in rows if r.status == "done"]
        pending = [r for r in rows if r.status == "pending"]
        assert len(done) == 1 and len(pending) == 1, "budget 1: exactly one read"

    def test_download_failure_recorded_marker_stays(self, world, svc_env, tmp_path, monkeypatch):
        events: list[tuple] = []
        import brain.services.whatsapp as wa

        monkeypatch.setattr(
            wa.ops,
            "record_event",
            lambda component, kind, **kw: events.append((component, kind, kw)),
        )
        world.seed_channel("Chat A")
        path = tmp_path / "messages.db"
        _write_bridge(
            path,
            [(CHAT_A_JID, "Chat A")],
            [_bridge_row("z0", datetime(2026, 10, 22, 8, 0, tzinfo=UTC), content="anchor")],
        )
        media = FakeBridgeHTTP(tmp_path / "store", fail=True)
        att = FakeAttachmentRecorder()
        poller = _media_poller(world, BridgeStore(path), media, att)
        poller.poll(now=datetime(2026, 10, 22, 8, 5, tzinfo=UTC))
        _add_row(path, _media_row("m1", datetime(2026, 10, 22, 10, 0, tzinfo=UTC)))
        poller.poll(now=datetime(2026, 10, 22, 12, 0, tzinfo=UTC))

        assert media.calls, "the download was attempted"
        assert att.captured == []
        assert any(kind == "media_download_error" for _, kind, _ in events)
        from brain.db.models import MemoryItem
        from brain.db.session import SessionLocal

        with SessionLocal() as session:
            item = session.query(MemoryItem).one()
            assert "attachment not read yet" in item.raw_text  # marker intact

    def test_oversized_media_never_downloaded(self, world, svc_env, tmp_path, monkeypatch):
        events: list[tuple] = []
        import brain.services.whatsapp as wa

        monkeypatch.setattr(
            wa.ops,
            "record_event",
            lambda component, kind, **kw: events.append((component, kind, kw)),
        )
        from brain.services.attachments import MAX_FILE_BYTES

        world.seed_channel("Chat A")
        path = tmp_path / "messages.db"
        _write_bridge(
            path,
            [(CHAT_A_JID, "Chat A")],
            [_bridge_row("z0", datetime(2026, 10, 22, 8, 0, tzinfo=UTC), content="anchor")],
        )
        media = FakeBridgeHTTP(tmp_path / "store", {"m1": b"x" * 16})
        att = FakeAttachmentRecorder()
        poller = _media_poller(world, BridgeStore(path), media, att)
        poller.poll(now=datetime(2026, 10, 22, 8, 5, tzinfo=UTC))
        _add_row(
            path,
            _media_row(
                "m1",
                datetime(2026, 10, 22, 10, 0, tzinfo=UTC),
                filename="big.pdf",
                length=MAX_FILE_BYTES + 1,
            ),
        )
        poller.poll(now=datetime(2026, 10, 22, 12, 0, tzinfo=UTC))

        assert media.calls == [], "20 MB files are skipped BEFORE any download"
        assert att.captured == []
        assert any(kind == "media_skip_too_large" for _, kind, _ in events)


class TestBridgeClientSafety:
    def test_real_client_has_no_send_capability(self):
        from brain.services.whatsapp import WhatsAppMediaClient

        client = WhatsAppMediaClient("http://127.0.0.1:8085")
        assert not any("send" in name.lower() for name in dir(client))
        assert hasattr(client, "download")
