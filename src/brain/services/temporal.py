"""Temporal retrieval: resolve date expressions in questions to UTC ranges.

"Why can't top-5 semantic similarity answer 'What am I doing on Friday
evening?'" — because no event text contains "Friday evening". This module
turns day/date expressions (English + Hebrew) into [start, end) ranges in
the user's timezone; the ask path fetches every item occurring inside them
and merges those into the context ahead of the semantic hits.

Deterministic rules only (no LLM): the supported expressions are a small
closed set — today/tomorrow/yesterday (היום/מחר/אתמול), weekday names
(English + Hebrew), explicit "October 27" / "27 October" dates, "this week"
(השבוע), and dayparts (morning/afternoon/evening + Hebrew). Anything else
parses to no range and retrieval falls back to semantic-only, exactly as
before.
"""

from __future__ import annotations

import re
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

# Daypart windows, local time (inclusive start, exclusive end).
_DAYPARTS: dict[str, tuple[time, time]] = {
    "morning": (time(6, 0), time(12, 0)),
    "afternoon": (time(12, 0), time(17, 0)),
    "evening": (time(17, 0), time(23, 59, 59, 999999)),
    "night": (time(20, 0), time(23, 59, 59, 999999)),
}

_WEEKDAYS_EN = {
    "monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3,
    "friday": 4, "saturday": 5, "sunday": 6,
}
# Hebrew day names and the common bare forms ("שישי", "ראשון", ...).
_WEEKDAYS_HE = {
    "יום ראשון": 6, "יום שני": 0, "יום שלישי": 1, "יום רביעי": 2,
    "יום חמישי": 3, "יום שישי": 4, "יום שבת": 5,
    "ראשון": 6, "שני": 0, "שלישי": 1, "רביעי": 2, "חמישי": 3,
    "שישי": 4, "שבת": 5,
}
_MONTHS_EN = {
    "january": 1, "february": 2, "march": 3, "april": 4, "may": 5,
    "june": 6, "july": 7, "august": 8, "september": 9, "october": 10,
    "november": 11, "december": 12,
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "jun": 6, "jul": 7,
    "aug": 8, "sep": 9, "sept": 9, "oct": 10, "nov": 11, "dec": 12,
}

_MONTH_PATTERN = "|".join(_MONTHS_EN)
# Longest-first so "יום שישי" wins over a bare "שישי" substring check.
_HE_DAY_NAMES = sorted(_WEEKDAYS_HE, key=len, reverse=True)

# Numeric dates: DD.MM, DD/MM, DD.MM.YY(YY) — day-first (Israeli usage):
# 'ב-22.10', '19/10', '15.10.26', '15.10.2026'. Boundaries reject matches
# inside longer numbers ("2026.10" -> only "26.10" would match) and partial
# tokens ("15.103"); the chain checks in _find_numeric_date reject dotted
# runs that are not dates ("version 1.2.3" must not parse as a day).
_NUMERIC_DATE = re.compile(r"(?<!\d)(\d{1,2})[./](\d{1,2})(?:[./](\d{2,4}))?(?!\d)")


def parse_question_ranges(
    question: str, tz: ZoneInfo, now: datetime
) -> list[tuple[datetime, datetime]]:
    """Resolve a question's date expressions into [start, end) aware-UTC ranges.

    ``now`` must be timezone-aware; ranges are computed in ``tz``. Later,
    more specific expressions win: a bare weekday defers to an explicit
    "Month Day" in the same question.
    """
    if not question:
        return []
    text = question.lower()
    local_now = now.astimezone(tz)

    daypart = _find_daypart(text)
    window = _find_day_window(text, local_now)

    if window is not None:
        start_day, end_day = window
        start_local = datetime.combine(start_day, time(0, 0), tzinfo=tz)
        end_local = datetime.combine(end_day, time(0, 0), tzinfo=tz)
        if daypart is not None:
            start_local = max(start_local, datetime.combine(start_day, daypart[0], tzinfo=tz))
            end_local = min(end_local, datetime.combine(start_day, daypart[1], tzinfo=tz))
        if start_local >= end_local:
            return []
        return [_to_utc_pair(start_local, end_local)]
    if daypart is not None:  # "in the evening" with no day -> today
        start_local = datetime.combine(local_now.date(), daypart[0], tzinfo=tz)
        end_local = datetime.combine(local_now.date(), daypart[1], tzinfo=tz)
        return [_to_utc_pair(start_local, end_local)]
    return []


def _find_daypart(text: str) -> tuple[time, time] | None:
    """Daypart windows come from EXPLICIT expressions only.

    Hebrew takes the attached-preposition forms (בערב, בבוקר, בלילה,
    אחר הצהריים): a bare "ערב" or "צהריים" is common inside event names
    ("ארוחת צהריים" = lunch) and must not turn the question into a
    time-of-day query.
    """
    if "evening" in text or "בערב" in text:
        return _DAYPARTS["evening"]
    if "morning" in text or "בבוקר" in text or "לפנות בוקר" in text:
        return _DAYPARTS["morning"]
    if "afternoon" in text or "אחר הצהריים" in text or "בצהריים" in text:
        return _DAYPARTS["afternoon"]
    if "night" in text or "בלילה" in text:
        return _DAYPARTS["night"]
    return None


