"""Deterministic date resolution for extracted children (brief 3b, 3c).

Hebrew calendar via pyluach (delta item 2a), weekday-vs-date conflicts
(item 2b), source-relative resolution with the DST-safe timezone rule
(item 41).
"""

from __future__ import annotations

from datetime import UTC, date, datetime, time
from zoneinfo import ZoneInfo

from brain.services.extraction import dates as xdates

TZ = ZoneInfo("Asia/Jerusalem")


def test_hebrew_calendar_date_resolves_via_pyluach():
    """'ז' בחשוון in an email sent 15.10.2026 -> Sun 18.10.2026 (delta item 1)."""
    resolved = xdates.resolve_hebrew_date(
        "כינוס הורים ז' בחשוון באמפי", after=date(2026, 10, 15)
    )
    assert resolved is not None
    day, month = resolved
    assert day == date(2026, 10, 18)
    assert month == 8  # Cheshvan
    assert day.weekday() == 6  # Sunday — the delta's own calendar derivation


def test_hebrew_date_rolls_to_next_year_when_past():
    """ט"ו בשבט in a December email means the UPCOMING February."""
    resolved = xdates.resolve_hebrew_date('חג האילנות ט"ו בשבט', after=date(2026, 12, 25))
    assert resolved is not None
    assert resolved[0] == date(2027, 1, 23)


def test_hebrew_numerals_are_additive():
    assert xdates._hebrew_numeral('ז\'') == 7
    assert xdates._hebrew_numeral('ט"ו') == 15  # ט(9)+ו(6), never tens-first
    assert xdates._hebrew_numeral('כה') == 25
    assert xdates._hebrew_numeral('7') == 7
    assert xdates._hebrew_numeral('שלום') is None


def test_weekday_date_disagreement_flags_conflict():
    """'יום שלישי 7.10': 7.10.2026 is a WEDNESDAY -> conflict, both readings."""
    resolved = xdates.resolve_when(
        "יום שלישי 7.10", datetime(2026, 10, 1, 9, 0, tzinfo=TZ), TZ
    )
    assert resolved is not None
    assert resolved.day == date(2026, 10, 7)  # numeric date wins as the day
    assert resolved.date_conflict
    assert resolved.conflict_note is not None
    assert "Tuesday" in resolved.conflict_note
    assert "Wednesday" in resolved.conflict_note


def test_weekday_date_agreement_flags_nothing():
    resolved = xdates.resolve_when(
        "חולצה לבנה יום חמישי 22.10", datetime(2026, 10, 16, 9, 0, tzinfo=TZ), TZ
    )
    assert resolved is not None
    assert resolved.day == date(2026, 10, 22)
    assert not resolved.date_conflict


def test_relative_dates_resolve_against_sent_time_not_today():
    """'מחר' in a message sent 21.10 means 22.10 even if run in November."""
    sent = datetime(2026, 10, 21, 13, 0, tzinfo=TZ)
    resolved = xdates.resolve_when("מחר", sent, TZ)
    assert resolved is not None
    assert resolved.day == date(2026, 10, 22)


def test_dst_across_sunday_25_october_2026():
    """Same 13:00 wall clock, opposite UTC offsets across the DST switch.

    Thu 22.10.2026 is inside IDT (UTC+3) -> 13:00 local = 10:00Z.
    Thu 29.10.2026 is inside IST (UTC+2) -> 13:00 local = 11:00Z.
    A fixed-offset conversion would get one of the two wrong (item 41).
    """
    local_before = datetime(2026, 10, 22, 13, 0, tzinfo=TZ)
    local_after = datetime(2026, 10, 29, 13, 0, tzinfo=TZ)
    thu_before = xdates.combine_local(date(2026, 10, 22), time(13, 0), TZ)
    thu_after = xdates.combine_local(date(2026, 10, 29), time(13, 0), TZ)
    assert thu_before == local_before.astimezone(UTC)  # 10:00Z
    assert thu_after == local_after.astimezone(UTC)  # 11:00Z
    assert local_before.utcoffset().total_seconds() == 3 * 3600
    assert local_after.utcoffset().total_seconds() == 2 * 3600
    # and the full path: 'tomorrow' resolved from either side of the switch
    w1 = xdates.resolve_when("מחר", datetime(2026, 10, 21, 13, 0, tzinfo=TZ), TZ)
    w2 = xdates.resolve_when("מחר", datetime(2026, 10, 29, 13, 0, tzinfo=TZ), TZ)
    assert w1 is not None and w2 is not None
    assert w1.day == date(2026, 10, 22)
    assert w2.day == date(2026, 10, 30)


def test_clock_extraction():
    assert xdates.extract_clock("איסוף הילדות 13:35") == time(13, 35)
    assert xdates.extract_clock("ב-9:40 בבוקר") == time(9, 40)
    assert xdates.extract_clock("יום חמישי 22.10") is None
