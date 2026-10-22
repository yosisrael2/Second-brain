"""Telegram bot tests: allowlist, commands, capture, events, poll semantics.

The update-processing core is PTB-agnostic and runs without network; the
poll-loop tests use a duck-typed fake Bot plus python-telegram-bot's real
exception types (skipped when the optional `telegram` extra is missing).
"""

import asyncio
import logging
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from tests.fakes import FULL_CLASSIFICATION, FakeRepository, StubClassifier

from brain.ai.ask import FakeLLMClient, LLMError
from brain.config import Settings
from brain.db.enums import ItemStatus
from brain.db.models import MemoryItem
from brain.services.ask import AskResult, AskService
from brain.services.ingest import IngestService
from brain.services.search import SearchHit
from brain.telegram_bot import (
    BotServices,
    TelegramBot,
    _ops_status_lines,
    heartbeat,
    record_event,
)

JERUSALEM = ZoneInfo("Asia/Jerusalem")


def _make_settings(**overrides: Any) -> Settings:
    payload: dict[str, Any] = {
        "TELEGRAM_BOT_TOKEN": "123:abc",
        "TELEGRAM_ALLOWED_USER_IDS": "111, 222",
        "TIMEZONE": "Asia/Jerusalem",
        "_env_file": None,  # never read the developer's real .env in tests
    }
    payload.update(overrides)
    return Settings(**payload)


class _StubIndexer:
    """Records index() calls; never touches Qdrant."""

    def __init__(self) -> None:
        self.indexed: list[MemoryItem] = []

    def index(self, item: MemoryItem) -> None:
        self.indexed.append(item)

    def index_batch(self, items: list[MemoryItem]) -> int:
        self.indexed.extend(items)
        return len(items)

    def probe(self) -> None:
        return None

    def purge(self) -> int:
        return 0


class _StubSearch:
    """SearchService stand-in returning prebuilt hits."""

    def __init__(self, hits: list[SearchHit] | None = None) -> None:
        self.hits = hits or []

    def search(self, query: str, *, limit: int, offset: int = 0) -> list[SearchHit]:
        return self.hits[:limit]


class _StubAsk:
    """AskService stand-in with a canned result or a raise."""

    def __init__(self, result: AskResult | None = None, error: Exception | None = None) -> None:
        self._result = result or AskResult(answer="42", cited_ids=[])
        self._error = error
        self.questions: list[str] = []

    def ask(self, question: str) -> AskResult:
        self.questions.append(question)
        if self._error is not None:
            raise self._error
        return self._result


class _ExplodingAsk:
    def ask(self, question: str) -> AskResult:
        raise LLMError("llm down")


class _StubItems:
    def __init__(self, items: list[MemoryItem] | None = None) -> None:
        self._items = items or []

    def list(self, *, item_type=None, project=None, status=None, limit=50, offset=0):
        return [item for item in self._items if status is None or item.status == status][
            offset : offset + limit
        ]


@dataclass
class _Events:
    """Monkeypatched ops shim capturing heartbeats + events."""

    heartbeats: list = field(default_factory=list)
    events: list = field(default_factory=list)

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import brain.telegram_bot as tb

        events = self

        def fake_heartbeat(component: str, detail: dict | None = None) -> None:
            events.heartbeats.append((component, detail))

        def fake_record(
            component: str,
            kind: str,
            ok: bool,
            latency_ms: int | None = None,
            detail: dict | None = None,
        ) -> None:
            events.events.append((kind, ok, detail))

        monkeypatch.setattr(tb, "heartbeat", fake_heartbeat)
        monkeypatch.setattr(tb, "record_event", fake_record)


def _core(
    *,
    repo: FakeRepository | None = None,
    search_hits: list[SearchHit] | None = None,
    ask: Any = None,
    inbox: list[MemoryItem] | None = None,
) -> tuple:
    """A TelegramBot wired to fakes; returns (core, repo)."""

    repo = repo or FakeRepository()
    indexer = _StubIndexer()
    search = _StubSearch(search_hits)
    ask_service = ask or AskService(search, FakeLLMClient(reply="42"))
    items_service = _StubItems(inbox or [])

    @contextmanager
    def factory():
        yield BotServices(
            ingest=IngestService(repo, StubClassifier(result=FULL_CLASSIFICATION), indexer),
            items=items_service,
            search=search,
            ask=ask_service,
        )

    core = TelegramBot(
        services_factory=factory,
        allowed_ids=frozenset({111, 222}),
        timezone=JERUSALEM,
    )
    return core, repo


def _fake_update(
    update_id: int = 1,
    user_id: int = 111,
    text: str | None = "hello note",
    document: Any = None,
    caption: str | None = None,
    callback: Any = None,
) -> SimpleNamespace:
    message = SimpleNamespace(
        text=text,
        chat_id=555,
        document=document,
        caption=caption,
    )
    return SimpleNamespace(
        update_id=update_id,
        effective_user=SimpleNamespace(id=user_id),
        effective_message=None if callback is not None else message,
        message=message,
        callback_query=callback,
    )


