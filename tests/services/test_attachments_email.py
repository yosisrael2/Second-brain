"""F3 (item 46): the Gmail poller hands attachment BYTES to the attachments core.

The F2 poller stores attachment REFERENCES only; F3 fetches the bytes via
GmailClient.get_attachment (users.messages.attachments.get, still read-only)
and captures each as its own attachment item (source='email', parent = the
email item). The 20 MB limit is respected BEFORE the fetch; failures are
recorded, never silent; reading goes through the daily budget.
"""

from typing import Any

from tests.test_attachments import FakeQuery, _make_service  # noqa: F401

from brain.ai.gmail import GmailAttachmentRef, GmailMessage
from brain.services.email_poller import EmailPoller


class FakeGmailClientWithAttachments:
    """list/get like F2's fake, plus scripted get_attachment bytes."""

    def __init__(
        self,
        messages: list[GmailMessage],
        blobs: dict[str, bytes] | None = None,
        error: Exception | None = None,
    ) -> None:
        self._messages = {m.id: m for m in messages}
        self._blobs = blobs or {}
        self._error = error
        self.list_calls: list[dict[str, Any]] = []
        self.get_calls: list[str] = []
        self.attachment_calls: list[tuple[str, str]] = []

    def list_messages(self, *, query: str, label_ids: list[str]) -> list[Any]:
        self.list_calls.append({"query": query, "label_ids": list(label_ids)})
        from brain.ai.gmail import GmailMessageStub

        return [GmailMessageStub(id=m.id, thread_id=m.thread_id) for m in self._messages.values()]

    def get_message(self, message_id: str) -> GmailMessage:
        self.get_calls.append(message_id)
        return self._messages[message_id]

    def get_attachment(self, message_id: str, attachment_id: str) -> bytes:
        self.attachment_calls.append((message_id, attachment_id))
        if self._error is not None:
            raise self._error
        return self._blobs[attachment_id]

    def close(self) -> None: ...


class FakeIngest:
    def __init__(self) -> None:
        self.created: list[dict[str, Any]] = []

    def capture_parsed(self, parsed, *, source=None, extra_meta=None, occurred_at=None):
        self.created.append({"source": source, "meta": extra_meta})

        class _Item:
            id = f"item-{len(self.created)}"

        return [_Item()]


class FakeSeenRepo:
    def __init__(self) -> None:
        self.seen_ids: set[str] = set()
        self.seen_gmail_ids: set[str] = set()

    def seen(self, dedupe_id: str) -> bool:
        return dedupe_id in self.seen_ids

    def seen_gmail(self, gmail_id: str) -> bool:
        return gmail_id in self.seen_gmail_ids

    def mark(self, dedupe_id, gmail_id, rfc_message_id, item_id) -> None:
        self.seen_ids.add(dedupe_id)
        self.seen_gmail_ids.add(gmail_id)


class _RealIngest:
    """Captures the email as a REAL memory_items row (parent for the FK)."""

    def __init__(self) -> None:
        self.last_item_id: Any = None

    def capture_parsed(self, parsed, *, source=None, extra_meta=None, occurred_at=None):
        from brain.db.models import MemoryItem
        from brain.db.session import SessionLocal

        with SessionLocal() as session:
            item = MemoryItem(raw_text="email body", status="inbox", meta=extra_meta)
            session.add(item)
            session.commit()
            self.last_item_id = item.id

        class _I:
            id = self.last_item_id

        return [_I()]


class _RecordingAttachments:
    """Wraps the real service; records wiring calls for assertions."""

    def __init__(self, svc: Any) -> None:
        self.svc = svc
        self.captured: list[dict[str, Any]] = []
        self.submitted: list[Any] = []
        self.drains = 0

    def capture(self, parent_item_id, content, filename, mime, source, source_ref=None):
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
        return self.svc.capture(parent_item_id, content, filename, mime, source, source_ref)

    def submit(self, attachment_id: Any) -> None:
        self.submitted.append(attachment_id)
        self.svc.submit(attachment_id)

    def drain(self) -> int:
        self.drains += 1
        return self.svc.drain()