def _find_day_window(text: str, local_now: datetime) -> tuple[date, date] | None:
    """A [start_day, end_day) window of local dates, or None."""
    explicit = _find_explicit_date(text, local_now.year)
    if explicit is not None:
        return (explicit, explicit + timedelta(days=1))
    if "this week" in text or "השבוע" in text:
        week_start = local_now.date() - timedelta(days=(local_now.weekday() + 1) % 7)
        week_end = max(week_start + timedelta(days=7), local_now.date() + timedelta(days=7))
        return (week_start, week_end)
    relative = _find_relative_day(text, local_now)
    if relative is not None:
        return (relative, relative + timedelta(days=1))
    weekday = _find_weekday(text)
    if weekday is not None:
        offset = (weekday - local_now.weekday()) % 7  # today counts as the match
        target = local_now.date() + timedelta(days=offset)
        return (target, target + timedelta(days=1))
    return None


def _find_explicit_date(text: str, year_hint: int) -> date | None:
    """'October 27' / 'Oct 27' / '27 October' -> a date near ``year_hint``."""
    month_name = rf"({_MONTH_PATTERN})"
    for pattern in (
        rf"{month_name}\s+(\d{{1,2}})(?:st|nd|rd|th)?",
        rf"(\d{{1,2}})(?:st|nd|rd|th)?\s+{month_name}",
    ):
        match = re.search(pattern, text)
        if not match:
            continue
        first, second = match.group(1), match.group(2)
        if first.isdigit():
            day, month = int(first), _MONTHS_EN[second.strip()]
        else:
            month, day = _MONTHS_EN[first.strip()], int(second)
        # Prefer ``year_hint``; roll to the next year only when the date is
        # invalid in it (Feb 29). We never skip ahead on a valid date —
        # school-year questions legitimately ask about earlier terms.
        return _first_valid_date(year_hint, month, day)
    numeric = _find_numeric_date(text, year_hint)
    if numeric is not None:
        return numeric
    return None


def _first_valid_date(year_hint: int, month: int, day: int) -> date | None:
    for year in (year_hint, year_hint + 1):
        try:
            return date(year, month, day)
        except ValueError:
            continue
    return None


def _find_numeric_date(text: str, year_hint: int) -> date | None:
    """DD.MM, DD/MM, DD.MM.YY(YY) — day-first, Israel-style.

    Known accepted ambiguity: HH.MM times with MM <= 12 ('8.10', '12.55'
    would not, MM>12 fails) parse as dates. Temporal hits only ADD
    context ahead of the semantic hits, so a spurious day range costs a
    little recall precision but never removes an answer.
    """
    for match in _NUMERIC_DATE.finditer(text):
        before = text[match.start() - 1] if match.start() else ""
        after = text[match.end() : match.end() + 2]
        chain_continues = len(after) == 2 and after[0] in "./" and after[1].isdigit()
        if before in (".", "/") or chain_continues:
            continue  # inside a dotted run: version number, not a date
        parsed = _numeric_date_from(match, year_hint)
        if parsed is not None:
            return parsed
        # syntactic match but not a valid date (e.g. '12.55') — keep looking
    return None


def _numeric_date_from(match: re.Match[str], year_hint: int) -> date | None:
    day, month = int(match.group(1)), int(match.group(2))
    if not 1 <= month <= 12 or not 1 <= day <= 31:
        return None
    if match.group(3):
        short_year = int(match.group(3))
        year = 2000 + short_year if short_year < 100 else short_year
        try:
            return date(year, month, day)
        except ValueError:
            return None
    return _first_valid_date(year_hint, month, day)


def _find_relative_day(text: str, local_now: datetime) -> date | None:
    if "today" in text or "היום" in text:
        return local_now.date()
    if "tomorrow" in text or "מחר" in text:
        return local_now.date() + timedelta(days=1)
    if "yesterday" in text or "אתמול" in text:
        return local_now.date() - timedelta(days=1)
    return None


def _find_weekday(text: str) -> int | None:
    # A Hebrew day name needs a left boundary too: bare 'שני'/'ראשון'/'שבת'
    # are ordinary words ("התשלום השני" = the second payment). Valid when the
    # name starts the text, follows whitespace, or carries a ב/ו/ל preposition
    # ('בשני', 'ושבת', 'לשישי'); invalid right after ה (the ordinal form).
    for name in _HE_DAY_NAMES:
        if re.search(rf"(?:^|\s)(?:[בול])?{name}(?![א-ת])", text):
            return _WEEKDAYS_HE[name]
    for name, index in _WEEKDAYS_EN.items():
        if re.search(rf"\b{name}\b", text):
            return index
    return None


def _to_utc_pair(start_local: datetime, end_local: datetime) -> tuple[datetime, datetime]:
    return (start_local.astimezone(UTC), end_local.astimezone(UTC))
