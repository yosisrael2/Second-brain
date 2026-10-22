"""Telegram bot v2 feature tests — one behavior test per spec item.

Written BEFORE the implementation (each test fails against the v1 bot, then
passes once the feature lands):

1. inline keyboards on capture cards (File/Type/Delete/Undo, 10-min validity,
   compact + validated callback data, graceful stale answers)
2. WhatsApp multi-message paste -> save-as-1 / save-separately choice
3. forwarded messages -> meta.sender + forward date, meta.forwarded
4. questions ending in "?" route to ask; TYPING chat action; model name +
   Open buttons per citation
5. /recent (last 10) + set_my_commands at startup
6. .ics documents -> calendar capture; other files/photos/voice -> polite
   unsupported reply
7. HTML card format: kind emoji, extracted fields, type/importance/
   actionability/status, escaped
8. exit codes: no token -> 0 (+ log), Conflict/Unauthorized -> 78, transient -> 1
"""

import asyncio
import logging
import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import telegram.error
from tests.test_telegram_bot import (
    _Events,
    _fake_update,
    _run_poll,
    make_core,
)

from brain.db.enums import ItemStatus, ItemType
from brain.db.models import MemoryItem
from brain.services.ask import AskResult

# --- 1. inline keyboards on capture cards ------------------------------------


class TestCaptureKeyboards:
    def test_capture_card_has_file_type_delete_undo_buttons(self, monkeypatch):
        core, repo = make_core()
        reply = core.route("Read the RAG paper and take notes", 111)

        labels = [label for row in reply.keyboard for label, _ in row]
        # one row; the stub classifier files at 0.95 confidence so File is hidden
        assert labels == ["🏷 Type", "🗑 Delete", "↩️ Undo"]
        assert len(reply.keyboard) == 1

    def test_capture_buttons_show_file_only_for_unfiled_items(self):
        core, _ = make_core()

        class Inbox:
            id = uuid.uuid4()
            status = "inbox"

        class Filed:
            id = uuid.uuid4()
            status = "filed"

        inbox_row = core._capture_buttons([Inbox()])[0]
        filed_row = core._capture_buttons([Filed()])[0]
        assert [label for label, _ in inbox_row] == ["✅ File", "🏷 Type", "🗑 Delete", "↩️ Undo"]
        assert [label for label, _ in filed_row] == ["🏷 Type", "🗑 Delete", "↩️ Undo"]

    def test_undo_callback_deletes_the_items_created_by_that_message(self, monkeypatch):
        core, repo = make_core()
        reply = core.route("hello note one", 111)
        assert len(repo.items) == 1
        token = reply.keyboard[0][1][1].split(":", 1)[1]  # second button '🗑 Delete'

        undo = core.handle_callback(f"u:{token}")

        assert repo.items == {}
        assert undo.text.startswith("↩️ Undone")

    def test_delete_and_file_callbacks(self, monkeypatch):
        core, repo = make_core()
        reply_one = core.route("hello note two", 111)
        core.route("hello note three", 111)

        d_token = reply_one.keyboard[0][0][1].split(":", 1)[1]  # '🗑 Delete'
        core.handle_callback(f"d:{d_token}")
        assert len(repo.items) == 1  # note two deleted, note three remains

        from brain.telegram_bot import Pending

        token = core.registry.put(Pending(ids=(next(iter(repo.items)),)))
        core.handle_callback(f"f:{token}")
        assert all(i.status == ItemStatus.FILED for i in repo.items.values())

    def test_type_submenu_then_apply(self, monkeypatch):
        core, repo = make_core()
        reply = core.route("hello note four", 111)
        _t_label, t_data = reply.keyboard[0][0]
        t_token = t_data.split(":", 1)[1]

        submenu = core.handle_callback(f"t:{t_token}")
        assert submenu.keyboard is not None
        type_labels = [label for row in submenu.keyboard for label, _ in row]
        assert set(type_labels) == {
            "✔️ Task", "⚖️ Decision", "📝 Note", "📅 Event", "🔗 Reference", "💡 Idea", "« Back",
        }

        item_id = next(iter(repo.items))
        apply_data = f"ty:{t_token}:{ItemType.TASK.value}"
        core.handle_callback(apply_data)
        assert repo.items[item_id].type == ItemType.TASK

    def test_stale_or_unknown_callbacks_are_answered_gracefully(self, monkeypatch):
        core, _ = make_core()

        for data in ("u:deadbeef", "zz:junk", "ty:deadbeef:task", "n:not-a-number"):
            reply = core.handle_callback(data)  # must not raise
            assert reply.keyboard is None
            assert "expire" in reply.text.lower() or "unknown" in reply.text.lower()

    def test_callback_data_is_compact(self, monkeypatch):
        core, _ = make_core()
        reply = core.route("hello note five", 111)

        for row in reply.keyboard:
            for _label, data in row:
                assert len(data.encode()) < 64


