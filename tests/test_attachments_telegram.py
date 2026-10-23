"""F1 — Telegram attachment input (design delta item 45).

Photo (largest size), image document, PDF document; the caption becomes the
parent's text; immediate "📎 saved, reading…" then a follow-up summary once
read. .ics unchanged; voice/video stay unsupported. All offline fakes.
"""

import asyncio
import uuid
from contextlib import contextmanager
from types import SimpleNamespace

from tests.test_telegram_bot import (
    FULL_CLASSIFICATION,
    FakeRepository,
    StubClassifier,
    _StubIndexer,
)

from brain.services.ingest import IngestService
from brain.services.items import ItemsService
from brain.telegram_bot import BotServices, TelegramBot, _handle_update


class _Tg:
    """Fake Telegram adapter: records sends, serves canned file bytes."""

    def __init__(self, sent: list[str]) -> None:
        self.sent = sent

    async def send_message(self, chat_id, text, **kwargs):
        self.sent.append(text)

    async def get_file(self, file_id):
        return SimpleNamespace(download_as_bytearray=lambda: FILES.get(file_id, b"PNG-bytes"))


FILES = {"doc1": b"%PDF-1.4 synthetic", "large": b"PNG-bytes", "small": b"tiny"}


def _update(message):
    return SimpleNamespace(
        update_id=1,
        effective_user=SimpleNamespace(id=111),
        effective_message=message,
        callback_query=None,
    )


class FakeAttachmentService:
    """AttachmentService stand-in recording capture/process calls."""

    def __init__(self) -> None:
        self.captured: list[dict] = []
        self.processed: list[uuid.UUID] = []
        self.gets: list[uuid.UUID] = []
        self._status = "done"

    def capture(self, parent_item_id, content, filename, mime, source, source_ref=None):
        att = SimpleNamespace(
            id=uuid.uuid4(),
            parent_item_id=parent_item_id,
            filename=filename,
            mime=mime,
            status="pending",
            text=None,
            error=None,
            text_method=None,
            source=source,
            source_ref=source_ref,
            sha256="ab" * 32,
        )
        self.captured.append(
            {
                "parent": parent_item_id,
                "content": content,
                "filename": filename,
                "mime": mime,
                "source": source,
                "source_ref": source_ref,
            }
        )
        return att

    async def process(self, attachment_id):
        self.processed.append(attachment_id)
        return self._status

    def get(self, attachment_id):
        self.gets.append(attachment_id)
        return SimpleNamespace(
            id=attachment_id,
            filename="photo.jpg",
            status=self._status,
            text_method="claude",
            error=None,
            text="שיעורי בית למחר",
            pages=None,
        )

    def own_item(self, attachment_id):
        return SimpleNamespace(meta={"description": "a synthetic classroom photo"})

    def queue_depth(self):
        return 0

    def read_today(self):
        return 0


class FakeTg:
    def __init__(self, files: dict[str, bytes]) -> None:
        self.sent: list[str] = []
        self._files = files

    async def send_message(self, chat_id, text, **kwargs):
        self.sent.append(text)

    async def get_file(self, file_id):
        return SimpleNamespace(download_as_bytearray=lambda: self._files[file_id])


def _wired(attachments: FakeAttachmentService, repo=None) -> tuple:
    """A TelegramBot whose BotServices carries a FakeAttachmentService."""
    repo = repo or FakeRepository()
    indexer = _StubIndexer()

    @contextmanager
    def factory():
        yield BotServices(
            ingest=IngestService(repo, StubClassifier(result=FULL_CLASSIFICATION), indexer),
            items=ItemsService(repo, indexer),
            search=SimpleNamespace(search=lambda q, *, limit, offset=0: []),
            ask=SimpleNamespace(ask=lambda q: SimpleNamespace(answer="x", cited_ids=[])),
            attachments=attachments,
        )

    core = TelegramBot(
        services_factory=factory,
        allowed_ids=frozenset({111}),
        timezone=__import__("zoneinfo", fromlist=["ZoneInfo"]).ZoneInfo("Asia/Jerusalem"),
    )
    return core, repo


def _drain():
    async def go():
        pending = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)

    return asyncio.run(go())


