"""Calendar items store a human one-liner as raw_text (full ICS in meta).

Format: "<title> — <Day DD Mon HH:MM–HH:MM> [@ location]", rendered in the
app's TIMEZONE (DST-correct: an event instant before/after the Israeli DST
switch renders with the right wall time). The original ICS stays in
meta['ics_raw'] (calendar-text path: meta['original_text']); all parsed
fields stay in meta. `brain normalize-calendar` rewrites pre-existing items.
"""

import sys
from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo

from tests.fakes import FakeRepository

from brain.config import get_settings
from brain.db.models import MemoryItem
from brain.services.dedupe import content_fingerprint
from brain.services.paste import parse as parse_paste

JERUSALEM = ZoneInfo("Asia/Jerusalem")

_ICS_RONA = (
    "BEGIN:VEVENT\n"
    "SUMMARY:צופים רונה\n"
    "DTSTART;TZID=Asia/Jerusalem:20261029T170000\n"
    "DTEND;TZID=Asia/Jerusalem:20261029T181500\n"
    "LOCATION:עמק חפר\n"
    "ORGANIZER;CN=zohari:mailto:zohari@wix.com\n"
    "ATTENDEE:mailto:yosisrael@gmail.com\n"
    "END:VEVENT"
)


def _parse_ics(text: str) -> Any:
    return parse_paste(text, kind_hint="calendar", tz=JERUSALEM)


# ---- one-liner format ----------------------------------------------------------


def test_ics_raw_text_is_a_human_one_liner_and_ics_goes_to_meta():
    parsed = _parse_ics(_ICS_RONA)

    chunk = parsed.chunks[0]
    assert chunk.raw_text == "צופים רונה — Thu 29 Oct 17:00–18:15 @ עמק חפר"
    assert chunk.meta["ics_raw"] == _ICS_RONA
    # parsed fields are untouched
    assert chunk.meta["title"] == "צופים רונה"
    assert chunk.meta["location"] == "עמק חפר"
    assert chunk.occurred_at == datetime(2026, 10, 29, 15, 0, tzinfo=UTC)  # 17:00 IST


def test_one_liner_without_location_has_no_at_part():
    ics = _ICS_RONA.replace("LOCATION:עמק חפר\n", "")
    parsed = _parse_ics(ics)

    assert parsed.chunks[0].raw_text == "צופים רונה — Thu 29 Oct 17:00–18:15"


def test_one_liner_dst_summer_and_winter_wall_times():
    # 16:00Z on Oct 20 2026 = 19:00 IDT (UTC+3); 15:00Z on Oct 29 = 17:00 IST (UTC+2)
    summer = _parse_ics(
        "BEGIN:VEVENT\nSUMMARY:כדורגל איתמר\nDTSTART:20261020T160000Z\n"
        "DTEND:20261020T171500Z\nEND:VEVENT"
    )
    winter = _parse_ics(
        "BEGIN:VEVENT\nSUMMARY:כדורגל רונה\nDTSTART:20261029T150000Z\n"
        "DTEND:20261029T161500Z\nEND:VEVENT"
    )

    assert summer.chunks[0].raw_text == "כדורגל איתמר — Tue 20 Oct 19:00–20:15"
    assert winter.chunks[0].raw_text == "כדורגל רונה — Thu 29 Oct 17:00–18:15"


def test_one_liner_date_only_event_shows_day_without_times():
    parsed = _parse_ics(
        "BEGIN:VEVENT\nSUMMARY:סבבה\nDTSTART;VALUE=DATE:20261029\nEND:VEVENT"
    )

    assert parsed.chunks[0].raw_text == "סבבה — Thu 29 Oct"


def test_calendar_text_path_gets_the_same_one_liner_and_keeps_original():
    parsed = parse_paste(
        "Team sync\nTuesday, October 21 · 10:00 – 11:00am\nMeeting room 2",
        kind_hint="auto",
        tz=JERUSALEM,
    )

    assert parsed.kind == "calendar"
    assert parsed.chunks[0].raw_text == "Team sync — Wed 21 Oct 10:00–11:00 @ Meeting room 2"
    assert parsed.chunks[0].meta["original_text"].startswith("Team sync")


# ---- brain normalize-calendar CLI -----------------------------------------------


def _legacy_calendar_item(repo: FakeRepository) -> MemoryItem:
    item = MemoryItem(
        raw_text=_ICS_RONA,  # pre-PR storage: the whole ICS blob
        source="calendar",
        occurred_at=datetime(2026, 10, 29, 15, 0, tzinfo=UTC),
        meta={
            "title": "צופים רונה",
            "start": "2026-10-29T17:00:00+02:00",
            "end": "2026-10-29T18:15:00+02:00",
            "location": "עמק חפר",
        },
    )
    repo.add(item)
    return item


def test_normalize_calendar_rewrites_ics_items_to_one_liners(
    monkeypatch, capsys
) -> None:
    from brain import cli

    repo = FakeRepository()
    item = _legacy_calendar_item(repo)
    monkeypatch.setenv("TIMEZONE", "Asia/Jerusalem")
    get_settings.cache_clear()

    try:
        assert cli._normalize_calendar(repo=repo) == 0
    finally:
        get_settings.cache_clear()

    assert item.raw_text == "צופים רונה — Thu 29 Oct 17:00–18:15 @ עמק חפר"
    assert item.meta["ics_raw"] == _ICS_RONA
    assert item.fingerprint == content_fingerprint(
        item.raw_text, item.occurred_at, "calendar"
    )
    assert "normalized 1 calendar item" in capsys.readouterr().out


def test_normalize_calendar_leaves_non_calendar_items_alone(monkeypatch) -> None:
    from brain import cli

    repo = FakeRepository()
    note = MemoryItem(raw_text="just a note", source="note")
    repo.add(note)
    monkeypatch.setenv("TIMEZONE", "Asia/Jerusalem")

    assert cli._normalize_calendar(repo=repo) == 0
    assert note.raw_text == "just a note"


def test_normalize_calendar_skips_items_without_usable_meta(monkeypatch) -> None:
    from brain import cli

    repo = FakeRepository()
    broken = MemoryItem(
        raw_text="BEGIN:VEVENT\nEND:VEVENT",  # no title/start in meta, ICS body
        source="calendar",
        meta={},
    )
    repo.add(broken)
    monkeypatch.setenv("TIMEZONE", "Asia/Jerusalem")

    assert cli._normalize_calendar(repo=repo) == 0
    assert broken.raw_text == "BEGIN:VEVENT\nEND:VEVENT"  # unchanged, not corrupted


def test_normalize_calendar_is_wired_into_main(monkeypatch) -> None:
    from brain import cli

    called: list[list[str]] = []
    monkeypatch.setattr(
        cli,
        "_normalize_calendar",
        lambda repo=None: called.append(sys.argv) or 0,
    )

    assert cli.main(["normalize-calendar"]) == 0
    assert len(called) == 1