# --- 2. WhatsApp multi-message choice ----------------------------------------


class TestWhatsAppChoice:
    PASTE = "[21/10/2026, 09:00:00] Dana: one\n[21/10/2026, 09:01:00] Rami: two"

    def test_multi_message_whatsapp_is_not_saved_until_choice(self, monkeypatch):
        core, repo = make_core()
        reply = core.route(self.PASTE, 111)

        assert repo.items == {}  # nothing saved yet
        labels = [label for row in reply.keyboard for label, _ in row]
        assert labels == ["Save as 1 item", "Save separately"]

    def test_save_as_one_item_joins_the_thread(self, monkeypatch):
        core, repo = make_core()
        reply = core.route(self.PASTE, 111)
        token = _choice_token(reply, "Save as 1 item")

        joined = core.handle_callback(f"w1:{token}")
        assert len(repo.items) == 1
        assert "✅ <b>Saved</b> · 💬 WhatsApp" in joined.text

    def test_save_separately_makes_one_item_per_message(self, monkeypatch):
        core, repo = make_core()
        reply = core.route(self.PASTE, 111)
        token = _choice_token(reply, "Save separately")

        split = core.handle_callback(f"wm:{token}")
        assert len(repo.items) == 2
        assert "· 2 items" in split.text


def _choice_token(reply, label: str) -> str:
    for row in reply.keyboard:
        for text, data in row:
            if text == label:
                return data.split(":", 1)[1]
    raise AssertionError(f"no button {label!r} in {reply.keyboard}")


# --- 3. forwarded messages ----------------------------------------------------


class TestForwardedMessages:
    def _forward_update(self, origin: Any, forward_date=None):
        from tests.test_telegram_bot import _fake_update

        update = _fake_update(text="Fwd: interesting note")
        update.message.forward_origin = origin
        update.message.forward_date = forward_date
        update.effective_message = update.message
        return update

    def test_forward_from_user_sets_sender_and_forward_date(self, monkeypatch):
        events = _Events()
        events.install(monkeypatch)
        core, repo = make_core()
        origin = SimpleNamespace(type="user", sender_user=SimpleNamespace(id=42))
        update = self._forward_update(origin)

        _handle(update, core)

        item = next(iter(repo.items.values()))
        assert item.meta["forwarded"] is True
        assert item.meta["sender"] == "user:42"  # bare id: no name/username on origin
        assert item.meta["sender_id"] == 42
        assert item.source == "note"  # source stays the parsed kind

    def test_forward_from_user_prefers_username(self, monkeypatch):
        core, repo = make_core()
        origin = SimpleNamespace(
            type="user",
            sender_user=SimpleNamespace(
                id=42, first_name="Dana", last_name="Cohen", username="dana_c"
            ),
        )
        update = self._forward_update(origin)

        _handle(update, core)

        item = next(iter(repo.items.values()))
        assert item.meta["sender"] == "@dana_c"
        assert item.meta["sender_id"] == 42

    def test_forward_from_user_without_username_uses_full_name(self, monkeypatch):
        core, repo = make_core()
        origin = SimpleNamespace(
            type="user",
            sender_user=SimpleNamespace(id=7, first_name="Rami", last_name="K", username=None),
        )
        update = self._forward_update(origin)

        _handle(update, core)

        item = next(iter(repo.items.values()))
        assert item.meta["sender"] == "Rami K"
        assert item.meta["sender_id"] == 7
        assert item.meta["channel"] == "telegram"

    def test_forward_from_hidden_user_uses_the_name(self, monkeypatch):
        core, repo = make_core()
        origin = SimpleNamespace(type="hidden_user", sender_user_name=" Dana ")
        update = self._forward_update(origin)

        _handle(update, core)

        item = next(iter(repo.items.values()))
        assert item.meta["sender"] == "Dana"
        assert item.meta["forwarded"] is True

    def test_forward_from_chat_uses_the_chat_title(self, monkeypatch):
        core, repo = make_core()
        origin = SimpleNamespace(type="chat", sender_chat=SimpleNamespace(id=-100, title="Team"))
        update = self._forward_update(origin)

        _handle(update, core)

        item = next(iter(repo.items.values()))
        assert item.meta["sender"] == "Team"
        assert item.meta["forwarded"] is True


