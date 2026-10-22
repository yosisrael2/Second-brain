"""Paste parsers: detection + extraction for email / WhatsApp / calendar / note.

Fixtures are realistic pastes (Gmail, Outlook, Apple Mail Hebrew, iOS +
Android WhatsApp incl. RTL marks, .ics, Google + Outlook copied events).

Timezone contract: zone-less times are interpreted in the ``tz`` passed to
:func:`parse` (the TIMEZONE setting in production) and returned as aware UTC;
explicit offsets (Outlook +0300, ICS TZID/Z) are honored as given. The
original raw strings stay in ``meta`` for audit.
"""

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest

from brain.config import local_timezone_name
from brain.services.paste import ParsedChunk, detect_kind, parse

JERUSALEM = ZoneInfo("Asia/Jerusalem")
NEW_YORK = ZoneInfo("America/New_York")


class TestDetectKind:
    def test_email_by_headers(self):
        assert detect_kind("From: Ada <a@b.c>\nSubject: hi\n\nbody") == "email"

    def test_email_by_apple_mail_marker(self):
        assert detect_kind("Begin forwarded message:\n\nFrom: a@b.c\n\nHello") == "email"

    def test_whatsapp_ios_bracket(self):
        assert detect_kind("[21/10/2026, 10:00:33] Dana: hi") == "whatsapp"

    def test_whatsapp_android_plain(self):
        assert detect_kind("21/10/2026, 10:00 - Dana: hi") == "whatsapp"

    def test_calendar_ics(self):
        assert detect_kind("BEGIN:VEVENT\nSUMMARY:x\nEND:VEVENT") == "calendar"

    def test_calendar_google_text(self):
        assert detect_kind("Team sync\nTuesday, October 21 · 10:00 – 11:00am") == "calendar"

    def test_note_fallback(self):
        assert detect_kind("a plain thought") == "note"

    def test_kind_hint_wins(self):
        assert detect_kind("a plain thought", kind_hint="email") == "email"


class TestEmail:
    def test_gmail_forward(self):
        text = (
            "---------- Forwarded message ---------\n"
            "From: Ada Levi <ada@example.com>\n"
            "Date: Tue, Oct 21, 2026 at 10:14 AM\n"
            "Subject: Quarterly OKR draft\n"
            "To: <team@example.com>\n"
            "\n"
            "Hi team,\n"
            "Please review the draft before Thursday.\n"
            "— Ada"
        )
        parsed = parse(text, tz=JERUSALEM)

        assert parsed.kind == "email"
        assert parsed.chunks[0].source == "email"
        assert parsed.chunks[0].meta["from"] == "Ada Levi <ada@example.com>"
        assert parsed.chunks[0].meta["subject"] == "Quarterly OKR draft"
        # zone-less Gmail date: interpreted in TIMEZONE, stored as aware UTC
        assert parsed.chunks[0].occurred_at == datetime(2026, 10, 21, 7, 14, tzinfo=UTC)
        assert parsed.chunks[0].meta["date"] == "Tue, Oct 21, 2026 at 10:14 AM"  # audit
        assert parsed.chunks[0].meta["date_iso"] == "2026-10-21T07:14:00+00:00"
        # nothing is lost: the item text is the full paste
        assert parsed.chunks[0].raw_text == text

    def test_outlook_rfc2822_date_offset_honored_as_given(self):
        text = (
            "From: Vendor <vendor@corp.com>\n"
            "To: me@example.com\n"
            "Subject: DPA signature\n"
            "Date: Wed, 22 Oct 2026 09:30:00 +0300\n"
            "\n"
            "Please sign the attached DPA."
        )
        parsed = parse(text)

        assert parsed.chunks[0].occurred_at == datetime.fromisoformat(
            "2026-10-22T09:30:00+03:00"
        )
        assert parsed.chunks[0].meta["date"] == "Wed, 22 Oct 2026 09:30:00 +0300"
        assert parsed.chunks[0].meta["date_iso"] == "2026-10-22T09:30:00+03:00"
        assert parsed.chunks[0].meta["subject"] == "DPA signature"

    def test_hebrew_apple_mail_forward(self):
        text = (
            "תחילת הודעה מועברת:\n"
            "מאת: דנה כהן <dana@example.com>\n"
            "תאריך: 21 באוק׳ 2026 בשעה 14:05\n"
            "נושא: חידוש רישיון התוכנה\n"
            "אל: <ops@example.com>\n"
            "\n"
            "הרישיון פג בעוד שבוע — צריך לחדש."
        )
        parsed = parse(text)

        assert parsed.kind == "email"
        assert parsed.chunks[0].meta["subject"] == "חידוש רישיון התוכנה"
        assert "רישיון" in parsed.chunks[0].raw_text

    def test_unparseable_date_stays_none_but_kept_in_meta(self):
        text = "From: a@b.c\nSubject: s\nDate: someday\n\nbody"
        parsed = parse(text)

        assert parsed.chunks[0].occurred_at is None
        assert parsed.chunks[0].meta["date"] == "someday"

    def test_body_never_empty(self):
        parsed = parse("From: a@b.c\nSubject: only subject line")

        assert parsed.chunks[0].raw_text.strip()


