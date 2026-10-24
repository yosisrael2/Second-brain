"""P1: recurring events in the paste path (RRULE on a pasted ICS VEVENT).

A pasted VEVENT with an RRULE becomes ONE item per occurrence within
[paste time, paste time + BRAIN_PASTE_RRULE_DAYS (default 60)], each rendered
with the EXISTING calendar one-liner (no second formatter). EXDATE is
honoured; COUNT/UNTIL are honoured; a per-series cap (setting, default 60)
bounds the fan-out; a duplicate re-paste stays idempotent through the
existing fingerprint dedupe. meta carries series_uid + the original RRULE
text. Occurrences before the paste instant are NOT created (the window's
lower bound is the paste time, inclusive).

Timezone: DTSTART with TZID=Asia/Jerusalem is wall-clock stable across the
Israeli DST change (Sun 25 Oct 2026) — a 10:00 local meeting stays 10:00
local every week; the UTC instant shifts with the offset.
"""

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from qdrant_client import QdrantClient
from sqlalchemy import func, select, text
from tests.conftest import assert_test_database
from tests.fakes import StubClassifier
from tests.test_ingest_service import FULL_CLASSIFICATION

from brain.ai.embedder import FakeEmbedder
from brain.ai.vectorstore import QdrantVectorStore
from brain.config import get_settings
from brain.db.models import MemoryItem
from brain.db.session import SessionLocal, engine
from brain.repositories.items import ItemsRepository
from brain.services.indexing import QdrantItemIndexer
from brain.services.ingest import IngestService
from brain.services.paste import parse as parse_paste
from brain.services.temporal import parse_question_ranges

JERUSALEM = ZoneInfo("Asia/Jerusalem")

# frozen 'now' for every parse in this module (the paste time)
NOW = datetime(2026, 10, 21, 12, 0, tzinfo=UTC)  # Wed 21 Oct 2026, 15:00 JLM

# weekly Sunday scouts series, already running (DTSTART before the paste
# moment): in-window occurrences start at the first Sunday >= paste time.
_SUNDAY_SERIES = (
    "BEGIN:VEVENT\n"
    "UID:scouts-series@example.com\n"
    "SUMMARY:צופים רונה\n"
    "DTSTART;TZID=Asia/Jerusalem:20261018T100000\n"
    "DTEND;TZID=Asia/Jerusalem:20261018T111500\n"
    "LOCATION:עמק חפר\n"
    "RRULE:FREQ=WEEKLY\n"
    "END:VEVENT"
)

# weekly Wednesday football series (DTSTART = the paste morning): used for
# the persistence + temporal tests (it has an occurrence on Wed 4.11).
_WEDNESDAY_SERIES = (
    "BEGIN:VEVENT\n"
    "UID:football-series@example.com\n"
    "SUMMARY:כדורגל איתמר\n"
    "DTSTART;TZID=Asia/Jerusalem:20261021T160000\n"
    "DTEND;TZID=Asia/Jerusalem:20261021T171500\n"
    "RRULE:FREQ=WEEKLY\n"
    "END:VEVENT"
)


def _parse_ics(text: str, *, now: datetime = NOW):
    return parse_paste(text, kind_hint="calendar", tz=JERUSALEM, now=now)


def _ics_with_rrule(rrule: str, *, dtstart: str = "20261101T100000", extra: str = "") -> str:
    """DTSTART Sun 1 Nov 2026 10:00 JLM (after NOW) + the given RRULE line."""
    return (
        "BEGIN:VEVENT\n"
        "UID:series@example.com\n"
        "SUMMARY:צופים רונה\n"
        f"DTSTART;TZID=Asia/Jerusalem:{dtstart}\n"
        f"DTEND;TZID=Asia/Jerusalem:{dtstart[:8]}T111500\n"
        f"RRULE:{rrule}\n"
        f"{extra}"
        "END:VEVENT"
    )


# ---- the window and the per-occurrence items ------------------------------------


