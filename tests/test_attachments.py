"""F1 — attachment core: files dir, dedupe, readers, budget/queue, search.

Design delta items 43-45. SAFETY: synthetic files only; the Claude path is
exercised through a fake query_fn (offline) plus unit tests of the sandbox
permission gate. The real claude_agent_sdk behaviour was probed separately.
"""

import asyncio
import hashlib
import os
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from claude_agent_sdk import AssistantMessage, TextBlock
from qdrant_client import QdrantClient

from brain.ai.embedder import FakeEmbedder
from brain.ai.vectorstore import QdrantVectorStore
from brain.db.models import Attachment, MemoryItem
from brain.db.session import SessionLocal
from brain.services.indexing import QdrantItemIndexer


def _run(coro):
    """Run an async service call from a sync test (no async pytest plugin)."""
    return asyncio.run(coro)


def _make_pdf(pages: int) -> bytes:
    """A minimal valid PDF with `pages` pages, each showing 'PAGE <n>'.

    Handcrafted (no generator dependency) so the text-layer reader can be
    exercised for real.
    """
    objects: list[bytes] = []

    def obj(i: int, body: str) -> None:
        objects.append(f"{i} 0 obj\n{body}\nendobj\n".encode())

    kids = " ".join(f"{3 + 2 * k} 0 R" for k in range(pages))
    font_num = 3 + 2 * pages
    obj(1, "<< /Type /Catalog /Pages 2 0 R >>")
    obj(2, f"<< /Type /Pages /Kids [ {kids} ] /Count {pages} >>")
    for k in range(pages):
        content_num = 4 + 2 * k
        stream = f"BT /F1 12 Tf 72 720 Td (PAGE {k + 1}) Tj ET"
        obj(
            3 + 2 * k,
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            f"/Contents {content_num} 0 R /Resources << /Font << /F1 {font_num} 0 R >> >> >>",
        )
        body = (
            f"{content_num} 0 obj\n<< /Length {len(stream)} >>"
            f"\nstream\n{stream}\nendstream\nendobj\n"
        )
        objects.append(body.encode())
    obj(font_num, "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")

    out = bytearray(b"%PDF-1.4\n")
    offsets: list[int] = []
    for body in objects:
        offsets.append(len(out))
        out.extend(body)
    xref_at = len(out)
    out.extend(f"xref\n0 {len(objects) + 1}\n".encode())
    out.extend(b"0000000000 65535 f \n")
    for off in offsets:
        out.extend(f"{off:010d} 00000 n \n".encode())
    trailer = f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref_at}\n%%EOF\n"
    out.extend(trailer.encode())
    return bytes(out)


class FakeQuery:
    """Stands in for claude_agent_sdk.query; records calls and sandbox files."""

    def __init__(self, reply: str) -> None:
        self.reply = reply
        self.calls: list[dict] = []
        self.file_names_seen: list[str] = []

    def __call__(self, *, prompt: str, options: object) -> AsyncIterator[object]:
        self.calls.append({"prompt": prompt, "options": options})
        cwd = Path(options.cwd)  # type: ignore[attr-defined]
        self.file_names_seen = sorted(p.name for p in cwd.iterdir())
        return self._gen()

    async def _gen(self) -> AsyncIterator[object]:
        yield AssistantMessage(content=[TextBlock(text=self.reply)], model="fake-model")


def _make_service(tmp_path: Path, fake_query: FakeQuery, daily_budget: int = 50):
    from brain.services.attachments import (
        AttachmentFileStore,
        AttachmentService,
        ClaudeFileReader,
        PdfTextLayerReader,
    )

    return AttachmentService(
        store=AttachmentFileStore(root=tmp_path / "files"),
        pdf_reader=PdfTextLayerReader(),
        claude_reader=ClaudeFileReader(query_fn=fake_query, scratch_root=tmp_path / "scratch"),
        indexer=_RecordingIndexer(),
        session_factory=SessionLocal,
        daily_budget=daily_budget,
    )


class _RecordingIndexer:
    def __init__(self) -> None:
        self.indexed: list[uuid.UUID] = []

    def index(self, item: MemoryItem) -> None:
        self.indexed.append(item.id)


def _parent() -> uuid.UUID:
    with SessionLocal() as session:
        item = MemoryItem(raw_text="parent message", status="inbox")
        session.add(item)
        session.commit()
        return item.id


@pytest.fixture(name="svc_env")
def svc_env_fixture(tmp_path: Path, clean_items: None):
    """A clean items table; yields a factory bound to SessionLocal."""
    yield tmp_path
    with SessionLocal() as session:
        session.query(Attachment).delete()
        session.query(MemoryItem).delete()
        session.commit()


