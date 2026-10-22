"""Telegram bot tests: allowlist, commands, capture, events, poll semantics.

The update-processing core is PTB-agnostic and runs without network; the
poll-loop tests use a duck-typed fake Bot plus python-telegram-bot's real
exception types (skipped when the optional `telegram` extra is missing).
"""

import asyncio
import importlib
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

    def remove(self, item_id) -> None:
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
    """A TelegramBot wired to fakes; returns (core, repo).

    ``inbox`` items are seeded into the repo (real ItemsService) so v2
    keyboard actions (File/Type/Delete) operate on real service methods.
    """
    from brain.services.items import ItemsService

    repo = repo or FakeRepository()
    for item in inbox or []:
        repo.add(item)
    indexer = _StubIndexer()
    search = _StubSearch(search_hits)
    ask_service = ask or AskService(search, FakeLLMClient(reply="42"))
    items_service = ItemsService(repo, indexer)

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

    async def send_message(self, chat_id=None, text=None, **kwargs):
        self.sent.append({"chat_id": chat_id, "text": text, **kwargs})


def _run_poll(tg, core, *, expect_runtime_error: bool = True, **kwargs):
    """Run poll_loop to completion; swallow the tests' RuntimeError("stop")."""
    from brain.telegram_bot import poll_loop

    try:
        asyncio.run(poll_loop(tg, core, poll_timeout_s=1, **kwargs))
    except RuntimeError as exc:
        if not (expect_runtime_error and str(exc) == "stop"):
            raise


# Aliases used by tests/test_telegram_v2.py (same fixtures, explicit names).
make_core = _core


