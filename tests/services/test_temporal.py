"""Temporal retrieval: date expressions in questions -> [start, end) ranges."""

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from brain.services.temporal import parse_question_ranges

JLM = ZoneInfo("Asia/Jerusalem")
# Fixed 'now': Thursday 2026-10-22 18:30 Israel (IDT, +03:00) — the dogfood instant.
NOW = datetime(2026, 10, 22, 18, 30, tzinfo=JLM)


def _fmt(span: tuple[datetime, datetime]) -> str:
    start, end = (instant.astimezone(JLM) for instant in span)
    return f"{start:%a %d %b %H:%M}..{end:%a %d %b %H:%M}"


def _ranges(question: str, now: datetime = NOW) -> list[str]:
    return [_fmt(span) for span in parse_question_ranges(question, JLM, now)]


def test_explicit_month_day_resolves_to_one_local_day() -> None:
    assert _ranges("What do the kids have on Tuesday October 27?") == [
        "Tue 27 Oct 00:00..Wed 28 Oct 00:00"
    ]


def test_reversed_month_day_form_also_parses() -> None:
    assert _ranges("meeting on 27 October?") == ["Tue 27 Oct 00:00..Wed 28 Oct 00:00"]


def test_evening_narrows_the_day_to_17_to_midnight() -> None:
    assert _ranges("What am I doing on Friday evening?") == [
        "Fri 23 Oct 17:00..Fri 23 Oct 23:59"
    ]


def test_explicit_date_with_evening() -> None:
    assert _ranges("What is happening on Monday evening, October 26?") == [
        "Mon 26 Oct 17:00..Mon 26 Oct 23:59"
    ]


def test_weekday_without_date_is_the_next_occurrence() -> None:
    # now = Thu 22 Oct; the next Tuesday is the 27th
    assert _ranges("What's on Tuesday?") == ["Tue 27 Oct 00:00..Wed 28 Oct 00:00"]


def test_today_counts_as_the_weekday_match() -> None:
    assert _ranges("what is on thursday?", now=NOW) == ["Thu 22 Oct 00:00..Fri 23 Oct 00:00"]


def test_this_week_spans_the_israeli_week_and_reaches_a_week_ahead() -> None:
    # Israeli week starts Sunday; now (Thu 22) is late in the week, so the
    # window always reaches 7 days ahead to include upcoming days.
    assert _ranges("How many times does Itamar have football this week?") == [
        "Sun 18 Oct 00:00..Thu 29 Oct 00:00"
    ]


def test_today_tomorrow_yesterday_english_and_hebrew() -> None:
    assert _ranges("מה יש היום?") == ["Thu 22 Oct 00:00..Fri 23 Oct 00:00"]
    assert _ranges("what about tomorrow?") == ["Fri 23 Oct 00:00..Sat 24 Oct 00:00"]
    assert _ranges("ואתמול?") == ["Wed 21 Oct 00:00..Thu 22 Oct 00:00"]


def test_hebrew_weekday_names_resolve() -> None:
    assert _ranges("מה מתוכנן ביום שישי?") == ["Fri 23 Oct 00:00..Sat 24 Oct 00:00"]
    assert _ranges("מה קורה בשבת?") == ["Sat 24 Oct 00:00..Sun 25 Oct 00:00"]


def test_daypart_without_a_day_means_today() -> None:
    assert _ranges("מה קורה בערב?") == ["Thu 22 Oct 17:00..Thu 22 Oct 23:59"]


def test_lunch_in_hebrew_is_not_an_afternoon_query() -> None:
    """'ארוחת צהריים' (lunch) must not turn the question into an afternoon range."""
    assert _ranges("מתי ארוחת הצהריים עם אור?") == []


def test_no_date_expression_yields_no_range() -> None:
    assert _ranges("Who picks Itamar up from basketball?") == []


def test_dst_boundary_is_honored_in_the_range_edges() -> None:
    # Oct 24 is IDT (+3), Oct 26 is IST (+2): the local-midnight boundaries
    # must map to different UTC instants across the change.
    assert _ranges("what's happening on Saturday October 24?") == [
        "Sat 24 Oct 00:00..Sun 25 Oct 00:00"
    ]
    assert _ranges("what's happening on Monday October 26?") == [
        "Mon 26 Oct 00:00..Tue 27 Oct 00:00"
    ]
    sunday = parse_question_ranges("on Sunday October 25?", JLM, NOW)[0]
    assert sunday[0].utcoffset() == UTC.utcoffset(None)  # ranges are aware UTC
    assert sunday[0].astimezone(JLM).utcoffset().total_seconds() == 3 * 3600  # IDT
