"""F3 round 1: the bridge download contract is JSON + store-path validation.

The real bridge (~/git/whatsapp-mcp/whatsapp-bridge/main.go /api/download)
responds application/json DownloadMediaResponse {success, message, filename,
path} — it downloads the media to its store dir and returns the PATH, never
the bytes. WhatsAppMediaClient must parse the JSON, require success, validate
the path (realpath inside the bridge store root, regular file) and only then
read the bytes. A path outside the store root is refused and recorded.
"""

import base64
import json
from typing import Any

import httpx
import pytest

from brain.ai.gmail import GmailError, RestGmailClient
pytest.importorskip("brain.services.whatsapp")
from brain.services.whatsapp import WhatsAppMediaClient  # noqa: E402


class _FakeAttachments:
    def __init__(self, result: dict[str, Any] | None, error: Exception | None = None) -> None:
        self._result = result
        self._error = error

    def get(self, userId: str, messageId: str, id: str) -> Any:  # noqa: N803, A002
        result, error = self._result, self._error

        class _Req:
            def execute(self) -> dict[str, Any]:
                if error is not None:
                    raise error
                return result

        return _Req()


class _FakeMessages:
    def __init__(self, attachments: _FakeAttachments) -> None:
        self._attachments = attachments

    def attachments(self) -> _FakeAttachments:
        return self._attachments


class _FakeUsers:
    def __init__(self, messages: _FakeMessages) -> None:
        self._messages = messages

    def messages(self) -> _FakeMessages:
        return self._messages


class _FakeService:
    def __init__(self, users: _FakeUsers) -> None:
        self._users = users

    def users(self) -> _FakeUsers:
        return self._users


def _gmail_client(result: dict[str, Any] | None) -> RestGmailClient:
    service = _FakeService(_FakeUsers(_FakeMessages(_FakeAttachments(result))))
    return RestGmailClient(
        token_loader=lambda: "refresh-token",
        service_factory=lambda token, user_id: service,
    )


def _bridge_transport(
    payload: dict[str, Any] | None = None,
    status_code: int = 200,
    calls: list[dict[str, Any]] | None = None,
):
    def handler(request: httpx.Request) -> httpx.Response:
        if calls is not None:
            calls.append({"url": str(request.url), "json": json.loads(request.content)})
        return httpx.Response(status_code, json=payload)

    return httpx.MockTransport(handler)