def _photo_message(caption="היום בכיתה"):
    return SimpleNamespace(
        text=None,
        chat_id=555,
        message_id=901,
        document=None,
        caption=caption,
        photo=[
            SimpleNamespace(file_id="small", width=90, height=90),
            SimpleNamespace(file_id="large", width=1280, height=720),
        ],
    )


def _doc_message(filename, mime, caption="הצעה לטיול"):
    return SimpleNamespace(
        text=None,
        chat_id=555,
        message_id=902,
        caption=caption,
        document=SimpleNamespace(file_id="doc1", file_name=filename, mime_type=mime),
        photo=None,
    )


def _update(message):
    return SimpleNamespace(
        update_id=1,
        effective_user=SimpleNamespace(id=111),
        effective_message=message,
        callback_query=None,
    )


class TestTelegramAttachments:
    def test_photo_saved_with_caption_then_summary(self):
        fake = FakeAttachmentService()
        core, repo = _wired(fake)
        sent: list[str] = []
        tg = _Tg(sent)

        async def go():
            await _handle_update(tg, core, _update(_photo_message()))
            await asyncio.sleep(0.05)

        asyncio.run(go())
        assert fake.captured, "photo must reach the attachment service"
        call = fake.captured[0]
        assert call["source"] == "telegram"
        assert call["mime"] == "image/jpeg"
        assert call["content"] == b"PNG-bytes"  # downloaded from the LARGEST size
        parent = next(i for i in repo.items.values() if i.id == call["parent"])
        assert parent.raw_text == "היום בכיתה"  # caption is the parent text
        assert fake.processed, "reading must start"
        # immediate reply, then a follow-up summary
        assert sent[0] == "📎 saved, reading…"
        assert any("photo.jpg" in s for s in sent[1:])

    def test_pdf_document_saved(self):
        fake = FakeAttachmentService()
        core, repo = _wired(fake)
        assert repo is not None
        sent: list[str] = []
        tg = _Tg(sent)

        async def go():
            await _handle_update(tg, core, _update(_doc_message("trip.pdf", "application/pdf")))
            await asyncio.sleep(0.05)

        asyncio.run(go())
        assert fake.captured[0]["mime"] == "application/pdf"
        parent = next(i for i in repo.items.values() if i.id == fake.captured[0]["parent"])
        assert parent.raw_text == "הצעה לטיול"

    def test_image_document_saved(self):
        fake = FakeAttachmentService()
        core, repo = _wired(fake)
        sent: list[str] = []
        tg = _Tg(sent)

        async def go():
            await _handle_update(
                tg, core, _update(_doc_message("pic.png", "image/png", caption="look"))
            )
            await asyncio.sleep(0.05)

        asyncio.run(go())
        assert fake.captured[0]["mime"] == "image/png"

    def test_ics_document_unchanged(self):
        fake = FakeAttachmentService()
        core, repo = _wired(fake)
        sent: list[str] = []
        tg = _Tg(sent)

        async def go():
            await _handle_update(
                tg, core, _update(_doc_message("cal.ics", "text/calendar", caption=None))
            )
            await asyncio.sleep(0.05)

        asyncio.run(go())
        assert fake.captured == []  # .ics keeps its own path, not attachments

    def test_caption_empty_parent_falls_back_to_filename(self):
        fake = FakeAttachmentService()
        core, repo = _wired(fake)
        assert repo is not None
        sent: list[str] = []
        tg = _Tg(sent)

        async def go():
            msg = _doc_message("report.pdf", "application/pdf", caption=None)
            await _handle_update(tg, core, _update(msg))
            await asyncio.sleep(0.05)

        asyncio.run(go())
        parent = next(i for i in repo.items.values() if i.id == fake.captured[0]["parent"])
        assert parent.raw_text == "[attachment: report.pdf]"

    def test_failure_reports_and_records(self):
        fake = FakeAttachmentService()

        async def boom(att_id):
            return "failed"

        fake.process = boom
        core, repo = _wired(fake)
        sent: list[str] = []
        tg = _Tg(sent)

        async def go():
            await _handle_update(tg, core, _update(_doc_message("x.png", "image/png")))
            await asyncio.sleep(0.05)

        asyncio.run(go())
        assert any("⚠️" in s or "failed" in s.lower() for s in sent), sent
