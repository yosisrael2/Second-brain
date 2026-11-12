"""Small Telegram UX fixes (card header, toasts, silent pings, card cleanup).

DB-free: cards render from transient MemoryItem rows, the adapter runs
against duck-typed tg/query fakes. Invented names only (public repo).
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from tests.test_telegram_bot import make_core

from brain.db.enums import Actionability, ItemType
from brain.db.models import MemoryItem
from brain.services.ask import AskResult
from brain.services.proposals import (
    ping_is_quiet,
    proposal_keyboard,
    render_source_card,
    source_short_title,
)
from brain.telegram_bot import Reply

TZ = "Asia/Jerusalem"

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

    def test_a_pre_durable_proposal_button_is_an_expired_toast(self):
        core, _ = make_core()
        reply = core.handle_callback("pk:abcd1234:0", 111)  # a card from before #172
        assert reply.text == "" and "expired" in reply.toast

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


# --- 3. silent notifications ----------------------------------------------------


def _send_one(reply: Reply) -> list[dict]:
    from brain.telegram_bot import _send  # noqa: PLC0415

    tg = _TG()
    asyncio.run(_send(tg, 555, reply))
    return tg.sent


class TestSilent:
    def test_silent_reply_disables_the_notification(self):
        sent = _send_one(Reply("☀️ digest", silent=True))
        assert sent[0]["disable_notification"] is True

    def test_default_reply_rings(self):
        sent = _send_one(Reply("📋 card"))
        assert not sent[0].get("disable_notification")

    def _claim(self):
        return SimpleNamespace(name="Morning digest")

    def test_scheduled_digest_is_silent(self):
        core, _ = make_core()
        outcome = SimpleNamespace(
            status="ok",
            result=AskResult(answer="Nothing new", cited_ids=[]),
            llm_unavailable=False,
            error=None,
        )
        assert core.scheduled_reply(self._claim(), outcome, "en").silent is True

    def test_scheduled_failure_still_rings(self):
        core, _ = make_core()
        outcome = SimpleNamespace(status="error", result=None, llm_unavailable=False, error="x")
        assert core.scheduled_reply(self._claim(), outcome, "he").silent is False

    def test_ping_more_than_a_day_away_is_quiet(self):
        now = datetime(2026, 11, 2, 9, 0, tzinfo=UTC)
        far = MemoryItem(raw_text="Bake sale", occurred_at=now + timedelta(hours=30))
        near = MemoryItem(raw_text="Bake sale", occurred_at=now + timedelta(hours=20))
        naive = MemoryItem(raw_text="Bake sale", occurred_at=datetime(2026, 11, 3, 18, 0))
        assert ping_is_quiet(far, now, TZ) is True
        assert ping_is_quiet(near, now, TZ) is False
        assert ping_is_quiet(naive, now, TZ) is True  # 18:00 local next day = 31h away


# --- 4. proposal card: no legend, FYI children collapsed without buttons --------


def _kids() -> list[MemoryItem]:
    when = datetime(2026, 11, 3, 15, 0, tzinfo=UTC)  # Tue 03.11 17:00 local
    return [
        MemoryItem(raw_text="Parents evening", type=ItemType.EVENT, occurred_at=when,
                   applies_to=["Noa"], child_state="proposed"),
        MemoryItem(raw_text="Grade 6 <b>paper planes</b> & prizes", type=ItemType.FACT,
                   applies_to=["nobody"], child_state="proposed"),
        MemoryItem(raw_text="Sign the trip form", type=ItemType.TASK, occurred_at=when,
                   applies_to=["Dan"], child_state="proposed"),
        MemoryItem(raw_text="The library is closed this week", type=ItemType.FACT,
                   applies_to=["Noa"], child_state="proposed"),
    ]


def _source() -> MemoryItem:
    return MemoryItem(raw_text="Class news", meta={"channel": "telegram"})


class TestCardCleanup:
    def test_english_card(self):
        card = render_source_card(_source(), _kids(), TZ)
        assert card.split("\n") == [
            "🧠 Found 4 items in: Class news",
            "1. 📅 Parents evening · Tue 03.11 17:00 · Noa",
            "2. ✔️ Sign the trip form · Tue 03.11 17:00 · Dan",
            "<blockquote expandable>ℹ️ For your information (2)",
            "ℹ️ Grade 6 &lt;b&gt;paper planes&lt;/b&gt; &amp; prizes · not for us",
            "ℹ️ The library is closed this week · Noa</blockquote>",
        ]

    def test_hebrew_card(self):
        names = {"Noa": "נועה", "Dan": "דן"}
        card = render_source_card(_source(), _kids(), TZ, lang="he", names=names)
        lines = card.split("\n")
        assert lines[0] == "🧠 מצאתי 4 פריטים בהודעה: Class news"
        assert lines[1].startswith("1. 📅") and lines[1].endswith("נועה")
        assert lines[2].startswith("2. ✔️") and lines[2].endswith("דן")
        assert lines[3] == "<blockquote expandable>ℹ️ לידיעה בלבד (2)"
        assert lines[4].endswith("· לא רלוונטי אלינו")
        assert lines[5].endswith("</blockquote>")
        assert "📅 אירוע · ✔️ משימה" not in card  # the legend is gone

    def test_keyboard_rows_match_the_numbers_and_skip_fyi(self):
        kids = _kids()
        for kid in kids:
            kid.id = uuid.uuid4()
        rows = proposal_keyboard("tok", kids)
        assert [[label for label, _ in row] for row in rows] == [
            ["1 ✅ Keep", "1 ✏️ Fix", "1 🗑 Drop"],
            ["2 ✅ Keep", "2 ✏️ Fix", "2 🗑 Drop"],
            ["✅ Keep all"],
        ]
        # each row's buttons act on ITS child: line 2 is the 3rd child
        assert [row[0][1] for row in rows[:2]] == [f"pk:{kids[0].id}", f"pk:{kids[2].id}"]

    def test_no_fyi_block_when_everything_is_actionable(self):
        kids = [kid for kid in _kids() if kid.type != ItemType.FACT]
        card = render_source_card(_source(), kids, TZ)
        assert "blockquote" not in card
        assert card.split("\n")[1].startswith("1. ")

    def test_a_fact_with_a_recon_verdict_keeps_its_buttons(self):
        kids = _kids()
        verdict = ("conflict", SimpleNamespace(id="rel-1", provenance={}), None)
        recon = {1: verdict}
        card = render_source_card(_source(), kids, TZ, recon=recon)
        assert "2. ℹ️ Grade 6" in card  # numbered, out of the FYI block
        rows = proposal_keyboard("tok", kids, recon=recon)
        assert [row[0][0] for row in rows] == [
            "1 ✅ Keep", "2 ✅ Keep existing", "3 ✅ Keep", "✅ Keep all"
        ]

    def test_fyi_line_with_an_action_stays_numbered(self):
        kids = _kids()
        kids[3].actionability = Actionability.NOW
        card = render_source_card(_source(), kids, TZ)
        assert "3. ℹ️ The library is closed this week" in card