def _message_with_attachment(
    mid: str = "m1",
    att_id: str = "att1",
    size: int = 100,
    filename: str = "note.pdf",
    mime: str = "application/pdf",
) -> GmailMessage:
    from datetime import UTC, datetime

    return GmailMessage(
        id=mid,
        thread_id="t1",
        rfc_message_id=f"<{mid}@b>",
        sender="teacher@example.com",
        subject="with attachment",
        internal_date=datetime(2026, 10, 23, 8, 0, tzinfo=UTC),
        body_text="see attached",
        label_ids=["Label_school"],
        attachments=[
            GmailAttachmentRef(attachment_id=att_id, filename=filename, mime_type=mime, size=size)
        ],
    )


class FakeAttachmentService:
    """Records capture/submit calls (no DB)."""

    def __init__(self) -> None:
        self.captured: list[dict[str, Any]] = []
        self.submitted: list[Any] = []
        self.drains = 0

    def capture(self, parent_item_id, content, filename, mime, source, source_ref=None):
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
        return SimpleAtt(id=f"att-{len(self.captured)}", item_id=f"own-{len(self.captured)}")

    def submit(self, attachment_id) -> None:
        self.submitted.append(attachment_id)

    def drain(self) -> int:
        self.drains += 1
        return 0


class SimpleAtt:
    def __init__(self, id: Any, item_id: Any) -> None:  # noqa: A002
        self.id = id
        self.item_id = item_id


def _poller(
    client: FakeGmailClientWithAttachments,
    attachments: Any,
    ingest: Any | None = None,
):
    from brain.config import Settings

    ingest = ingest or FakeIngest()
    poller = EmailPoller(
        client=client,
        ingest=ingest,
        seen_repo=FakeSeenRepo(),
        settings=Settings(gmail_label_id="Label_school", gmail_credentials_path="", _env_file=None),
        attachments=attachments,
    )
    return poller, ingest