def make_core_with_repo(*, repo=None, ask=None) -> tuple:
    return _core(repo=repo, ask=ask)


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

        # compact card header — the stub classifier files plain notes as tasks,
        # and the header shows the classified type for note-source items
        assert reply.text.startswith("✅ <b>Saved</b> · ✔️ Task")
        assert len(repo.items) == 1
        item = next(iter(repo.items.values()))
        assert item.meta["channel"] == "telegram"
        assert item.source == "note"
        assert item.occurred_at is None

    def test_capture_single_message_whatsapp_saves_directly(self, monkeypatch):
        _Events().install(monkeypatch)
        core, repo = _core()

        reply = core.route("[21/10/2026, 09:00:00] Dana: one", 111)

        assert "✅ <b>Saved</b> · 💬 WhatsApp" in reply.text
        assert len(repo.items) == 1

    def test_capture_timezone_makes_occurred_at_aware(self, monkeypatch):
        _Events().install(monkeypatch)
        core, repo = _core()

        core.route("[21/10/2026, 09:00:00] Dana: hi", 111)

        item = next(iter(repo.items.values()))
        assert item.occurred_at == datetime(2026, 10, 21, 6, 0, tzinfo=UTC)

    def test_capture_multi_message_whatsapp_asks_before_saving(self, monkeypatch):
        core, repo = _core()

        reply = core.route(
            "[21/10/2026, 09:00:00] Dana: one\n[21/10/2026, 09:01:00] Rami: two", 111
        )

        assert repo.items == {}  # choice first
        token = reply.keyboard[0][0][1].split(":", 1)[1]
        core.handle_callback(f"w1:{token}")
        assert len(repo.items) == 1

    def test_search_returns_hits(self, monkeypatch):
        _Events().install(monkeypatch)
        item = MemoryItem(id=uuid.uuid4(), raw_text="Found the meeting notes", source="note")
        core, _ = _core(search_hits=[SearchHit(item=item, score=0.9)])

        reply = core.route("/search meeting", 111)

        assert "1 result(s) for 'meeting'" in reply.text
        assert "Found the meeting notes" in reply.text

    def test_search_empty_query_gets_usage(self, monkeypatch):
        events = _Events()
        events.install(monkeypatch)
        core, _ = _core()

        assert core.route("/search", 111).text.startswith("Usage:")
        assert events.events[-1] == ("search", False, {"error": "empty query"})

    def test_ask_answers(self, monkeypatch):
        _Events().install(monkeypatch)
        core, _ = _core()

        assert "42" in core.route("/ask meaning of life?", 111).text

    def test_ask_llm_error_is_friendly(self, monkeypatch):
        events = _Events()
        events.install(monkeypatch)
        core, _ = _core(ask=_ExplodingAsk())

        reply = core.route("/ask what?", 111)

        assert reply.text.startswith("⚠️ LLM unavailable")
        assert events.events[-1][0] == "ask" and events.events[-1][1] is False

    def test_review_lists_inbox(self, monkeypatch):
        _Events().install(monkeypatch)
        item = MemoryItem(
            id=uuid.uuid4(), raw_text="inbox candidate", source="note", status=ItemStatus.INBOX
        )
        core, _ = _core(inbox=[item])

        reply = core.route("/review", 111)

        assert "Inbox" in reply.text and "inbox candidate" in reply.text

    def test_status_without_cockpit(self, monkeypatch):
        _Events().install(monkeypatch)
        # feature/cockpit ships brain.ops; simulate the pre-cockpit install by
        # making the shim's optional import fail.
        real_import = importlib.import_module

        def no_ops(name: str, *args: object, **kwargs: object) -> object:
            if name == "brain.ops":
                raise ImportError(f"simulated: {name}")
            return real_import(name, *args, **kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr(importlib, "import_module", no_ops)
        core, _ = _core()

        assert core.route("/status", 111).text == "cockpit not installed yet"

    def test_status_with_cockpit_installed(self, monkeypatch):
        """Zero-change pickup: with brain.ops present, /status shows components."""
        _Events().install(monkeypatch)
        from brain.ops import health as ops_health

        monkeypatch.setattr(ops_health, "run_health_checks", lambda: {
            "components": {
                "postgres": {"status": "ok", "summary": "3 items"},
                "qdrant": {"status": "degraded", "summary": "vectors 6/6"},
                "telegram": {"status": "disabled", "summary": "no token"},
            }
        })
        core, _ = _core()
        reply = core.route("/status", 111)
        assert "🟠 brain: degraded" in reply.text
        assert "🟢 postgres: 3 items" in reply.text
        assert "🟡 qdrant" in reply.text and "⚪ telegram" in reply.text

    def test_status_down_when_a_component_is_down(self, monkeypatch):
        _Events().install(monkeypatch)
        from brain.ops import health as ops_health

        monkeypatch.setattr(ops_health, "run_health_checks", lambda: {
            "components": {"qdrant": {"status": "down", "summary": "unreachable"}}
        })
        core, _ = _core()
        assert "🔴 brain: down" in core.route("/status", 111).text

    def test_start_and_help(self, monkeypatch):
        _Events().install(monkeypatch)
        core, _ = _core()

        assert "Second Brain bot ready" in core.route("/start", 111).text
        assert "/search" in core.route("/help", 111).text


# --- poll loop ---------------------------------------------------------------


class TestPollLoop:
    def test_processes_allowed_update_and_replies(self, monkeypatch):
        events = _Events()
        events.install(monkeypatch)
        core, repo = _core()
        tg = _FakeTG([[_fake_update(text="hello note")], [RuntimeError("stop")]])

        _run_poll(tg, core)

        assert len(tg.sent) == 1 and tg.sent[0]["chat_id"] == 555
        assert "✔️ Task" in tg.sent[0]["text"]  # card header (classified type)
        assert len(repo.items) == 1
        assert events.heartbeats and events.heartbeats[0][1]["pending"] == 1
        assert events.events[0][0] == "capture"

    def test_conflict_records_and_stops_after_the_window(self, monkeypatch):
        """A conflict that persists past the retry window stops the bot (78
        upstream); each retry records an ops conflict event."""
        telegram = pytest.importorskip("telegram")
        events = _Events()
        events.install(monkeypatch)
        core, _ = _core()
        tg = _FakeTG(
            [[telegram.error.Conflict("terminated by other getUpdates request")]] * 12
        )  # persistent conflict: enough batches to outlast the 0.05s window

        with pytest.raises(telegram.error.Conflict):
            _run_poll(
                tg, core, expect_runtime_error=False,
                conflict_window_s=0.05, conflict_backoff_s=0.01,
            )

        conflicts = [e for e in events.events if e[0] == "conflict"]
        assert len(conflicts) >= 2  # retried within the window, not a first-poll exit
        assert conflicts[0][1] is False
        assert tg.shutdowns == 1

    def test_rejected_user_gets_no_reply_and_is_logged(
        self, monkeypatch, caplog: pytest.LogCaptureFixture
    ):
        _Events().install(monkeypatch)
        core, repo = _core()
        tg = _FakeTG([[_fake_update(user_id=999, text="sneak")], [RuntimeError("stop")]])

        with caplog.at_level(logging.INFO, logger="brain.telegram_bot"):
            _run_poll(tg, core)

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

        _run_poll(tg, core)

        assert "not supported yet" in tg.sent[0]["text"].lower()  # non-.ics in v2
        assert repo.items == {}

    def test_callback_query_is_acknowledged(self, monkeypatch):
        pytest.importorskip("telegram")
        core, _ = _core()
        acked: list[bool] = []

        async def _answer() -> None:
            acked.append(True)

        callback = SimpleNamespace(answer=_answer)
        tg = _FakeTG([[_fake_update(callback=callback)], [RuntimeError("stop")]])

        _run_poll(tg, core)

        assert acked == [True]

    def test_heartbeat_reports_allowed_users_and_pending(self, monkeypatch):
        events = _Events()
        events.install(monkeypatch)
        core, _ = _core()
        tg = _FakeTG([[], [RuntimeError("stop")]])

        _run_poll(tg, core)

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

    def test_telegram_without_token_exits_zero_disabled(self, caplog):
        import logging

        from brain import telegram_bot

        # Explicit token=None (over env/.env): the service reports "disabled".
        settings = telegram_bot.Settings(TELEGRAM_BOT_TOKEN=None, _env_file=None)
        with caplog.at_level(logging.ERROR, logger="brain.telegram_bot"):
            assert telegram_bot.run(settings) == 0
        assert "telegram bot disabled (no token)" in caplog.text

    def test_run_configures_stderr_logging(self, capsys):
        """run() installs a stderr root handler BEFORE logging anything, so a
        launchd job (no logging config of its own) still captures the startup
        line and errors in StandardErrorPath."""
        import logging
        import sys

        from brain import telegram_bot

        root = logging.getLogger()
        saved_handlers, saved_level = root.handlers[:], root.level
        root.handlers = []  # simulate a bare launchd process
        try:
            settings = telegram_bot.Settings(TELEGRAM_BOT_TOKEN=None, _env_file=None)
            assert telegram_bot.run(settings) == 0
            stderr_handlers = [
                h
                for h in root.handlers
                if isinstance(h, logging.StreamHandler) and h.stream is sys.stderr
            ]
            assert stderr_handlers, "run() left the root logger without a stderr handler"
        finally:
            root.handlers[:] = saved_handlers
            root.setLevel(saved_level)


class _ScriptedPollTG:
    """get_updates yields scripted items: an exception is raised, a list is
    returned; an empty script raises RuntimeError("stop") like the other
    poll-loop fakes."""

    def __init__(self, script):
        self._script = list(script)
        self.polls = 0
        self.initialized = False
        self.shutdown_called = False

    async def initialize(self):
        self.initialized = True

    async def shutdown(self):
        self.shutdown_called = True

    async def set_my_commands(self, commands):
        return None

    async def get_updates(self, offset=None, timeout=0):
        self.polls += 1
        item = self._script.pop(0) if self._script else RuntimeError("stop")
        if isinstance(item, BaseException):
            raise item
        return item


class _AlwaysConflictTG(_ScriptedPollTG):
    """Every get_updates conflicts forever (the give-up path)."""

    async def get_updates(self, offset=None, timeout=0):
        from telegram.error import Conflict

        self.polls += 1
        raise Conflict("forever")


class TestConflictBackoff:
    """409 is usually transient (a restart's old long-poll lingers for a few
    seconds): retry with backoff inside the window, exit 78 only when the
    conflict persists past it."""

    @staticmethod
    def _core():
        return SimpleNamespace(allowed_count=0)

    def test_transient_conflict_keeps_polling(self, monkeypatch):
        from telegram.error import Conflict

        from brain import telegram_bot

        events: list[tuple] = []
        monkeypatch.setattr(
            telegram_bot, "record_event", lambda *a, **k: events.append((a, k))
        )
        tg = _ScriptedPollTG([Conflict("c1"), Conflict("c2"), []])

        _run_poll(tg, self._core(), conflict_window_s=5.0, conflict_backoff_s=0.01)

        assert tg.polls >= 4  # 2 conflicts retried + 1 success + 1 stop
        assert tg.shutdown_called  # clean finally, still polling
        conflicts = [(a, k) for a, k in events if len(a) > 1 and a[1] == "conflict"]
        assert len(conflicts) == 2
        assert all(k.get("ok") is False for _, k in conflicts)

    def test_permanent_conflict_exits_78_after_the_window(self):
        from telegram.error import Conflict

        from brain.telegram_bot import poll_loop

        tg = _AlwaysConflictTG([])
        with pytest.raises(Conflict):
            asyncio.run(
                poll_loop(
                    tg,
                    SimpleNamespace(allowed_count=0),
                    poll_timeout_s=0.01,
                    conflict_window_s=0.06,
                    conflict_backoff_s=0.01,
                )
            )
        assert tg.polls >= 2  # it retried before giving up (not a first-poll exit)

    def test_success_resets_the_conflict_window(self, monkeypatch):
        from telegram.error import Conflict

        from brain import telegram_bot

        monkeypatch.setattr(telegram_bot, "record_event", lambda *a, **k: None)
        # conflict, success, conflict, success ...: each success must reset the
        # window, so alternating conflicts NEVER exhaust it.
        tg = _ScriptedPollTG([Conflict("c"), [], Conflict("c"), [], Conflict("c"), []])

        _run_poll(tg, self._core(), conflict_window_s=0.1, conflict_backoff_s=0.01)

        assert tg.polls == 7  # 3 conflicts + 3 successes + stop


class TestSingleInstanceLock:
    """One getUpdates poller per bot, enforced in-process: exclusive flock on
    a machine-wide per-bot lock file; the second instance exits 78 (no 409
    restart loop)."""

    def test_second_acquire_reports_the_first_pid(self, monkeypatch, tmp_path):
        import os

        from brain import telegram_bot

        monkeypatch.setenv("SECOND_BRAIN_CACHE_DIR", str(tmp_path / "cache"))
        first, other = telegram_bot._acquire_instance_lock("123:secret")
        assert first is not None and other is None
        try:
            second, holder = telegram_bot._acquire_instance_lock("123:secret")
            assert second is None, "second flock must lose"
            assert holder == os.getpid()
        finally:
            first.close()  # releases the lock (and lets other tests acquire)

    def test_run_exits_78_when_another_instance_holds_the_lock(self, caplog, monkeypatch, tmp_path):
        import logging
        import os

        from brain import telegram_bot

        monkeypatch.setenv("SECOND_BRAIN_CACHE_DIR", str(tmp_path / "cache"))
        lock, _ = telegram_bot._acquire_instance_lock("123:placeholder")
        try:
            def _fail_build(_settings):
                raise AssertionError("build_bot must not run in a second instance")

            monkeypatch.setattr(telegram_bot, "build_bot", _fail_build)
            settings = telegram_bot.Settings(
                TELEGRAM_BOT_TOKEN="123:placeholder",
                TELEGRAM_ALLOWED_USER_IDS="1",
                _env_file=None,
            )
            with caplog.at_level(logging.ERROR, logger="brain.telegram_bot"):
                assert telegram_bot.run(settings) == telegram_bot.EXIT_CONFIG
            assert "another bot instance is running" in caplog.text
            assert f"pid {os.getpid()}" in caplog.text
        finally:
            lock.close()


class TestLockScope:
    """The single-instance lock must be MACHINE-WIDE and per bot id — a bot
    started from any other clone/worktree with the same token must be blocked
    too (that is exactly the unexplained-second-poller class)."""

    def test_same_bot_id_from_another_repo_is_blocked(self, monkeypatch, tmp_path):
        import os

        from brain import telegram_bot

        monkeypatch.setenv("SECOND_BRAIN_CACHE_DIR", str(tmp_path / "cache"))
        monkeypatch.setattr(telegram_bot, "_repo_lock_dir", lambda: tmp_path / "repoA")
        first, other = telegram_bot._acquire_instance_lock("123:SECRET")
        assert first is not None and other is None
        try:
            # A different checkout, same bot id: must hit the SAME lock file.
            monkeypatch.setattr(telegram_bot, "_repo_lock_dir", lambda: tmp_path / "repoB")
            second, holder = telegram_bot._acquire_instance_lock("123:SECRET")
            assert second is None, "lock must be machine-wide, not per checkout"
            assert holder == os.getpid()
            # The secret half of the token never appears in the lock name.
            assert (tmp_path / "cache" / "telegram-123.lock").exists()
            assert not list((tmp_path / "cache").glob("*SECRET*"))
        finally:
            first.close()

    def test_different_bot_ids_do_not_block_each_other(self, monkeypatch, tmp_path):
        from brain import telegram_bot

        monkeypatch.setenv("SECOND_BRAIN_CACHE_DIR", str(tmp_path / "cache"))
        monkeypatch.setattr(telegram_bot, "_repo_lock_dir", lambda: tmp_path / "repoA")
        first, _ = telegram_bot._acquire_instance_lock("123:SECRET")
        try:
            second, _ = telegram_bot._acquire_instance_lock("456:OTHER")
            assert second is not None, "a different bot id has its own lock"
        finally:
            first.close()

    def test_default_cache_dir_under_home_when_env_unset(self, monkeypatch, tmp_path):
        """With SECOND_BRAIN_CACHE_DIR UNSET the lock dir must be
        $HOME/Library/Caches/second-brain — NOT Path('') == '.' (the CWD).
        Regression guard: Path(os.environ.get(X) or '') is truthy, which made
        the production default silently the current directory."""
        from brain import telegram_bot

        monkeypatch.delenv("SECOND_BRAIN_CACHE_DIR", raising=False)
        monkeypatch.setenv("HOME", str(tmp_path / "home"))
        monkeypatch.setattr(telegram_bot, "_repo_lock_dir", lambda: tmp_path / "repoA" / ".dev")
        handle, other = telegram_bot._acquire_instance_lock("123:SECRET")
        try:
            assert handle is not None and other is None
            expected = (
                tmp_path / "home" / "Library" / "Caches" / "second-brain" / "telegram-123.lock"
            )
            assert expected.exists(), "lock must default to HOME/Library/Caches, not the CWD"
        finally:
            handle.close()

    def test_falls_back_to_repo_dev_when_cache_dir_unwritable(self, monkeypatch, tmp_path):
        from brain import telegram_bot

        blocker = tmp_path / "blocker"
        blocker.write_text("not a dir")  # cache mkdir under this must fail
        monkeypatch.setenv("SECOND_BRAIN_CACHE_DIR", str(blocker / "cache"))
        monkeypatch.setattr(telegram_bot, "_repo_lock_dir", lambda: tmp_path / "repoA" / ".dev")
        handle, other = telegram_bot._acquire_instance_lock("123:SECRET")
        try:
            assert handle is not None and other is None
            assert (tmp_path / "repoA" / ".dev" / "telegram.lock").exists()
        finally:
            handle.close()