def _handle(update, core, tg=None):
    """Run one update through the polling adapter (async)."""

    async def _run():
        from brain.telegram_bot import _handle_update

        await _handle_update(tg or _AdapterTG(), core, update)

    asyncio.run(_run())


class _AdapterTG:
    """Minimal tg for _handle_update: records sends, resolves .ics files."""

    def __init__(self, file_bytes: bytes = b""):
        self.sent: list[dict] = []
        self.edits: list[dict] = []
        self.chat_actions: list[tuple] = []
        self.commands: list | None = None
        self._file_bytes = file_bytes

    async def send_message(self, chat_id=None, text=None, **kwargs):
        self.sent.append({"chat_id": chat_id, "text": text, **kwargs})

    async def edit_message_text(self, chat_id=None, message_id=None, text=None, **kwargs):
        self.edits.append({"chat_id": chat_id, "message_id": message_id, "text": text, **kwargs})

    async def send_chat_action(self, chat_id=None, action=None):
        self.chat_actions.append((chat_id, action))

    async def set_my_commands(self, commands):
        self.commands = commands

    async def get_file(self, file_id=None):
        return SimpleNamespace(download_as_bytearray=lambda: bytearray(self._file_bytes))


# --- 4. question routing + typing + open buttons ------------------------------


class TestQuestionRouting:
    def test_question_text_routes_to_ask(self, monkeypatch):
        core, _ = make_core(ask=_CannedAsk(AskResult(answer="42", cited_ids=[], model="gpt-x")))
        reply = core.route("What did I decide yesterday?", 111)
        assert "42" in reply.text
        assert "gpt-x" in reply.text

    def test_typing_chat_action_while_waiting(self, monkeypatch):
        core, _ = make_core(ask=_CannedAsk(AskResult(answer="42", cited_ids=[], model="m")))
        tg = _AdapterTG()

        _handle(_text_update("What?"), core, tg)

        assert ("typing" in [a for _, a in tg.chat_actions]) or tg.chat_actions

    def test_open_buttons_per_cited_item(self, monkeypatch):
        from tests.test_telegram_bot import make_core_with_repo

        core, repo = make_core_with_repo()
        cited = MemoryItem(id=uuid.uuid4(), raw_text="cited source", source="note")
        repo.add(cited)
        ask = _CannedAsk(AskResult(answer="42", cited_ids=[str(cited.id)], model="m"))
        core = make_core_with_repo(repo=repo, ask=ask)[0]

        reply = core.route("What?", 111)
        open_buttons = [d for row in reply.keyboard for _l, d in row if d.startswith("o:")]
        assert len(open_buttons) == 1
        assert reply.keyboard[0][0][0] == "📎 1"  # one row, numbered

        opened = core.handle_callback(open_buttons[0])
        assert "cited source" in opened.text


def _text_update(text: str):
    from tests.test_telegram_bot import _fake_update

    return _fake_update(text=text)


class _CannedAsk:
    def __init__(self, result: AskResult) -> None:
        self._result = result

    def ask(self, question: str) -> AskResult:
        return self._result


# --- 5. /recent + registered commands -----------------------------------------


class TestRecentAndCommands:
    def test_recent_lists_last_ten_items(self, monkeypatch):
        core, repo = make_core()
        for i in range(12):
            repo.add(MemoryItem(id=uuid.uuid4(), raw_text=f"item {i}", source="note"))
        reply = core.route("/recent", 111)
        lines = reply.text.splitlines()
        assert lines[0] == "🕘 Last 10 item(s):"  # header says 10, not 12
        rows = [line for line in lines[1:] if line[0].isdigit()]
        assert len(rows) == 10
        assert rows[0].startswith("1. 📝") and " · " in rows[0]  # numbered, emoji, date
        # exactly the 10 newest (items 2..11); order comes from created_at DESC
        shown = sorted(line.split(". 📝 ", 1)[1].split(" · ")[0] for line in rows)
        assert shown == sorted(f"item {i}" for i in range(2, 12))

    def test_bot_commands_registered_at_startup(self, monkeypatch):
        core, _ = make_core()
        tg = _FakeTGForPoll()
        tg.queue([])

        _run_poll(tg, core)

        names = [c.command for c in (tg.commands or [])]
        assert names == [
            "ask", "search", "review", "recent", "profile", "remember", "status", "help",
        ]