class TestCapture:
    def test_capture_creates_pending_attachment_and_own_item(self, svc_env):

        parent = _parent()
        svc = _make_service(svc_env, FakeQuery(reply="x"))
        att = svc.capture(
            parent_item_id=parent,
            content=_make_pdf(1),
            filename="note.pdf",
            mime="application/pdf",
            source="telegram",
            source_ref="msg-1",
        )
        assert att.status == "pending"
        assert att.parent_item_id == parent
        assert att.item_id is not None and att.item_id != parent
        # the attachment's OWN item exists, raw text starts with the filename
        with SessionLocal() as session:
            own = session.get(MemoryItem, att.item_id)
            assert own is not None and own.raw_text == "note.pdf"
        # the file is stored content-addressed ONCE
        assert att.sha256 and (svc_env / "files" / f"{att.sha256}.pdf").exists()

    def test_dedupe_same_bytes_stored_once(self, svc_env):

        parent = _parent()
        svc = _make_service(svc_env, FakeQuery(reply="x"))
        data = _make_pdf(1)
        a1 = svc.capture(parent, data, "a.pdf", "application/pdf", "telegram", "m1")
        a2 = svc.capture(parent, data, "b.pdf", "application/pdf", "telegram", "m2")
        assert a1.sha256 == a2.sha256
        # two attachment rows, one stored file
        assert len(list((svc_env / "files").iterdir())) == 1

    def test_too_large_rejected_without_read(self, svc_env):
        from brain.services.attachments import MAX_FILE_BYTES

        parent = _parent()
        fake = FakeQuery(reply="x")
        svc = _make_service(svc_env, fake)
        att = svc.capture(
            parent, b"x" * (MAX_FILE_BYTES + 1), "big.pdf", "application/pdf", "telegram", "m"
        )
        assert att.status == "too_large"
        assert fake.calls == []  # never sent anywhere


class TestReading:
    def test_pdf_text_layer_read_locally_no_claude_call(self, svc_env):

        parent = _parent()
        fake = FakeQuery(reply="should not be called")
        svc = _make_service(svc_env, fake)
        att = svc.capture(parent, _make_pdf(3), "pages.pdf", "application/pdf", "telegram", "m")
        status = _run(svc.process(att.id))
        assert status == "done"
        assert fake.calls == []  # text layer found locally — no API call
        with SessionLocal() as session:
            row = session.get(Attachment, att.id)
            assert row.text_method == "pdf_text" and row.pages == 3
            assert "PAGE 1" in row.text and "PAGE 3" in row.text
            own = session.get(MemoryItem, row.item_id)
            assert own.raw_text.startswith("pages.pdf\n")

    def test_image_pdf_without_text_layer_goes_to_claude(self, svc_env):

        parent = _parent()
        fake = FakeQuery(reply="בוקר טוב\nמחר יום שלישי\nDESCRIPTION: a synthetic school note")
        svc = _make_service(svc_env, fake)
        # an image-based PDF (rendered page image; no text layer) — use PNG
        att = svc.capture(parent, b"\x89PNG synthetic", "shot.png", "image/png", "telegram", "m")
        status = _run(svc.process(att.id))
        assert status == "done"
        assert len(fake.calls) == 1
        with SessionLocal() as session:
            row = session.get(Attachment, att.id)
            assert row.text_method == "claude"
            assert "בוקר טוב" in row.text
            assert row.error is None or "DESCRIPTION" not in row.text
        # the description lands on the row, never inside the verbatim text
        assert "synthetic school note" not in row.text

    def test_pdf_over_20_pages_is_truncated_with_marker(self, svc_env):

        parent = _parent()
        fake = FakeQuery(reply="verbatim from claude")
        svc = _make_service(svc_env, fake)
        att = svc.capture(parent, _make_pdf(25), "long.pdf", "application/pdf", "telegram", "m")
        status = _run(svc.process(att.id))
        assert status == "done"
        assert fake.calls == []  # text layer handled it locally
        with SessionLocal() as session:
            row = session.get(Attachment, att.id)
            assert "PAGE 20" in row.text
            assert "PAGE 21" not in row.text
            assert "truncat" in row.text.lower()

    def test_failure_is_recorded_never_silent(self, svc_env):

        parent = _parent()
        svc = _make_service(svc_env, FakeQuery(reply="x"))

        def boom(*a, **k):
            raise RuntimeError("sdk exploded")

        svc.claude_reader._query_fn = boom
        att = svc.capture(parent, b"\x89PNG x", "broken.png", "image/png", "telegram", "m")
        status = _run(svc.process(att.id))
        assert status == "failed"
        with SessionLocal() as session:
            row = session.get(Attachment, att.id)
            assert row.status == "failed" and "exploded" in row.error