def test_weekly_rrule_creates_one_item_per_occurrence_in_the_window():
    parsed = _parse_ics(_SUNDAY_SERIES)

    # Sundays from 25 Oct (first >= paste time) through 20 Dec (window end,
    # 60 days after the paste): 25 Oct, 1/8/15/22/29 Nov, 6/13/20 Dec
    assert [c.occurred_at for c in parsed.chunks] == [
        datetime(2026, 10, 25, 8, 0, tzinfo=UTC),  # 10:00 IST (UTC+2)
        datetime(2026, 11, 1, 8, 0, tzinfo=UTC),
        datetime(2026, 11, 8, 8, 0, tzinfo=UTC),
        datetime(2026, 11, 15, 8, 0, tzinfo=UTC),
        datetime(2026, 11, 22, 8, 0, tzinfo=UTC),
        datetime(2026, 11, 29, 8, 0, tzinfo=UTC),
        datetime(2026, 12, 6, 8, 0, tzinfo=UTC),
        datetime(2026, 12, 13, 8, 0, tzinfo=UTC),
        datetime(2026, 12, 20, 8, 0, tzinfo=UTC),
    ]
    assert all(c.occurred_at >= NOW for c in parsed.chunks), "paste time - 0"
    assert all(c.occurred_at <= NOW + timedelta(days=60) for c in parsed.chunks)


def test_each_occurrence_renders_the_existing_one_liner_with_its_own_date():
    parsed = _parse_ics(_SUNDAY_SERIES)

    first, second = parsed.chunks[0], parsed.chunks[1]
    assert first.raw_text == "צופים רונה — Sun 25 Oct 10:00–11:15 @ עמק חפר"
    assert second.raw_text == "צופים רונה — Sun 1 Nov 10:00–11:15 @ עמק חפר"
    # the occurrence start/end in meta are the occurrence's own instants
    assert first.meta["start"] == "2026-10-25T10:00:00+02:00"
    assert second.meta["end"] == "2026-11-01T11:15:00+02:00"


def test_meta_carries_series_uid_and_the_original_rrule_text():
    parsed = _parse_ics(_SUNDAY_SERIES)

    assert parsed.chunks, "the series still produces occurrences"
    for chunk in parsed.chunks:
        assert chunk.meta["series_uid"] == "scouts-series@example.com"
        assert chunk.meta["rrule_raw"] == "FREQ=WEEKLY"


def test_dst_change_keeps_the_wall_time_across_25_oct():
    """A weekly Wednesday series with occurrences on BOTH sides of the Israeli
    DST change: 21 Oct 10:00 JLM = 07:00Z (UTC+3), 28 Oct 10:00 JLM = 08:00Z
    (UTC+2) — the wall clock stays 10:00; a UTC-adder would drift it."""
    parsed = _parse_ics(
        _ics_with_rrule("FREQ=WEEKLY", dtstart="20261021T100000"),
        now=datetime(2026, 10, 21, 6, 0, tzinfo=UTC),
    )

    assert [c.occurred_at for c in parsed.chunks][:2] == [
        datetime(2026, 10, 21, 7, 0, tzinfo=UTC),  # 10:00 IDT (UTC+3)
        datetime(2026, 10, 28, 8, 0, tzinfo=UTC),  # 10:00 IST (UTC+2)
    ]
    assert all("10:00" in c.raw_text for c in parsed.chunks[:2])


def test_exdate_skips_the_occurrence():
    parsed = _parse_ics(
        _ics_with_rrule("FREQ=WEEKLY", extra="EXDATE;TZID=Asia/Jerusalem:20261108T100000\n")
    )

    starts = [c.occurred_at.date().isoformat() for c in parsed.chunks]
    assert "2026-11-08" not in starts
    assert "2026-11-01" in starts and "2026-11-15" in starts


def test_count_is_honoured():
    parsed = _parse_ics(_ics_with_rrule("FREQ=WEEKLY;COUNT=3"))

    assert len(parsed.chunks) == 3


def test_until_is_honoured():
    # UNTIL 15 Nov 09:00Z: the 15 Nov 10:00 JLM (= 08:00Z) occurrence is in,
    # 22 Nov is out
    parsed = _parse_ics(_ics_with_rrule("FREQ=WEEKLY;UNTIL=20261115T090000Z"))

    starts = [c.occurred_at.date().isoformat() for c in parsed.chunks]
    assert "2026-11-15" in starts and "2026-11-22" not in starts


def test_cap_limits_occurrences_per_series(monkeypatch):
    monkeypatch.setenv("BRAIN_PASTE_RRULE_MAX_OCCURRENCES", "4")
    get_settings.cache_clear()
    try:
        parsed = _parse_ics(_ics_with_rrule("FREQ=DAILY"))
        assert len(parsed.chunks) == 4, "the per-series cap bounds the fan-out"
    finally:
        get_settings.cache_clear()