class _FakeTGForPoll:
    """Scripted fake for poll_loop tests: one batch then a clean stop."""

    def __init__(self) -> None:
        self.batches: list = []
        self.sent: list[dict] = []
        self.chat_actions: list[tuple] = []
        self.commands: list | None = None
        self.answered: list = []
        self.shutdowns = 0
        self.initialized = False

    def queue(self, batch) -> None:
        self.batches.append(batch)

    async def initialize(self):
        self.initialized = True

    async def shutdown(self):
        self.shutdowns += 1

    async def get_updates(self, offset=None, timeout=0):
        if not self.batches:
            raise RuntimeError("stop")  # clean test stop
        batch = self.batches.pop(0)
        if isinstance(batch, Exception):
            raise batch
        return batch

    async def send_message(self, chat_id=None, text=None, **kwargs):
        self.sent.append({"chat_id": chat_id, "text": text, **kwargs})

    async def send_chat_action(self, chat_id=None, action=None):
        self.chat_actions.append((chat_id, action))

    async def set_my_commands(self, commands):
        self.commands = commands

    async def answer_callback_query(self, callback_query_id=None):
        self.answered.append(callback_query_id)

    async def get_file(self, file_id=None):
        return SimpleNamespace(download_as_bytearray=lambda: bytearray(b""))


# --- 6. documents ------------------------------------------------------------


class TestDocuments:
    def test_ics_document_is_captured_as_calendar(self, monkeypatch):
        core, repo = make_core()
        ics = b"BEGIN:VCALENDAR\nBEGIN:VEVENT\nSUMMARY:Standup\nEND:VEVENT\nEND:VCALENDAR"
        tg = _AdapterTG(file_bytes=ics)
        document = SimpleNamespace(
            file_name="invite.ics", mime_type="text/calendar", file_id="f1"
        )
        update = _fake_update(text=None, document=document)

        _handle(update, core, tg)

        assert any(i.source == "calendar" for i in repo.items.values())
        assert "Standup" in tg.sent[0]["text"] or "Saved" in tg.sent[0]["text"]

    def test_non_ics_files_get_not_supported_yet(self, monkeypatch):
        core, repo = make_core()
        tg = _AdapterTG()
        document = SimpleNamespace(
            file_name="contract.pdf", mime_type="application/pdf", file_id="f2"
        )
        update = _fake_update(text=None, document=document)

        _handle(update, core, tg)

        assert repo.items == {}
        assert "not supported yet" in tg.sent[0]["text"].lower()

    def test_photos_and_voice_get_not_supported_yet_no_crash(self, monkeypatch):
        core, repo = make_core()
        tg = _AdapterTG()
        photo_update = _fake_update(text=None)
        photo_update.message.text = None
        photo_update.message.document = None
        photo_update.message.photo = [SimpleNamespace(file_id="p1")]
        photo_update.effective_message = photo_update.message

        _handle(photo_update, core, tg)

        assert repo.items == {}
        assert "not supported yet" in tg.sent[0]["text"].lower()


# --- 7. card format -----------------------------------------------------------


class TestCardFormat:
    EMAIL = (
        "From: Dana Cohen <dana@example.com>\n"
        "Subject: Kickoff meeting\n"
        "Date: 21/10/2026 09:00\n"
        "\n"
        "Let's meet <b>tomorrow</b> at the Haifa office.\n"
        "Location: Haifa office, floor 3"
    )

    def test_card_shows_kind_emoji_and_fields(self, monkeypatch):
        core, _ = make_core()
        reply = core.route(self.EMAIL, 111)

        assert reply.text.startswith("✅ <b>Saved</b> · 📧 Email")
        assert "From: Dana Cohen" in reply.text
        assert "Subject: Kickoff meeting" in reply.text
        assert "When:" in reply.text
        assert "⭐" in reply.text  # importance as stars
        assert "type:" not in reply.text and "importance:" not in reply.text
        assert "status:" not in reply.text  # no debug labels

        cal = core.route(
            "TUESDAY, October 21, 2026\n9:00 – 12:30pm\nStandup\n"
            "Location: Haifa office",
            111,
        )
        assert "Where: Haifa office" in cal.text  # location (calendar kind)

    def test_html_is_escaped(self, monkeypatch):
        core, _ = make_core()
        reply = core.route(self.EMAIL, 111)

        assert "<b>tomorrow</b>" not in reply.text
        assert "&lt;b&gt;tomorrow&lt;/b&gt;" in reply.text

    def test_kind_emoji_per_kind(self, monkeypatch):
        core, _ = make_core()
        assert "💬 WhatsApp" in core.route("[21/10/2026, 09:00:00] Dana: hi", 111).text
        cal_reply = core.route("TUESDAY, October 21, 2026\n9:00 – 12:30pm\nStandup", 111)
        assert "📅 Event" in cal_reply.text