class TestWhatsApp:
    def test_ios_bracket_format_with_seconds(self):
        parsed = parse("[21/10/2026, 10:00:33] Dana: Renew the TLS certificate", tz=JERUSALEM)

        assert parsed.kind == "whatsapp"
        assert parsed.chunks[0].meta["messages"][0] == {
            "sender": "Dana",
            "at": "2026-10-21T07:00:33+00:00",  # 10:00:33 IDT (+03:00) as UTC
            "at_raw": "21/10/2026, 10:00:33",
        }
        assert parsed.chunks[0].occurred_at == datetime(2026, 10, 21, 7, 0, 33, tzinfo=UTC)

    def test_android_plain_format_24h(self):
        parsed = parse("21/10/2026, 14:30 - Dana: moved the call to 15:00", tz=JERUSALEM)

        assert parsed.chunks[0].meta["messages"][0]["sender"] == "Dana"
        assert parsed.chunks[0].occurred_at == datetime(2026, 10, 21, 11, 30, tzinfo=UTC)

    def test_12h_am_pm(self):
        parsed = parse("[10/2/2026, 9:05:00 PM] Rami: see you tomorrow", tz=JERUSALEM)

        # Feb 2026 = IST (+02:00) in Israel
        assert parsed.chunks[0].occurred_at == datetime(2026, 2, 10, 19, 5, 0, tzinfo=UTC)

    def test_israel_dst_switch_week(self):
        # 2026-10-25: clocks fall back 02:00 IDT -> 01:00 IST; 03:00 is IST
        parsed = parse("[25/10/2026, 03:00:00] Dana: after the switch", tz=JERUSALEM)

        assert parsed.chunks[0].occurred_at == datetime(2026, 10, 25, 1, 0, tzinfo=UTC)

    def test_timezone_override_changes_interpretation(self):
        text = "[21/10/2026, 10:00:00] Dana: same paste"

        in_jerusalem = parse(text, tz=JERUSALEM).chunks[0].occurred_at
        in_new_york = parse(text, tz=NEW_YORK).chunks[0].occurred_at

        assert in_jerusalem == datetime(2026, 10, 21, 7, 0, tzinfo=UTC)
        assert in_new_york == datetime(2026, 10, 21, 14, 0, tzinfo=UTC)  # 10:00 EDT (-04:00)

    def test_default_tz_is_the_machine_local_zone(self):
        parsed = parse("[21/10/2026, 10:00:00] Dana: hi")

        local = ZoneInfo(local_timezone_name())
        assert parsed.chunks[0].occurred_at == expected_local(local)

    def test_rtl_marks_stripped_hebrew_text_kept(self):
        text = (
            "‏[21/10/2026, 21:05:10] ‏דנה: הפגישה עם הספק עברה ליום רביעי\n"
            "\u200e[21/10/2026, 21:06:00] \u200fיוסי: אישור, עדכנתי ביומן"
        )
        parsed = parse(text, tz=JERUSALEM)

        assert "\u200e" not in parsed.chunks[0].raw_text
        assert "\u200f" not in parsed.chunks[0].raw_text
        assert "עברה ליום רביעי" in parsed.chunks[0].raw_text
        assert parsed.chunks[0].meta["messages"][1]["sender"] == "יוסי"

    def test_multiline_message_appends_continuation_lines(self):
        text = "[21/10/2026, 09:00:00] Dana: agenda:\n- budget\n- hiring"
        parsed = parse(text, tz=JERUSALEM)

        assert len(parsed.chunks) == 1
        assert "- budget" in parsed.chunks[0].raw_text
        assert "- hiring" in parsed.chunks[0].raw_text

    def test_split_true_yields_one_chunk_per_message(self):
        text = "[21/10/2026, 09:00:00] Dana: one\n[21/10/2026, 09:01:00] Rami: two"
        parsed = parse(text, split=True, tz=JERUSALEM)

        assert [chunk.meta["messages"][0]["sender"] for chunk in parsed.chunks] == ["Dana", "Rami"]
        assert parsed.chunks[0].occurred_at == datetime(2026, 10, 21, 6, 0, tzinfo=UTC)
        assert parsed.chunks[1].occurred_at == datetime(2026, 10, 21, 6, 1, tzinfo=UTC)

    def test_default_joins_thread_into_one_chunk(self):
        text = "[21/10/2026, 09:00:00] Dana: one\n[21/10/2026, 09:01:00] Rami: two"
        parsed = parse(text, tz=JERUSALEM)

        assert len(parsed.chunks) == 1
        assert "one" in parsed.chunks[0].raw_text
        assert "two" in parsed.chunks[0].raw_text
        assert parsed.chunks[0].meta["count"] == 2

    def test_line_without_sender_continues_previous_message(self):
        text = "[21/10/2026, 09:00:00] Dana: agenda:\n- budget\n- hiring"
        parsed = parse(text, split=True, tz=JERUSALEM)

        assert len(parsed.chunks) == 1
        assert "agenda:" in parsed.chunks[0].raw_text
        assert "- budget" in parsed.chunks[0].raw_text


