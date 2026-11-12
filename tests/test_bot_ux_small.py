"""Small Telegram UX fixes (card header, toasts, silent pings, card cleanup).

DB-free: cards render from transient MemoryItem rows, the adapter runs
against duck-typed tg/query fakes. Invented names only (public repo).
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from tests.test_telegram_bot import make_core

from brain.db.models import MemoryItem
from brain.services.proposals import source_short_title
from brain.telegram_bot import Reply

# --- 1. card header never names the transport ----------------------------------


class TestSourceTitleSkipsTransport:
    def test_telegram_paste_uses_the_first_line_not_the_channel(self):
        source = MemoryItem(
            raw_text="\n  אסיפת הורים בכיתה הדקל  \nביום שלישי בערב",
            source="telegram",
            meta={"channel": "telegram"},
        )
        assert source_short_title(source) == "אסיפת הורים בכיתה הדקל"

    def test_every_transport_name_is_skipped(self):
        for channel in ("telegram", "Telegram", "email", "gmail", "manual", "api", "whatsapp"):
            source = MemoryItem(raw_text="Bake sale on Friday", meta={"channel": channel})
            assert source_short_title(source) == "Bake sale on Friday", channel

    def test_subject_still_wins(self):
        source = MemoryItem(
            raw_text="hello", meta={"channel": "telegram", "subject": "Class trip"}
        )
        assert source_short_title(source) == "Class trip"

    def test_whatsapp_group_name_is_a_real_title(self):
        source = MemoryItem(
            raw_text="[WhatsApp · הורי הדקל]\n[01/11/2026, 09:00] Dan: hi",
            meta={"kind": "whatsapp_conversation", "channel": "הורי הדקל"},
        )
        assert source_short_title(source) == "הורי הדקל"

    def test_skips_lines_without_letters(self):
        body = "-----\n🎉🎉\n> Picnic in the park"
        source = MemoryItem(raw_text=body, meta={"channel": "telegram"})
        assert source_short_title(source) == "Picnic in the park"

    def test_long_first_line_is_trimmed(self):
        source = MemoryItem(raw_text="word " * 40, meta={"channel": "telegram"})
        title = source_short_title(source)
        assert len(title) <= 60 and title.endswith("…")

    def test_falls_back_to_the_sender_when_no_text_line(self):
        source = MemoryItem(raw_text="🎉\n----", meta={"channel": "telegram", "sender": "Noa"})
        assert source_short_title(source) == "Noa"
        email = MemoryItem(raw_text="", meta={"from": "dan@example.com"})
        assert source_short_title(email) == "dan@example.com"


# --- 2. tap feedback is a toast, not a chat message -----------------------------


class _Query:
    """A duck-typed CallbackQuery recording how it was answered."""

    def __init__(self, data: str) -> None:
        self.data = data
        self.message = SimpleNamespace(chat_id=555, message_id=9)
        self.answers: list[dict] = []

    async def answer(self, **kwargs) -> None:
        self.answers.append(kwargs)


class _TG:
    def __init__(self) -> None:
        self.sent: list[dict] = []
        self.edits: list[dict] = []

    async def send_message(self, **kwargs) -> None:
        self.sent.append(kwargs)

    async def edit_message_text(self, **kwargs) -> None:
        self.edits.append(kwargs)


class _StubCore:
    def __init__(self, reply: Reply) -> None:
        self._reply = reply

    def is_allowed(self, user_id) -> bool:
        return True

    def handle_callback(self, data: str, user_id=None) -> Reply:
        return self._reply


def _tap(core, data: str = "x:1") -> tuple[_Query, _TG]:
    from brain.telegram_bot import _handle_update  # noqa: PLC0415

    query, tg = _Query(data), _TG()
    update = SimpleNamespace(effective_user=SimpleNamespace(id=111), callback_query=query)
    asyncio.run(_handle_update(tg, core, update))
    return query, tg


class TestToasts:
    def test_expired_button_is_a_toast_only(self):
        core, _ = make_core()
        reply = core.handle_callback("f:deadbeef", 111)
        assert reply.text == ""
        assert reply.toast and "expired" in reply.toast

    def test_unknown_button_is_a_toast_only(self):
        core, _ = make_core()
        reply = core.handle_callback("zz:junk", 111)
        assert reply.text == "" and reply.toast

    def test_adapter_answers_with_the_toast_and_sends_nothing(self):
        core, _ = make_core()
        query, tg = _tap(core, "f:deadbeef")
        assert tg.sent == [] and tg.edits == []
        assert len(query.answers) == 1
        assert "expired" in query.answers[0]["text"]
        assert query.answers[0]["show_alert"] is False

    def test_card_edit_and_toast_together(self):
        stub = _StubCore(Reply("🧠 card", edit=True, toast="✅ Kept: <b>Bake</b> &amp; sale"))
        query, tg = _tap(stub)
        assert query.answers == [{"text": "✅ Kept: Bake & sale", "show_alert": False}]
        assert [e["text"] for e in tg.edits] == ["🧠 card"]

    def test_real_content_still_goes_to_the_chat(self):
        query, tg = _tap(_StubCore(Reply("📋 a new card")))
        assert query.answers == [{}]  # a plain ack, no toast
        assert [m["text"] for m in tg.sent] == ["📋 a new card"]

    def test_hebrew_toast(self):
        core, _ = make_core()
        core._read_language = lambda: "he"  # noqa: SLF001 — the stored language
        reply = core.handle_callback("f:deadbeef", 111)
        assert reply.text == "" and reply.toast.startswith("⌛ הכפתורים")