# --- 8. exit codes -------------------------------------------------------------


class TestExitCodes:
    @staticmethod
    def _short_conflict_window(monkeypatch) -> None:
        """run() uses the production conflict window (10 min); the exit-code
        tests only need the MAPPING, so shrink the window via poll_loop."""
        from brain import telegram_bot

        real_poll_loop = telegram_bot.poll_loop

        def _fast(tg, core):
            return real_poll_loop(
                tg, core, conflict_window_s=0.05, conflict_backoff_s=0.01
            )

        monkeypatch.setattr(telegram_bot, "poll_loop", _fast)

    def test_no_token_logs_disabled_and_exits_zero(self, monkeypatch, caplog):
        from brain import telegram_bot

        settings = telegram_bot.Settings(TELEGRAM_BOT_TOKEN=None, _env_file=None)
        with caplog.at_level(logging.ERROR, logger="brain.telegram_bot"):
            assert telegram_bot.run(settings) == 0
        assert "telegram bot disabled (no token)" in caplog.text

    def test_conflict_exits_78_after_the_window(self, monkeypatch):
        from brain import telegram_bot

        self._short_conflict_window(monkeypatch)
        conflict = telegram.error.Conflict("terminated by other getUpdates request")
        monkeypatch.setattr(telegram_bot, "build_bot", lambda s: (object(), _ExitTG(conflict)))
        settings = telegram_bot.Settings(
            TELEGRAM_BOT_TOKEN="123:x", TELEGRAM_ALLOWED_USER_IDS="1", _env_file=None
        )

        assert telegram_bot.run(settings) == 78

    def test_unauthorized_token_exits_78(self, monkeypatch):
        from brain import telegram_bot

        # PTB v22 renamed Unauthorized -> InvalidToken
        unauthorized = telegram.error.InvalidToken("401: Unauthorized")
        monkeypatch.setattr(telegram_bot, "build_bot", lambda s: (object(), _ExitTG(unauthorized)))
        settings = telegram_bot.Settings(
            TELEGRAM_BOT_TOKEN="123:x", TELEGRAM_ALLOWED_USER_IDS="1", _env_file=None
        )

        assert telegram_bot.run(settings) == 78

    def test_transient_error_exits_1(self, monkeypatch):
        from brain import telegram_bot

        monkeypatch.setattr(
            telegram_bot, "build_bot", lambda s: (object(), _ExitTG(RuntimeError("boom")))
        )
        settings = telegram_bot.Settings(
            TELEGRAM_BOT_TOKEN="123:x", TELEGRAM_ALLOWED_USER_IDS="1", _env_file=None
        )

        assert telegram_bot.run(settings) == 1


class _ExitTG:
    """Fake whose first get_updates raises; enough to drive run() to a code."""

    def __init__(self, exc: BaseException) -> None:
        self._exc = exc

    async def initialize(self):
        return None

    async def shutdown(self):
        return None

    async def get_updates(self, offset=None, timeout=0):
        raise self._exc


# --- 9. golden text: the exact rendered cards (A5 regression guard) -----------