class TestCalendar:
    def test_ics_vevent(self):
        text = (
            "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nBEGIN:VEVENT\r\n"
            "SUMMARY:Vendor review\r\n"
            "DTSTART;TZID=Asia/Jerusalem:20261021T100000\r\n"
            "DTEND;TZID=Asia/Jerusalem:20261021T110000\r\n"
            "LOCATION:Board room\r\n"
            "ORGANIZER;CN=Ada:mailto:ada@example.com\r\n"
            "ATTENDEE;CN=Bob:mailto:bob@example.com\r\n"
            "END:VEVENT\r\nEND:VCALENDAR"
        )
        parsed = parse(text)

        assert parsed.kind == "calendar"
        meta = parsed.chunks[0].meta
        assert meta["title"] == "Vendor review"
        assert meta["location"] == "Board room"
        # TZID is explicit: honored as given, not re-interpreted
        assert meta["start"] == "2026-10-21T10:00:00+03:00"
        assert meta["end"] == "2026-10-21T11:00:00+03:00"
        assert meta["start_raw"] == "20261021T100000"
        assert "bob@example.com" in meta["attendees"]
        assert "ada@example.com" in meta["organizer"]
        assert parsed.chunks[0].occurred_at == datetime.fromisoformat(
            "2026-10-21T10:00:00+03:00"
        )

    def test_ics_utc_zulu_honored(self):
        parsed = parse("SUMMARY:x\nDTSTART:20261021T100000Z")

        assert parsed.chunks[0].occurred_at == datetime(2026, 10, 21, 10, 0, tzinfo=UTC)

    def test_google_copied_event(self):
        year = datetime.now().year
        parsed = parse(
            "Team sync\nTuesday, October 21 · 10:00 – 11:00am\nMeeting room 2",
            tz=JERUSALEM,
        )

        assert parsed.chunks[0].meta["title"] == "Team sync"
        assert parsed.chunks[0].occurred_at == datetime(year, 10, 21, 7, 0, tzinfo=UTC)
        assert parsed.chunks[0].meta["start"] == f"{year}-10-21T07:00:00+00:00"
        assert parsed.chunks[0].meta["end"] == f"{year}-10-21T08:00:00+00:00"
        assert parsed.chunks[0].meta["start_raw"] == "Tuesday, October 21 · 10:00 – 11:00am"
        assert parsed.chunks[0].meta["location"] == "Meeting room 2"

    def test_outlook_copied_event(self):
        text = (
            "DPA signature call\n"
            "Thursday, October 22, 2026\n"
            "9:30 AM - 10:00 AM\n"
            "Location: Teams call\n"
            "Attendees: ada@example.com, bob@example.com"
        )
        parsed = parse(text, tz=JERUSALEM)

        meta = parsed.chunks[0].meta
        assert meta["title"] == "DPA signature call"
        assert meta["start"] == "2026-10-22T06:30:00+00:00"  # 09:30 IDT
        assert meta["end"] == "2026-10-22T07:00:00+00:00"
        assert meta["start_raw"] == "9:30 AM - 10:00 AM"
        assert meta["location"] == "Teams call"
        assert "bob@example.com" in meta["attendees"]

    def test_12h_pm_end_derivation(self):
        year = datetime.now().year
        parsed = parse("Offsite planning\nFriday, October 23 · 9:00 – 12:30pm", tz=JERUSALEM)

        assert parsed.chunks[0].occurred_at == datetime(year, 10, 23, 6, 0, tzinfo=UTC)
        assert parsed.chunks[0].meta["end"] == f"{year}-10-23T09:30:00+00:00"


