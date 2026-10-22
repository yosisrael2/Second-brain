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
        assert labels == ["File", "Type", "Delete", "Undo"]
        assert reply.keyboard[1] == [("Type", f"t:{reply.keyboard[1][0][1].split(':')[1]}")]

    def test_undo_callback_deletes_the_items_created_by_that_message(self, monkeypatch):
        core, repo = make_core()
        reply = core.route("hello note one", 111)
        assert len(repo.items) == 1
        token = reply.keyboard[3][0][1].split(":", 1)[1]  # "u:<token>"

        undo = core.handle_callback(f"u:{token}")

        assert repo.items == {}
        assert "Undo" in undo.text or "deleted" in undo.text.lower()

    def test_delete_and_file_callbacks(self, monkeypatch):
        core, repo = make_core()
        reply_one = core.route("hello note two", 111)
        reply_other = core.route("hello note three", 111)

        d_token = reply_one.keyboard[2][0][1].split(":", 1)[1]
        core.handle_callback(f"d:{d_token}")
        assert len(repo.items) == 1  # note two deleted, note three remains

        f_token = reply_other.keyboard[0][0][1].split(":", 1)[1]
        core.handle_callback(f"f:{f_token}")
        assert all(i.status == ItemStatus.FILED for i in repo.items.values())

    def test_type_submenu_then_apply(self, monkeypatch):
        core, repo = make_core()
        reply = core.route("hello note four", 111)
        t_label, t_data = reply.keyboard[1][0]
        t_token = t_data.split(":", 1)[1]

        submenu = core.handle_callback(f"t:{t_token}")
        assert submenu.keyboard is not None
        type_labels = [label for row in submenu.keyboard for label, _ in row]
        assert set(type_labels) == {"task", "decision", "note", "event", "reference", "idea"}

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
        assert "1 item(s)" in joined.text

    def test_save_separately_makes_one_item_per_message(self, monkeypatch):
        core, repo = make_core()
        reply = core.route(self.PASTE, 111)
        token = _choice_token(reply, "Save separately")

        split = core.handle_callback(f"wm:{token}")
        assert len(repo.items) == 2
        assert "2 item(s)" in split.text


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
        self.chat_actions: list[tuple] = []
        self.commands: list | None = None
        self._file_bytes = file_bytes

    async def send_message(self, chat_id=None, text=None, **kwargs):
        self.sent.append({"chat_id": chat_id, "text": text, **kwargs})

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
        assert "12" not in reply.text.split("last")[0]  # header, not all items
        assert "item 11" in reply.text and "item 2" in reply.text  # last 10
        assert "item 0" not in reply.text and "item 1\n" not in reply.text

    def test_bot_commands_registered_at_startup(self, monkeypatch):
        core, _ = make_core()
        tg = _FakeTGForPoll()
        tg.queue([])

        _run_poll(tg, core)

        names = [c.command for c in (tg.commands or [])]
        assert names == ["ask", "search", "review", "recent", "status", "help"]


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

        assert reply.text.startswith("📧")  # email emoji
        assert "dana@example.com" in reply.text  # from
        assert "Kickoff meeting" in reply.text  # subject
        assert "2026" in reply.text  # when
        assert "type:" in reply.text and "importance:" in reply.text
        assert "actionability:" in reply.text and "status:" in reply.text

        cal = core.route(
            "TUESDAY, October 21, 2026\n9:00 – 12:30pm\nStandup\n"
            "Location: Haifa office",
            111,
        )
        assert "Haifa office" in cal.text  # location (calendar kind)

    def test_html_is_escaped(self, monkeypatch):
        core, _ = make_core()
        reply = core.route(self.EMAIL, 111)

        assert "<b>tomorrow</b>" not in reply.text
        assert "&lt;b&gt;tomorrow&lt;/b&gt;" in reply.text

    def test_kind_emoji_per_kind(self, monkeypatch):
        core, _ = make_core()
        assert core.route("[21/10/2026, 09:00:00] Dana: hi", 111).text.startswith("💬")
        cal_reply = core.route("TUESDAY, October 21, 2026\n9:00 – 12:30pm\nStandup", 111)
        assert cal_reply.text.startswith("📅")


# --- 8. exit codes -------------------------------------------------------------


class TestExitCodes:
    def test_no_token_logs_disabled_and_exits_zero(self, monkeypatch, caplog):
        from brain import telegram_bot

        settings = telegram_bot.Settings(TELEGRAM_BOT_TOKEN=None, _env_file=None)
        with caplog.at_level(logging.ERROR, logger="brain.telegram_bot"):
            assert telegram_bot.run(settings) == 0
        assert "telegram bot disabled (no token)" in caplog.text

    def test_conflict_exits_78(self, monkeypatch):
        from brain import telegram_bot

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