class _FakeTG:
    """Duck-typed telegram.Bot: scripted get_updates batches, recorded sends."""

    def __init__(self, batches: list) -> None:
        self._batches = list(batches)
        self.sent: list[tuple[int, str]] = []
        self.initialized = False
        self.shutdowns = 0

    async def initialize(self) -> None:
        self.initialized = True

    async def shutdown(self) -> None:
        self.shutdowns += 1

    async def get_updates(self, offset=None, timeout=0):
        if not self._batches:
            raise RuntimeError("no more scripted batches")
        batch = self._batches.pop(0)
        if batch and isinstance(batch[0], Exception):
            raise batch[0]
        return batch

    async def send_message(self, chat_id=None, text=None):
        self.sent.append((chat_id, text))


def _run_poll(tg, core):
    """poll_loop with a short poll timeout so a broken loop fails fast."""
    from brain.telegram_bot import poll_loop as pl

    return pl(tg, core, poll_timeout_s=1)


# --- configuration -----------------------------------------------------------


class TestTelegramSettings:
    def test_token_requires_allowlist(self):
        with pytest.raises(Exception, match="TELEGRAM_ALLOWED_USER_IDS is required"):
            _make_settings(TELEGRAM_ALLOWED_USER_IDS="")

    def test_allowlist_parses_comma_separated_ids(self):
        assert _make_settings().telegram_allowed_ids == frozenset({111, 222})

    def test_token_is_masked_in_str(self):
        assert "abc" not in str(_make_settings().telegram_bot_token)

    def test_bad_allowlist_format_is_rejected(self):
        with pytest.raises(Exception, match="comma-separated integers"):
            _make_settings(TELEGRAM_ALLOWED_USER_IDS="ada,bob")


# --- core: routing, capture, commands ----------------------------------------


class TestCore:
    def test_capture_creates_item_with_channel_meta(self, monkeypatch):
        _Events().install(monkeypatch)
        core, repo = _core()

        reply = core.route("Read the RAG paper and take notes", 111)

        assert reply.startswith("✅ Captured 1 item(s) (note)")
        assert len(repo.items) == 1
        item = next(iter(repo.items.values()))
        assert item.meta["channel"] == "telegram"
        assert item.source == "note"
        assert item.occurred_at is None

    def test_capture_whatsapp_thread_joins(self, monkeypatch):
        _Events().install(monkeypatch)
        core, repo = _core()

        reply = core.route(
            "[21/10/2026, 09:00:00] Dana: one\n[21/10/2026, 09:01:00] Rami: two", 111
        )

        assert "1 item(s) (whatsapp)" in reply
        assert len(repo.items) == 1
        assert next(iter(repo.items.values())).meta["count"] == 2

    def test_capture_timezone_makes_occurred_at_aware(self, monkeypatch):
        _Events().install(monkeypatch)
        core, repo = _core()

        core.route("[21/10/2026, 09:00:00] Dana: hi", 111)

        item = next(iter(repo.items.values()))
        assert item.occurred_at == datetime(2026, 10, 21, 6, 0, tzinfo=UTC)

    def test_search_returns_hits(self, monkeypatch):
        _Events().install(monkeypatch)
        item = MemoryItem(id=uuid.uuid4(), raw_text="Found the meeting notes", source="note")
        core, _ = _core(search_hits=[SearchHit(item=item, score=0.9)])

        reply = core.route("/search meeting", 111)

        assert "1 result(s) for 'meeting'" in reply
        assert "Found the meeting notes" in reply

    def test_search_empty_query_gets_usage(self, monkeypatch):
        events = _Events()
        events.install(monkeypatch)
        core, _ = _core()

        assert core.route("/search", 111).startswith("Usage:")
        assert events.events[-1] == ("search", False, {"error": "empty query"})

    def test_ask_answers(self, monkeypatch):
        _Events().install(monkeypatch)
        core, _ = _core()

        assert "42" in core.route("/ask meaning of life?", 111)

    def test_ask_llm_error_is_friendly(self, monkeypatch):
        events = _Events()
        events.install(monkeypatch)
        core, _ = _core(ask=_ExplodingAsk())

        reply = core.route("/ask what?", 111)

        assert reply.startswith("⚠️ LLM unavailable")
        assert events.events[-1][0] == "ask" and events.events[-1][1] is False

    def test_review_lists_inbox(self, monkeypatch):
        _Events().install(monkeypatch)
        item = MemoryItem(
            id=uuid.uuid4(), raw_text="inbox candidate", source="note", status=ItemStatus.INBOX
        )
        core, _ = _core(inbox=[item])

        reply = core.route("/review", 111)

        assert "Inbox" in reply and "inbox candidate" in reply

    def test_status_without_cockpit(self, monkeypatch):
        _Events().install(monkeypatch)
        core, _ = _core()

        assert core.route("/status", 111) == "cockpit not installed yet"

    def test_start_and_help(self, monkeypatch):
        _Events().install(monkeypatch)
        core, _ = _core()

        assert "Second Brain bot ready" in core.route("/start", 111)
        assert "/search" in core.route("/help", 111)