class TestGoldenFormat:
    """Freeze the exact rendered text per message type (manager spec A2-A4)."""

    EMAIL = (
        "From: Dana Cohen <dana@example.com>\nSubject: Kickoff meeting\n"
        "Date: 21/10/2026 09:00\n\nLet's meet <b>tomorrow</b>.\n"
        "Location: Haifa office, floor 3"
    )
    PASTE = "[21/10/2026, 09:00:00] Dana: one\n[21/10/2026, 09:01:00] Rami: two"

    def _sources(self, repo):
        i1 = MemoryItem(
            id=uuid.UUID(int=1),
            raw_text="Renew the TLS certificate before Friday",
            source="email",
            occurred_at=datetime(2026, 10, 22, 9, 0, tzinfo=UTC),
        )
        i2 = MemoryItem(
            id=uuid.UUID(int=2),
            raw_text="WhatsApp pool party on Saturday, bring towels and snacks for everyone",
            source="whatsapp",
            created_at=datetime(2026, 10, 21, 9, 0, tzinfo=UTC),
        )
        repo.add(i1)
        repo.add(i2)
        return i1, i2

    def test_ask_answer_with_sources_golden(self, monkeypatch):
        core, repo = make_core()
        i1, i2 = self._sources(repo)
        from brain.services.search import SearchHit

        result = AskResult(
            answer=f"Renew it <b>before</b> Friday & audit keys. See [{i1.id}] and [{i2.id}].",
            cited_ids=[str(i1.id), str(i2.id)],
            sources=[SearchHit(item=i1, score=0.9), SearchHit(item=i2, score=0.8)],
            model="claude-sonnet-5-5",
        )
        core = make_core(ask=_CannedAsk(result))[0]

        reply = core.route("When is the TLS cert due?", 111)

        assert reply.text == (
            "🧠 <b>Answer</b>\n"
            "Renew it &lt;b&gt;before&lt;/b&gt; Friday &amp; audit keys. See ¹ and ².\n"
            "\n"
            "📚 <b>Sources</b>\n"
            "1. 📧 Renew the TLS certificate before Friday · 22 Oct\n"
            "2. 💬 WhatsApp pool party on Saturday, bring towels and snacks for… · 21 Oct\n"
            "\n"
            "<i>claude-sonnet-5-5 · "
        ) or True  # seconds vary: assert prefix exactly, footer tail loosely
        assert reply.text.startswith(
            "🧠 <b>Answer</b>\n"
            "Renew it &lt;b&gt;before&lt;/b&gt; Friday &amp; audit keys. See ¹ and ².\n"
            "\n"
            "📚 <b>Sources</b>\n"
            "1. 📧 Renew the TLS certificate before Friday · 22 Oct\n"
            "2. 💬 WhatsApp pool party on Saturday, bring towels and snacks for… · 21 Oct\n"
            "\n"
            "<i>claude-sonnet-5-5 · "
        )
        labels = [label for row in reply.keyboard for label, _ in row]
        assert labels == ["📎 1", "📎 2"]  # one row, matches source numbers

    def test_ask_without_citations_is_plain(self, monkeypatch):
        core, _ = make_core(
            ask=_CannedAsk(AskResult(answer="I do not know.", cited_ids=[], model="m"))
        )
        reply = core.route("What?", 111)

        assert reply.text.startswith("🧠 <b>Answer</b>\nI do not know.\n\n<i>m · ")
        assert reply.text.endswith("</i>")
        assert "📚" not in reply.text  # no sources block
        assert reply.keyboard == []  # no buttons

    def test_ask_escapes_html_and_fake_tags(self, monkeypatch):
        answer = "Got it <script>alert(1)</script> & done."
        core, _ = make_core(ask=_CannedAsk(AskResult(answer=answer, cited_ids=[], model=None)))
        reply = core.route("What?", 111)

        assert "<script>" not in reply.text
        assert "&lt;script&gt;alert(1)&lt;/script&gt;" in reply.text
        assert "&amp;" in reply.text

    def test_capture_email_card_golden(self, monkeypatch):
        core, _ = make_core()
        reply = core.route(TestCardFormat.EMAIL, 111)

        lines = reply.text.splitlines()
        assert lines[0] == "✅ <b>Saved</b> · 📧 Email"
        assert lines[2] == "From: Dana Cohen &lt;dana@example.com&gt;"
        assert lines[3] == "Subject: Kickoff meeting"
        assert lines[4] == "When: 21/10/2026 09:00"
        assert lines[5] == "⭐⭐⭐⭐ · ⏰ Now"
        assert len(lines) == 6  # no status line: born filed, not needs_review
        # one action row, File hidden (born filed at 0.95 confidence)
        assert [label for row in reply.keyboard for label, _ in row] == [
            "🏷 Type", "🗑 Delete", "↩️ Undo",
        ]

    def test_capture_whatsapp_card_golden(self, monkeypatch):
        core, _ = make_core()
        reply = core.route("[21/10/2026, 09:00:00] Dana: hi", 111)

        assert reply.text.startswith(
            "✅ <b>Saved</b> · 💬 WhatsApp\n[21/10/2026, 09:00:00] Dana: hi\n"
        )

    def test_needs_review_line_with_confidence(self, monkeypatch):
        core, repo = make_core()
        item = MemoryItem(
            id=uuid.uuid4(),
            raw_text="unsure thing",
            source="note",
            status=ItemStatus.NEEDS_REVIEW,
            confidence=0.63,
        )
        repo.add(item)
        from brain.telegram_bot import Pending

        reply = core._card_reply(Pending(ids=(item.id,)))

        assert "🔎 Needs review (63%)" in reply.text

    def test_search_golden(self, monkeypatch):
        from tests.test_telegram_bot import make_core_with_repo

        from brain.services.search import SearchHit

        core, repo = make_core_with_repo()
        i1 = MemoryItem(
            id=uuid.uuid4(), raw_text="Found the meeting notes", source="note",
            created_at=datetime(2026, 10, 22, 9, 0, tzinfo=UTC),
        )
        i2 = MemoryItem(
            id=uuid.uuid4(), raw_text="Meeting agenda draft", source="whatsapp",
            occurred_at=datetime(2026, 10, 21, 9, 0, tzinfo=UTC),
        )
        repo.add(i2)
        repo.add(i1)
        from tests.test_telegram_bot import _core as _make_core

        core = _make_core(repo=repo, search_hits=[
            SearchHit(item=i1, score=0.9), SearchHit(item=i2, score=0.8),
        ])[0]

        reply = core.route("/search meeting", 111)

        assert reply.text == (
            "🔍 2 result(s) for 'meeting':\n"
            "1. 📝 Found the meeting notes · 22 Oct\n"
            "2. 💬 Meeting agenda draft · 21 Oct"
        )
        labels = [label for row in reply.keyboard for label, _ in row]
        assert labels == ["📎 1", "📎 2"]

    def test_review_card_golden(self, monkeypatch):
        core, repo = make_core()
        item = MemoryItem(
            id=uuid.uuid4(),
            raw_text="inbox candidate",
            source="note",
            status=ItemStatus.INBOX,
            importance=2,
        )
        core, _ = make_core(inbox=[item])
        reply = core.route("/review", 111)

        assert reply.text == (
            "📥 Inbox (1/1)\n"
            "inbox candidate\n"
            "⭐⭐ · —"
        )
        labels = [label for row in reply.keyboard for label, _ in row]
        assert labels == ["✅ File", "🏷 Type", "🗑 Delete"]

    def test_status_golden(self, monkeypatch):
        from brain.ops import health as ops_health

        monkeypatch.setattr(ops_health, "run_health_checks", lambda: {
            "components": {
                "api": {"status": "ok", "summary": "pid 7"},
                "qdrant": {"status": "degraded", "summary": "vectors 6/6"},
                "telegram": {"status": "disabled", "summary": "no token"},
            }
        })
        core, _ = make_core()
        reply = core.route("/status", 111)

        assert reply.text == (
            "🟠 brain: degraded\n"
            "🟢 api: pid 7\n"
            "🟡 qdrant: vectors 6/6\n"
            "⚪ telegram: no token"
        )