def test_window_days_setting(monkeypatch):
    monkeypatch.setenv("BRAIN_PASTE_RRULE_DAYS", "10")
    get_settings.cache_clear()
    try:
        parsed = _parse_ics(_SUNDAY_SERIES)  # window: 21 Oct 12:00Z .. 31 Oct 12:00Z
        starts = [c.occurred_at.date().isoformat() for c in parsed.chunks]
        assert starts == ["2026-10-25"], "only occurrences within 10 days of the paste"
    finally:
        get_settings.cache_clear()


def test_non_recurring_ics_is_unchanged():
    parsed = _parse_ics(
        "BEGIN:VEVENT\n"
        "UID:one-off@example.com\n"
        "SUMMARY:צופים רונה\n"
        "DTSTART;TZID=Asia/Jerusalem:20261029T170000\n"
        "DTEND;TZID=Asia/Jerusalem:20261029T181500\n"
        "END:VEVENT"
    )

    assert len(parsed.chunks) == 1
    assert parsed.chunks[0].raw_text == "צופים רונה — Thu 29 Oct 17:00–18:15"
    assert "series_uid" not in parsed.chunks[0].meta


def test_malformed_rrule_falls_back_to_the_single_item():
    """A rule dateutil cannot parse never fails the paste: exactly ONE item
    at DTSTART, identical to the non-recurring path ('a paste is never
    lost')."""
    parsed = _parse_ics(_ics_with_rrule("FREQ=SOMETIMES;BYDAY=XX"))

    assert len(parsed.chunks) == 1
    assert parsed.chunks[0].occurred_at == datetime(2026, 11, 1, 8, 0, tzinfo=UTC)
    assert parsed.chunks[0].raw_text == "צופים רונה — Sun 1 Nov 10:00–11:15"
    assert "series_uid" not in parsed.chunks[0].meta
    assert "rrule_raw" not in parsed.chunks[0].meta


def test_naive_until_without_leading_semicolon_never_raises_and_honours_until():
    """A producer-bug naive UNTIL at RRULE position 0 (no leading ';') with a
    TZID DTSTART is interpreted in the DTSTART's timezone — the same rule the
    ';UNTIL=' form already follows — and never raises."""
    parsed = _parse_ics(_ics_with_rrule("UNTIL=20261201T100000;FREQ=WEEKLY"))

    starts = [c.occurred_at for c in parsed.chunks]
    assert starts, "the paste is never lost"
    # Sundays from 1 Nov through 29 Nov are <= 1 Dec 10:00 JLM (08:00Z);
    # 6 Dec is out
    assert len(starts) == 5
    assert starts[-1] == datetime(2026, 11, 29, 8, 0, tzinfo=UTC)
    assert datetime(2026, 12, 6, 8, 0, tzinfo=UTC) not in starts


def test_zoneless_timed_rrule_keeps_the_wall_time_across_dst():
    """Same anchor rule as TZID: a zone-less timed DTSTART is interpreted in
    the app zone and the series keeps its wall time across the 25 Oct DST
    change (a UTC-fixed anchor would drift 16:00 -> 17:00)."""
    parsed = _parse_ics(
        "BEGIN:VEVENT\n"
        "UID:zoneless@example.com\n"
        "SUMMARY:כדורגל איתמר\n"
        "DTSTART:20261021T160000\n"
        "DTEND:20261021T171500\n"
        "RRULE:FREQ=WEEKLY\n"
        "END:VEVENT",
        now=datetime(2026, 10, 21, 6, 0, tzinfo=UTC),
    )

    assert [c.occurred_at for c in parsed.chunks][:2] == [
        datetime(2026, 10, 21, 13, 0, tzinfo=UTC),  # 16:00 IDT (UTC+3)
        datetime(2026, 10, 28, 14, 0, tzinfo=UTC),  # 16:00 IST (UTC+2)
    ]
    assert all("16:00" in c.raw_text for c in parsed.chunks[:2])