# --- poll loop ---------------------------------------------------------------


class TestPollLoop:
    def test_processes_allowed_update_and_replies(self, monkeypatch):
        events = _Events()
        events.install(monkeypatch)
        core, repo = _core()
        tg = _FakeTG([[_fake_update(text="hello note")], [RuntimeError("stop")]])

        with pytest.raises(RuntimeError, match="stop"):
            asyncio.run(_run_poll(tg, core))

        assert len(tg.sent) == 1 and tg.sent[0][0] == 555
        assert "✅" in tg.sent[0][1]
        assert len(repo.items) == 1
        assert events.heartbeats and events.heartbeats[0][1]["pending"] == 1
        assert events.events[0][0] == "capture"

    def test_conflict_records_and_stops(self, monkeypatch):
        telegram = pytest.importorskip("telegram")
        events = _Events()
        events.install(monkeypatch)
        core, _ = _core()
        tg = _FakeTG([[telegram.error.Conflict("terminated by other getUpdates request")]])

        with pytest.raises(telegram.error.Conflict):
            asyncio.run(_run_poll(tg, core))

        assert events.events[0][0] == "conflict" and events.events[0][1] is False
        assert tg.shutdowns == 1

    def test_rejected_user_gets_no_reply_and_is_logged(
        self, monkeypatch, caplog: pytest.LogCaptureFixture
    ):
        _Events().install(monkeypatch)
        core, repo = _core()
        tg = _FakeTG([[_fake_update(user_id=999, text="sneak")], [RuntimeError("stop")]])

        with caplog.at_level(logging.INFO, logger="brain.telegram_bot"):
            with pytest.raises(RuntimeError, match="stop"):
                asyncio.run(_run_poll(tg, core))

        assert tg.sent == []  # strangers never get a reply
        assert repo.items == {}
        assert "rejected update from user id 999" in caplog.text

    def test_document_is_captured(self, monkeypatch):
        _Events().install(monkeypatch)
        core, repo = _core()
        document = SimpleNamespace(file_name="contract.pdf")
        tg = _FakeTG(
            [
                [_fake_update(text=None, document=document, caption="the DPA")],
                [RuntimeError("stop")],
            ]
        )

        with pytest.raises(RuntimeError, match="stop"):
            asyncio.run(_run_poll(tg, core))

        assert "📎 Saved contract.pdf" in tg.sent[0][1]
        assert any(i.raw_text.startswith("Document: contract.pdf") for i in repo.items.values())

    def test_callback_query_is_acknowledged(self, monkeypatch):
        pytest.importorskip("telegram")
        core, _ = _core()
        acked: list[bool] = []

        async def _answer() -> None:
            acked.append(True)

        callback = SimpleNamespace(answer=_answer)
        tg = _FakeTG([[_fake_update(callback=callback)], [RuntimeError("stop")]])

        with pytest.raises(RuntimeError, match="stop"):
            asyncio.run(_run_poll(tg, core))

        assert acked == [True]

    def test_heartbeat_reports_allowed_users_and_pending(self, monkeypatch):
        events = _Events()
        events.install(monkeypatch)
        core, _ = _core()
        tg = _FakeTG([[], [RuntimeError("stop")]])

        with pytest.raises(RuntimeError, match="stop"):
            asyncio.run(_run_poll(tg, core))

        component, detail = events.heartbeats[0]
        assert component == "telegram"
        assert detail["allowed_users"] == 2
        assert detail["last_update_id"] == 0
        assert detail["pending"] == 0


# --- ops shim -----------------------------------------------------------------


class TestOpsShim:
    def test_heartbeat_and_events_never_raise_without_ops(self):
        # brain.ops is not installed in this repo: both calls must be no-ops.
        heartbeat("telegram", {"allowed_users": 1})
        record_event("telegram", "capture", True, detail={"chars": 3})

    def test_status_lines_none_without_ops(self):
        try:
            from brain import ops  # noqa: F401
        except ImportError:
            assert _ops_status_lines() is None
        else:
            pytest.skip("brain.ops installed in this environment")


# --- CLI ----------------------------------------------------------------------


class TestCli:
    def test_whoami_hint_prints_and_exits_zero(self, capsys):
        from brain.cli import main

        assert main(["telegram", "--whoami-hint"]) == 0
        assert "rejected update from user id" in capsys.readouterr().out

    def test_telegram_without_token_exits_nonzero(self, capsys):
        from brain import telegram_bot

        # Explicit token=None (over env/.env): run() must refuse before any network.
        settings = telegram_bot.Settings(TELEGRAM_BOT_TOKEN=None, _env_file=None)
        assert telegram_bot.run(settings) == 2
        assert "TELEGRAM_BOT_TOKEN is not set" in capsys.readouterr().err