class TestBridgeDownloadContract:
    def test_parses_json_and_reads_the_store_file(self, tmp_path):
        store = tmp_path / "store"
        chat_dir = store / "120363425999009211@g.us"
        chat_dir.mkdir(parents=True)
        media = chat_dir / "talmul.pdf"
        media.write_bytes(b"%PDF-1.4 real bytes")
        client = WhatsAppMediaClient(
            "http://127.0.0.1:8085",
            store_root=store,
            transport=_bridge_transport(
                {
                    "success": True,
                    "message": "Successfully downloaded document media",
                    "filename": "talmul.pdf",
                    "path": str(media),
                }
            ),
        )
        assert client.download("m1", "120363425999009211@g.us") == b"%PDF-1.4 real bytes"

    def test_download_posts_message_id_and_chat_jid(self, tmp_path):
        calls: list[dict[str, Any]] = []
        store = tmp_path / "store"
        store.mkdir()
        media = store / "pic.jpg"
        media.write_bytes(b"jpeg")
        client = WhatsAppMediaClient(
            "http://127.0.0.1:8085",
            store_root=store,
            transport=_bridge_transport(
                {"success": True, "path": str(media), "filename": "pic.jpg"}, calls=calls
            ),
        )
        client.download("m1", "chat@g.us")
        assert calls == [{"url": "http://127.0.0.1:8085/api/download",
                          "json": {"message_id": "m1", "chat_jid": "chat@g.us"}}]

    def test_success_false_is_refused(self, tmp_path):
        store = tmp_path / "store"
        store.mkdir()
        client = WhatsAppMediaClient(
            "http://127.0.0.1:8085",
            store_root=store,
            transport=_bridge_transport(
                {"success": False, "message": "Failed to download media: expired"}, status_code=500
            ),
        )
        from brain.services.whatsapp import MediaDownloadError

        with pytest.raises(MediaDownloadError):
            client.download("m1", "chat@g.us")

    def test_path_outside_store_root_is_refused(self, tmp_path):
        outside = tmp_path / "elsewhere" / "secret.bin"
        outside.parent.mkdir(parents=True)
        outside.write_bytes(b"stolen")
        store = tmp_path / "store"
        store.mkdir()
        client = WhatsAppMediaClient(
            "http://127.0.0.1:8085",
            store_root=store,
            transport=_bridge_transport({"success": True, "path": str(outside)}),
        )
        from brain.services.whatsapp import MediaDownloadError

        with pytest.raises(MediaDownloadError):
            client.download("m1", "chat@g.us")

    def test_path_traversal_is_refused(self, tmp_path):
        store = tmp_path / "store"
        store.mkdir()
        (store / "chat@g.us").mkdir()
        client = WhatsAppMediaClient(
            "http://127.0.0.1:8085",
            store_root=store,
            transport=_bridge_transport(
                {"success": True, "path": str(store / "chat@g.us" / ".." / ".." / "escape.bin")}
            ),
        )
        from brain.services.whatsapp import MediaDownloadError

        with pytest.raises(MediaDownloadError):
            client.download("m1", "chat@g.us")

    def test_missing_path_is_refused(self, tmp_path):
        store = tmp_path / "store"
        store.mkdir()
        client = WhatsAppMediaClient(
            "http://127.0.0.1:8085",
            store_root=store,
            transport=_bridge_transport({"success": True, "message": "ok"}),
        )
        from brain.services.whatsapp import MediaDownloadError

        with pytest.raises(MediaDownloadError):
            client.download("m1", "chat@g.us")

    def test_non_regular_file_is_refused(self, tmp_path):
        store = tmp_path / "store"
        target = store / "somedir"
        target.mkdir(parents=True)
        client = WhatsAppMediaClient(
            "http://127.0.0.1:8085",
            store_root=store,
            transport=_bridge_transport({"success": True, "path": str(target)}),
        )
        from brain.services.whatsapp import MediaDownloadError

        with pytest.raises(MediaDownloadError):
            client.download("m1", "chat@g.us")

    def test_fails_closed_without_store_root(self):
        client = WhatsAppMediaClient(
            "http://127.0.0.1:8085",
            transport=_bridge_transport({"success": True, "path": "/tmp/x.bin"}),
        )
        from brain.services.whatsapp import MediaDownloadError

        with pytest.raises(MediaDownloadError):
            client.download("m1", "chat@g.us")

    def test_symlink_escape_is_refused(self, tmp_path):
        store = tmp_path / "store"
        chat_dir = store / "chat@g.us"
        chat_dir.mkdir(parents=True)
        secret = tmp_path / "outside.bin"
        secret.write_bytes(b"nope")
        link = chat_dir / "media.jpg"
        link.symlink_to(secret)
        client = WhatsAppMediaClient(
            "http://127.0.0.1:8085",
            store_root=store,
            transport=_bridge_transport({"success": True, "path": str(link)}),
        )
        from brain.services.whatsapp import MediaDownloadError

        with pytest.raises(MediaDownloadError):
            client.download("m1", "chat@g.us")


class TestGmailAttachmentShape:
    """users.messages.attachments.get returns {'size', 'data': base64url}."""

    def test_decodes_data_field(self):
        client = _gmail_client(
            {"size": 11, "data": base64.urlsafe_b64encode(b"pdf-bytes!").decode()}
        )
        assert client.get_attachment("m1", "att1") == b"pdf-bytes!"

    def test_missing_data_is_an_error_not_empty_bytes(self):
        client = _gmail_client({"size": 11})
        with pytest.raises(GmailError):
            client.get_attachment("m1", "att1")

    def test_empty_data_is_an_error(self):
        client = _gmail_client({"size": 0, "data": ""})
        with pytest.raises(GmailError):
            client.get_attachment("m1", "att1")