class TestEmailAttachments:
    def test_attachment_bytes_captured_with_source_email(self, svc_env):
        from tests.test_attachments import _make_service

        client = FakeGmailClientWithAttachments(
            [_message_with_attachment(filename="photo.png", mime="image/png")],
            blobs={"att1": b"\x89PNG fake bytes"},
        )
        real = _make_service(svc_env, FakeQuery(reply="the attached note"))
        svc = _RecordingAttachments(real)
        poller, ingest = _poller(client, svc, ingest=_RealIngest())
        summary = poller.poll_once()
        assert summary.new == 1
        assert client.attachment_calls == [("m1", "att1")]
        assert len(svc.captured) == 1
        call = svc.captured[0]
        assert call["source"] == "email"
        assert call["content"] == b"\x89PNG fake bytes"
        assert call["filename"] == "photo.png"
        assert call["mime"] == "image/png"
        assert call["source_ref"] == "m1"
        assert svc.submitted and svc.drains == 1  # queued, drained once per poll
        from brain.db.models import Attachment, MemoryItem
        from brain.db.session import SessionLocal

        with SessionLocal() as session:
            # Scoped to THIS test's email item (T1): a stray attachment row in
            # the shared *_test db from an interrupted session must not break it.
            row = session.query(Attachment).filter_by(parent_item_id=ingest.last_item_id).one()
            assert row.source == "email" and row.status == "done"
            assert "the attached note" in (row.text or "")
            parent = session.get(MemoryItem, row.parent_item_id)
            assert parent is not None, "the email item is the attachment's parent"
            assert parent.meta["gmail_id"] == "m1"

    def test_email_attachment_test_ignores_stray_rows(self, svc_env):
        """T1: the DB assertions must be scoped to THIS test's own parent item.

        A stray attachment row left in the shared *_test database by an
        interrupted earlier session used to break query(Attachment).one()
        on the whole table. Plants one stray row (its own parent item, so FK
        holds) and then runs the test body above verbatim: it must still
        pass. svc_env's teardown removes both the stray and the real rows.
        """
        from brain.db.models import Attachment, MemoryItem
        from brain.db.session import SessionLocal

        with SessionLocal() as session:
            stray_parent = MemoryItem(raw_text="stray from an earlier session", status="inbox")
            session.add(stray_parent)
            session.flush()
            session.add(
                Attachment(
                    item_id=stray_parent.id,
                    parent_item_id=stray_parent.id,
                    sha256="0" * 64,
                    mime="text/plain",
                    filename="stray.txt",
                    size_bytes=4,
                    source="email",
                    source_ref="stray-m1",
                    status="done",
                )
            )
            session.commit()

        TestEmailAttachments().test_attachment_bytes_captured_with_source_email(svc_env=svc_env)

    def test_too_large_reference_is_skipped_before_the_fetch(self, svc_env):
        from brain.services.attachments import MAX_FILE_BYTES

        client = FakeGmailClientWithAttachments(
            [_message_with_attachment(size=MAX_FILE_BYTES + 1)], blobs={"att1": b"x"}
        )
        svc = FakeAttachmentService()
        poller, _ = _poller(client, svc)
        summary = poller.poll_once()
        assert summary.new == 1  # the email itself is captured as usual
        assert client.attachment_calls == []  # never fetched
        assert svc.captured == []

    def test_fetch_failure_recorded_never_silent(self, svc_env, monkeypatch):
        events: list[tuple] = []
        import brain.services.email_poller as ep

        monkeypatch.setattr(
            ep.ops,
            "record_event",
            lambda component, kind, **kw: events.append((component, kind, kw)),
        )
        client = FakeGmailClientWithAttachments(
            [_message_with_attachment()], error=RuntimeError("gmail 500")
        )
        svc = FakeAttachmentService()
        poller, _ = _poller(client, svc)
        summary = poller.poll_once()
        assert summary.new == 1  # the email still captured
        assert svc.captured == []
        assert summary.state == "ok"
        assert any(kind == "attachment_fetch_error" for _, kind, _ in events), (
            "the failure must be recorded in ops, not swallowed"
        )

    def test_budget_via_drain(self, svc_env):
        """Two attachments, budget 1: capture both, drain reads at most the budget.

        The poller's job is capture + submit + one drain; the budget cap
        itself lives in AttachmentService (proven in test_attachments).
        """
        from tests.test_attachments import FakeQuery

        from brain.db.models import Attachment, MemoryItem
        from brain.db.session import SessionLocal
        from brain.services.attachments import AttachmentService

        class _RealIngest:
            def capture_parsed(self, parsed, *, source=None, extra_meta=None, occurred_at=None):
                with SessionLocal() as session:
                    item = MemoryItem(raw_text="email body", status="inbox")
                    session.add(item)
                    session.commit()

                    class _I:
                        id = item.id

                    return [_I()]

        class _RealAttachments:
            def __init__(self, svc: AttachmentService) -> None:
                self.svc = svc
                self.submitted: list[Any] = []
                self.drains = 0

            def capture(self, parent_item_id, content, filename, mime, source, source_ref=None):
                return self.svc.capture(parent_item_id, content, filename, mime, source, source_ref)

            def submit(self, attachment_id) -> None:
                self.submitted.append(attachment_id)
                self.svc.submit(attachment_id)

            def drain(self) -> int:
                self.drains += 1
                return self.svc.drain()

        msg = _message_with_attachment(mid="m1", att_id="a1", filename="a.png", mime="image/png")
        msg2 = _message_with_attachment(mid="m2", att_id="a2", filename="b.png", mime="image/png")
        msg2.attachments[0] = GmailAttachmentRef(
            attachment_id="a2", filename="b.png", mime_type="image/png", size=10
        )
        client = FakeGmailClientWithAttachments(
            [msg, msg2], blobs={"a1": b"\x89PNG one", "a2": b"\x89PNG two"}
        )
        fake = FakeQuery(reply="txt\nDESCRIPTION: d")
        real = _make_service(svc_env, fake, daily_budget=1)
        wrapper = _RecordingAttachments(real)
        poller, _ = _poller(client, wrapper, ingest=_RealIngest())
        poller.poll_once()
        with SessionLocal() as session:
            statuses = [a.status for a in session.query(Attachment).all()]
        assert statuses.count("done") == 1  # budget 1: exactly one read
        assert "pending" in statuses  # the second is queued, not lost