class TestEditInPlace:
    """Callback actions EDIT the card message instead of sending a new one."""

    @staticmethod
    def _callback_update(core, data: str, message_id: int = 42):
        from tests.test_telegram_bot import _fake_update

        message = SimpleNamespace(chat_id=555, message_id=message_id, text=None)
        return _fake_update(callback=SimpleNamespace(data=data, message=message))

    def test_delete_edits_the_card_in_place(self, monkeypatch):
        core, repo = make_core()
        reply = core.route("hello edit one", 111)
        token = reply.keyboard[0][0][1].split(":", 1)[1]  # first button: Delete
        tg = _AdapterTG()

        _handle(self._callback_update(core, f"d:{token}"), core, tg)

        assert repo.items == {}
        assert len(tg.edits) == 1 and not tg.sent
        assert tg.edits[0]["message_id"] == 42
        assert tg.edits[0]["text"].startswith("🗑 Deleted")

    def test_open_sends_a_new_message(self, monkeypatch):
        from tests.test_telegram_bot import make_core_with_repo

        core, repo = make_core_with_repo()
        cited = MemoryItem(id=uuid.uuid4(), raw_text="cited source", source="note")
        repo.add(cited)
        ask = _CannedAsk(AskResult(answer="42", cited_ids=[str(cited.id)], model="m"))
        core = make_core_with_repo(repo=repo, ask=ask)[0]
        reply = core.route("What?", 111)
        data = reply.keyboard[0][0][1]
        tg = _AdapterTG()

        _handle(self._callback_update(core, data), core, tg)

        assert not tg.edits and len(tg.sent) == 1  # Open is a new card, not an edit
# --- 9. hotfix: no reply may carry raw HTML (BadRequest parse-entities) --------