class TestBudgetAndQueue:
    def test_daily_budget_queues_the_rest(self, svc_env):

        parent = _parent()
        svc = _make_service(svc_env, FakeQuery(reply="txt"), daily_budget=2)
        ids = [
            svc.capture(parent, b"\x89PNG %d" % i, f"i{i}.png", "image/png", "telegram", f"m{i}").id
            for i in range(3)
        ]
        assert _run(svc.process(ids[0])) == "done"
        assert _run(svc.process(ids[1])) == "done"
        assert _run(svc.process(ids[2])) == "pending"  # over budget: queued
        assert svc.read_today() == 2
        assert svc.queue_depth() == 1

    def test_pending_processed_when_budget_returns(self, svc_env):
        from datetime import timedelta

        from brain.services.attachments import Attachment

        parent = _parent()
        svc = _make_service(svc_env, FakeQuery(reply="txt"), daily_budget=1)
        a1 = svc.capture(parent, b"\x89PNG a", "a.png", "image/png", "telegram", "m1")
        a2 = svc.capture(parent, b"\x89PNG b", "b.png", "image/png", "telegram", "m2")
        assert _run(svc.process(a1.id)) == "done"
        assert _run(svc.process(a2.id)) == "pending"
        # simulate the next day
        with SessionLocal() as session:
            row = session.get(Attachment, a1.id)
            row.read_at = datetime.now(UTC) - timedelta(days=1)
            session.commit()
        _run(svc.process_pending())
        with SessionLocal() as session:
            assert session.get(Attachment, a2.id).status == "done"


class TestSandbox:
    def _reader(self, tmp_path, fake):
        from brain.services.attachments import ClaudeFileReader

        return ClaudeFileReader(query_fn=fake, scratch_root=tmp_path / "scratch")

    def test_claude_reader_options_are_locked_down(self, tmp_path, svc_env):
        parent = _parent()
        fake = FakeQuery(reply="text\nDESCRIPTION: d")

        svc = _make_service(tmp_path, fake)
        att = svc.capture(parent, b"\x89PNG", "x.png", "image/png", "telegram", "m")
        _run(svc.process(att.id))
        opts = fake.calls[0]["options"]
        assert list(opts.tools) == ["Read"]  # ONLY Read
        assert opts.strict_mcp_config is True
        assert opts.setting_sources == []
        assert opts.max_turns == 3
        assert opts.can_use_tool is not None  # the per-file gate is wired
        assert opts.cwd.startswith(str(tmp_path / "scratch"))
        expected_name = hashlib.sha256(b"\x89PNG").hexdigest() + ".png"
        assert fake.file_names_seen == [expected_name]
        # the temp dir is deleted afterwards
        assert not Path(opts.cwd).exists()

    def test_permission_gate_allows_only_the_one_file(self, tmp_path):
        import hashlib

        from brain.services.attachments import make_read_gate

        sandbox = tmp_path / "sb"
        sandbox.mkdir()
        target = sandbox / "abc.png"
        target.write_bytes(b"x")
        gate = make_read_gate(target)
        ok = _run(gate("Read", {"file_path": str(target)}, None))
        assert ok.behavior == "allow"
        for bad in [
            sandbox / "other.png",  # another path
            Path("/Users/yos.i/git/Second-brain/.env"),  # outside
            sandbox / ".." / "escape.png",  # '..' traversal
        ]:
            denied = _run(gate("Read", {"file_path": str(bad)}, None))
            assert denied.behavior == "deny", bad
        # a symlink pointing OUTSIDE the sandbox is denied too
        link = sandbox / "link.png"
        link.symlink_to(tmp_path / "outside.png")
        (tmp_path / "outside.png").write_bytes(b"secret")
        denied = _run(gate("Read", {"file_path": str(link)}, None))
        assert denied.behavior == "deny"
        # non-Read tools are always denied
        denied = _run(gate("Bash", {"command": "cat /etc/passwd"}, None))
        assert denied.behavior == "deny"
        assert hashlib.sha256  # (keep import honest)

    def test_instruction_tells_the_model_to_ignore_in_file_instructions(self, tmp_path, svc_env):
        parent = _parent()
        fake = FakeQuery(reply="x")
        svc = _make_service(svc_env, fake)
        att = svc.capture(parent, b"\x89PNG", "x.png", "image/png", "telegram", "m")
        _run(svc.process(att.id))
        opts = fake.calls[0]["options"]
        assert "ignore" in opts.system_prompt.lower()
        assert "DATA" in opts.system_prompt