def test_all_day_weekly_rrule_renders_one_date_only_item_per_occurrence():
    """DTSTART;VALUE=DATE + RRULE weekly: one item per occurrence, each
    rendered as an all-day one-liner (day, no times), dates correct across
    the 25 Oct 2026 DST change (a midnight anchor keeps its wall date)."""
    parsed = _parse_ics(
        "BEGIN:VEVENT\n"
        "UID:allday-series@example.com\n"
        "SUMMARY:צופים רונה\n"
        "DTSTART;VALUE=DATE:20261018\n"
        "RRULE:FREQ=WEEKLY\n"
        "END:VEVENT"
    )

    # Sundays 25 Oct .. 20 Dec (in-window); each at LOCAL midnight, so the
    # UTC instant shifts an hour at the 25 Oct DST change while the wall
    # date stays the calendar Sunday
    assert [c.occurred_at for c in parsed.chunks] == [
        datetime(2026, 10, 24, 21, 0, tzinfo=UTC),  # 25 Oct 00:00 IDT (UTC+3)
        datetime(2026, 10, 31, 22, 0, tzinfo=UTC),  # 1 Nov 00:00 IST (UTC+2)
        datetime(2026, 11, 7, 22, 0, tzinfo=UTC),
        datetime(2026, 11, 14, 22, 0, tzinfo=UTC),
        datetime(2026, 11, 21, 22, 0, tzinfo=UTC),
        datetime(2026, 11, 28, 22, 0, tzinfo=UTC),
        datetime(2026, 12, 5, 22, 0, tzinfo=UTC),
        datetime(2026, 12, 12, 22, 0, tzinfo=UTC),
        datetime(2026, 12, 19, 22, 0, tzinfo=UTC),
    ]
    assert parsed.chunks[0].raw_text == "צופים רונה — Sun 25 Oct"
    assert parsed.chunks[1].raw_text == "צופים רונה — Sun 1 Nov"
    assert all("–" not in c.raw_text for c in parsed.chunks), "no times on all-day items"
    assert all(c.meta["series_uid"] == "allday-series@example.com" for c in parsed.chunks)


# ---- persistence: idempotent re-paste + the real search path ---------------------


@pytest.fixture(name="fresh_service")
def fresh_service_fixture() -> Iterator[tuple[IngestService, ItemsRepository, Any]]:
    """A clean-slate ingest service; the session is owned HERE and closed with
    the test (house convention, cf. tests/services/test_memory.py).

    Closing is not optional: an un-closed session keeps its pooled connection
    checked out with the last SELECT's transaction still open (idle in
    transaction), and those locks deadlock the migration DDL that
    tests/test_datetime_serialization.py runs later in the full suite — the
    documented flaky hang that its ``engine.dispose()`` hygiene cannot fix
    (dispose never closes a still-checked-out connection).
    """
    assert_test_database(str(engine.url))
    session = SessionLocal()
    try:
        session.execute(text("DELETE FROM memory_items"))
        session.commit()
        repo = ItemsRepository(session)
        store = QdrantVectorStore(client=QdrantClient(":memory:"))
        indexer = QdrantItemIndexer(FakeEmbedder(dim=8), store)
        service = IngestService(repo, StubClassifier(FULL_CLASSIFICATION), indexer)
        yield service, repo, session
    finally:
        session.rollback()
        session.close()


def _item_count(session) -> int:
    return int(session.execute(select(func.count()).select_from(MemoryItem)).scalar_one())


def test_repaste_of_the_same_series_creates_zero_new_items(fresh_service):
    service, repo, session = fresh_service
    parsed = _parse_ics(_WEDNESDAY_SERIES)

    first = service.capture_parsed(parsed)
    assert len(first) == 9  # DTSTART (21 Oct 13:00Z, after the paste) .. 16 Dec

    second = service.capture_parsed(_parse_ics(_WEDNESDAY_SERIES))
    assert len(second) == 9, "every occurrence dedupes to its existing item"
    assert _item_count(session) == 9


def test_temporal_question_finds_the_occurrence_through_the_real_search_path(fresh_service):
    service, repo, session = fresh_service
    service.capture_parsed(_parse_ics(_WEDNESDAY_SERIES))

    # the ask path's temporal leg: resolve the question's ranges, then fetch
    # every item occurring inside them (ask._temporal_hits -> by_occurrence)
    ranges = parse_question_ranges(
        "מה יום רביעי 4.11?", JERUSALEM, datetime(2026, 10, 21, 12, 0, tzinfo=UTC)
    )
    assert ranges, "the question must resolve to a date range"
    hits = [
        item for start, end in ranges for item in repo.by_occurrence(start=start, end=end, limit=30)
    ]
    assert any("כדורגל איתמר" in item.raw_text for item in hits), (
        "the Wednesday 4.11 occurrence must be found by the temporal path"
    )
