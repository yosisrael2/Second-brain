"""Small Telegram UX fixes (card header, toasts, silent pings, card cleanup).

DB-free: cards render from transient MemoryItem rows, the adapter runs
against duck-typed tg/query fakes. Invented names only (public repo).
"""

from __future__ import annotations

from brain.db.models import MemoryItem
from brain.services.proposals import source_short_title

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