class TestSearch:
    def test_attachment_text_is_retrievable_and_cites_filename(self, svc_env):
        from brain.repositories.items import ItemsRepository
        from brain.services.search import SearchService

        parent = _parent()
        store = QdrantVectorStore(client=QdrantClient(":memory:"))
        indexer = QdrantItemIndexer(FakeEmbedder(dim=8), store)
        fake = FakeQuery(reply="להביא כובע ובקבוק מים\nDESCRIPTION: note")
        svc = _make_service(svc_env, fake)
        svc._indexer = indexer
        att = svc.capture(parent, b"\x89PNG", "hat-note.png", "image/png", "telegram", "m")
        _run(svc.process(att.id))
        with SessionLocal() as session:
            repo = ItemsRepository(session)

            class _NoRerank:
                def rerank(self, query, candidates, top_n):
                    return candidates[:top_n]

            search = SearchService(repo, FakeEmbedder(dim=8), store, reranker=_NoRerank())
            hits = search.search("כובע", limit=5)
        assert hits, "attachment text must be searchable"
        assert hits[0].item.raw_text.startswith("hat-note.png\n")


class TestOpsEndpoint:
    def test_ops_attachments_reports_queue_and_budget(self, svc_env, monkeypatch):
        from fastapi.testclient import TestClient

        import brain.api.deps as deps
        from brain.config import get_settings
        from brain.main import create_app

        parent = _parent()
        fake = FakeQuery(reply="txt")
        svc = _make_service(svc_env, fake, daily_budget=7)
        svc.capture(parent, b"\x89PNG a", "a.png", "image/png", "telegram", "m")
        svc.capture(parent, b"\x89PNG b", "b.png", "image/png", "telegram", "m2")

        class _StubOps:
            def queue_depth(self):
                return svc.queue_depth()

            def read_today(self):
                return svc.read_today()

            daily_budget = 7

        monkeypatch.setattr(deps, "_attachment_service", lambda: _StubOps())
        get_settings.cache_clear()
        try:
            client = TestClient(create_app())
            body = client.get("/ops/attachments").json()
        finally:
            get_settings.cache_clear()
        assert body["daily_budget"] == 7
        assert body["read_today"] == 0
        assert body["queue_depth"] == 0  # capture does not enqueue by itself


class TestGateRelativePaths:
    """The prompt names the file; Claude may call Read with a RELATIVE path.

    The gate must resolve a relative file_path against the SANDBOX dir (not
    the python process cwd) before the realpath comparison.
    """

    def _sandbox(self, tmp_path: Path):
        sandbox = tmp_path / "sb"
        sandbox.mkdir()
        target = sandbox / "abc.png"
        target.write_bytes(b"x")
        return sandbox, target

    def test_relative_name_is_allowed(self, tmp_path):
        from brain.services.attachments import make_read_gate

        sandbox, target = self._sandbox(tmp_path)
        gate = make_read_gate(target)
        old = os.getcwd()
        os.chdir(tmp_path)  # cwd deliberately != sandbox (proves it is not used)
        try:
            ok = _run(gate("Read", {"file_path": "abc.png"}, None))
            assert ok.behavior == "allow"
        finally:
            os.chdir(old)

    def test_relative_dotdot_is_denied(self, tmp_path):
        from brain.services.attachments import make_read_gate

        sandbox, target = self._sandbox(tmp_path)
        gate = make_read_gate(target)
        denied = _run(gate("Read", {"file_path": "../x.png"}, None))
        assert denied.behavior == "deny"

    def test_absolute_other_file_is_denied(self, tmp_path):
        from brain.services.attachments import make_read_gate

        sandbox, target = self._sandbox(tmp_path)
        other = tmp_path / "other.png"
        other.write_bytes(b"y")
        gate = make_read_gate(target)
        denied = _run(gate("Read", {"file_path": str(other)}, None))
        assert denied.behavior == "deny"

    def test_prompt_names_the_absolute_path(self, tmp_path, svc_env):
        parent = _parent()
        fake = FakeQuery(reply="txt")
        svc = _make_service(svc_env, fake)
        att = svc.capture(parent, b"\x89PNG", "x.png", "image/png", "telegram", "m")
        _run(svc.process(att.id))
        prompt = fake.calls[0]["prompt"]
        assert "/brain-attach-" in prompt  # the sandbox cwd appears in full
        assert prompt.strip().endswith(".png and transcribe it.")  # named by sha256, absolute


class TestReaderTimeout:
    def test_hung_reader_times_out(self, tmp_path):
        """A query_fn that never yields must not block the queue forever."""
        import pytest

        from brain.services.attachments import AttachmentError, ClaudeFileReader

        async def hung(*, prompt, options):
            await asyncio.sleep(3600)
            yield None  # pragma: no cover

        reader = ClaudeFileReader(query_fn=hung, timeout_s=0.2, scratch_root=tmp_path / "s")
        with pytest.raises(AttachmentError, match="timed out"):
            _run(reader.read(b"\x89PNG", ".png"))