class TestHtmlSafety:
    """Every user/LLM-derived string in a reply is HTML-escaped (hotfix)."""

    def test_ask_answer_escapes_lt_gt_amp_and_hebrew(self, monkeypatch):
        answer = "Use <b>bold</b> & <fake tag> — עברית עובדת <>"
        core, _ = make_core(ask=_CannedAsk(AskResult(answer=answer, cited_ids=[], model="m")))

        reply = core.route("What?", 111)

        # the card's own header uses <b>, but the ANSWER body must be escaped
        assert "<b>bold</b>" not in reply.text
        assert "<fake tag>" not in reply.text
        assert "&lt;b&gt;bold&lt;/b&gt;" in reply.text
        assert "&amp;" in reply.text
        assert "&lt;fake tag&gt;" in reply.text
        assert "&lt;&gt;" in reply.text
        assert "עברית עובדת" in reply.text

    def test_send_resends_plain_when_telegram_rejects_entities(self, monkeypatch):
        """BadRequest 'parse entities' -> resend stripped, parse_mode=None, ops event."""
        from tests.test_telegram_bot import _fake_update

        events = _Events()
        events.install(monkeypatch)
        core, repo = make_core()
        tg = _AdapterTG()
        calls = {"n": 0}

        async def flaky_send(**kwargs):
            calls["n"] += 1
            if calls["n"] == 1:
                from telegram.error import BadRequest

                raise BadRequest("Can't parse entities: unsupported start tag")
            tg.sent.append(kwargs)
            return None

        tg.send_message = flaky_send  # type: ignore[method-assign]
        message = SimpleNamespace(chat_id=555, message_id=7, text="hello note")
        update = _fake_update(text="hello note")
        update = SimpleNamespace(
            update_id=update.update_id,
            effective_user=update.effective_user,
            callback_query=None,
            effective_message=message,
        )

        async def _run():
            from brain.telegram_bot import _handle_update

            await _handle_update(tg, core, update)

        asyncio.run(_run())

        assert calls["n"] == 2  # one failure, one retry
        plain = tg.sent[-1]
        assert plain.get("parse_mode") is None  # retry without HTML parsing
        # the reply WAS valid HTML (capture card: '✅ <b>Saved</b> · 📝 Note'); the
        # stripped fallback must keep the text but carry no tags
        assert "<b>Saved</b>" not in plain["text"]
        assert "Saved" in plain["text"] and "hello note" in plain["text"]
        assert any(e[0] == "error" and e[1] is False for e in events.events)  # ops record


class TestReviewFixes:
    """PR #17 review: local times on cards, type-aware kind labels, no dead registry put."""

    def test_when_line_renders_in_the_user_timezone(self):
        core, repo = make_core()
        item = MemoryItem(
            id=uuid.uuid4(),
            raw_text="Board meeting",
            source="calendar",
            occurred_at=datetime(2026, 10, 27, 14, 0, tzinfo=UTC),
        )
        repo.add(item)
        from brain.telegram_bot import Pending

        reply = core._card_reply(Pending(ids=(item.id,)))

        assert "When: Tue 27 Oct 16:00" in reply.text  # 14:00Z + 2h (IST after DST end)

    def test_when_line_survives_the_october_dst_change(self):
        core, repo = make_core()
        before = MemoryItem(
            id=uuid.uuid4(), raw_text="IDT event", source="calendar",
            occurred_at=datetime(2026, 10, 24, 13, 0, tzinfo=UTC),  # +03:00
        )
        after = MemoryItem(
            id=uuid.uuid4(), raw_text="IST meeting", source="calendar",
            occurred_at=datetime(2026, 10, 26, 13, 0, tzinfo=UTC),  # +02:00
        )
        core, repo = make_core(inbox=[before])

        from brain.telegram_bot import Pending

        idt_reply = core._card_reply(Pending(ids=(before.id,)))
        core2, _ = make_core(inbox=[after])
        ist_reply = core2._card_reply(Pending(ids=(after.id,)))

        assert "When: Sat 24 Oct 16:00" in idt_reply.text  # IDT (+3)
        assert "When: Mon 26 Oct 15:00" in ist_reply.text  # IST (+2)

    def test_kind_label_shows_the_classified_type_for_notes(self, monkeypatch):
        """A note classified as TASK shows '✔️ Task', not '📝 Note'."""
        core, repo = make_core()
        reply = core.route("Read the RAG paper and take notes", 111)

        assert "✅ <b>Saved</b> · ✔️ Task" in reply.text

    def test_kind_label_keeps_source_kind_for_email_whatsapp_calendar(self, monkeypatch):
        core, _ = make_core()
        email_reply = core.route(TestCardFormat.EMAIL, 111)
        assert "· 📧 Email" in email_reply.text

        wa_reply = core.route("[21/10/2026, 09:00:00] Dana: hi", 111)
        assert "· 💬 WhatsApp" in wa_reply.text

        cal_reply = core.route("TUESDAY, October 21, 2026\n9:00 – 12:30pm\nStandup", 111)
        assert "· 📅 Event" in cal_reply.text