class TestNote:
    def test_plain_text(self):
        parsed = parse("Read the RAG paper and take notes")

        assert parsed.kind == "note"
        assert parsed.chunks == [
            ParsedChunk(
                raw_text="Read the RAG paper and take notes",
                occurred_at=None,
                meta={},
                source="note",
            )
        ]

    def test_chunk_source_reflects_kind(self):
        assert parse("[21/10/2026, 10:00:00] D: hi").chunks[0].source == "whatsapp"
        assert parse("SUMMARY:x\nDTSTART:20261021T100000Z").chunks[0].source == "calendar"


class TestShape:
    def test_chunk_is_frozen(self):
        from dataclasses import FrozenInstanceError

        chunk = ParsedChunk(raw_text="x", occurred_at=None, meta={}, source="note")

        with pytest.raises(FrozenInstanceError):
            chunk.raw_text = "y"  # type: ignore[misc]

    def test_extracted_json_serializable(self):
        import json

        parsed = parse("[21/10/2026, 10:00:33] Dana: hi")

        json.dumps(parsed.extracted)

    def test_detect_kind_prefers_calendar_over_email_for_ics(self):
        text = "From: x@y.z\nSubject: invite\n\nBEGIN:VEVENT\nSUMMARY:s\nEND:VEVENT"
        assert detect_kind(text) == "calendar"


def expected_local(zone: ZoneInfo) -> datetime:
    """Aware-UTC instant of 2026-10-21 10:00 in the given zone."""
    local = datetime(2026, 10, 21, 10, 0).replace(tzinfo=zone)
    return local.astimezone(UTC)
