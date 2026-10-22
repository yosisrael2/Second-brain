"""Deterministic date resolution for extracted children (brief 3b, 3c).

Sources express dates three ways: Hebrew-calendar ("ז' בחשוון"), numeric
("22.10", "15.10.26") and relative-to-sent ("this Thursday", "מחר"). All are
resolved DETERMINISTICALLY — never by the LLM (delta item 2a) — against the
SOURCE's sent time, not today (item 41). A weekday named in the text that
disagrees with the resolved date raises a conflict flag (item 2b): both
readings are kept, the child is never silently fixed.

Hebrew calendar conversion uses pyluach (delta item 2a); weekday/numeric/
relative parsing reuses the retrieval module's battle-tested rules (same
package — PRs #20/#21 hardened them).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from brain.services import temporal
from brain.services.textnorm import normalize

# Hebrew month names (incl. the mar- prefix and both Adars). Numbers are
# pyluach's month order: 1=Nisan ... 7=Tishri ... 12=Adar I-ish (see below).
_HE_MONTHS: dict[str, int] = {
    "ניסן": 1, "אייר": 2, "סיון": 3, "תמוז": 4, "אב": 5, "אלול": 6,
    "תשרי": 7, "חשוון": 8, "מרחשוון": 8, "כסלו": 9, "טבת": 10, "שבט": 11,
    "אדר": 12,  # in a leap year pyluach calls the first one Adar I
}
# In leap years the month AFTER Shevat is Adar I (13) and "אדר" means Adar II.
_ADAR_I, _ADAR_II = 13, 14  # pyluach leap-year numbering: 13=Adar I, 14=Adar II

_HE_MONTH_RE = re.compile(
    r"(?:^|[^\u05d0-\u05ea])"
    r"([\u05d0-\u05ea\u05f4\u05f3\"'\d]{1,6}?)"  # day token, gershayim-tolerant
    r"[\u05f4\u05f3\"']?\s*\u05d1(?:\u05d4)?"
    r"(" + "|".join(sorted(_HE_MONTHS, key=len, reverse=True)) + r")(?![\u05d0-\u05ea])"
)
# Date-ish expressions: numeric dates (DD.MM[.YY]) OR Hebrew month NAMES as
# words (with the ב prefix allowed). The char-level fallback is gone: a
# character class over month-name letters matches EVERY Hebrew word.
DATEISH_RE = re.compile(
    r"\d{1,2}[./]\d{1,2}(?:[./]\d{2,4})?"
    r"|(?<![\u05d0-\u05ea])\u05d1?(?:"
    + "|".join(sorted(_HE_MONTHS, key=len, reverse=True))
    + r")(?![\u05d0-\u05ea])"
)

_HE_ONES = {"א": 1, "ב": 2, "ג": 3, "ד": 4, "ה": 5, "ו": 6, "ז": 7, "ח": 8, "ט": 9}
_HE_TENS = {"י": 10, "כ": 20, "ל": 30}


@dataclass(frozen=True)
class ResolvedWhen:
    """A resolved date (+optional clock time) with provenance and conflicts."""

    day: date
    when_raw: str
    kind: str  # 'hebrew' | 'numeric' | 'relative' | 'weekday'
    # Weekday named in the text (0=Mon..6=Sun) when one is present.
    named_weekday: int | None = None
    # Both readings of a weekday-vs-date disagreement (item 2b).
    conflict_note: str | None = None
    matched_times: tuple[time, ...] = field(default_factory=tuple)

    @property
    def date_conflict(self) -> bool:
        return self.conflict_note is not None


def _hebrew_numeral(text: str) -> int | None:
    """ז / ז' / ט\"ו / כה / 7 -> 7 / 7 / 15 / 25 / 7. None otherwise."""
    cleaned = normalize(text).replace(" ", "")
    cleaned = re.sub(r"['\u05f3\"\u05f4]", "", cleaned)  # geresh/gershayim
    if cleaned.isdigit():
        return int(cleaned)
    # Hebrew numerals are ADDITIVE: ט"ו = ט(9)+ו(6) = 15, כה = 20+5 = 25,
    # יז = 10+7 = 17 (15/16 are never י"ה/י"ו — that's why the additive read
    # is safe for every day-of-month form).
    letters = [ch for ch in cleaned if ch in _HE_ONES or ch in _HE_TENS]
    if not letters or len(letters) != len(cleaned):
        return None
    return sum(_HE_ONES.get(ch) or _HE_TENS.get(ch) or 0 for ch in letters)


def resolve_hebrew_date(text: str, after: date) -> tuple[date, int | None] | None:
    """'ז\\' בחשוון' / 'ט\"ו בשבט' / 'בחשוון' -> the first occurrence after ``after``.

    Returns (gregorian_date, hebrew_month_number) or None when the text holds
    no Hebrew-calendar date. Year inference: the Hebrew year containing
    ``after``; if the date already passed, the NEXT Hebrew year (an email sent
    in December about ט\"ו בשבט means the upcoming February).
    """
    import pyluach.dates  # noqa: PLC0415
    import pyluach.hebrewcal  # noqa: PLC0415

    for match in _HE_MONTH_RE.finditer(text):
        day = _hebrew_numeral(match.group(1)) if match.group(1) else 1
        month_name = match.group(2)
        month = _HE_MONTHS[month_name]
        if day is None or not 1 <= day <= 30:
            continue
        hyear = pyluach.dates.HebrewDate.from_pydate(after).year
        for candidate_year in (hyear, hyear + 1):
            resolved_month = month
            if month == 12:  # אדר: in a leap year plain אדר = Adar II (שני)
                resolved_month = (
                    _ADAR_II if pyluach.hebrewcal.Year(candidate_year).leap else 12
                )
            try:
                hd = pyluach.dates.HebrewDate(candidate_year, resolved_month, day)
            except ValueError:
                continue
            greg = hd.to_greg().to_pydate()
            if greg >= after:
                return (greg, resolved_month)
        # every occurrence of this month/day is in the past: keep scanning
    return None


def resolve_when(
    when_raw: str, sent_at_local: datetime, tz: ZoneInfo
) -> ResolvedWhen | None:
    """Resolve one raw date expression deterministically (brief 3b/3c).

    ``sent_at_local`` is the source's sent time ALREADY converted to the
    user's timezone (item 41: never a fixed offset — DST matters). Order:
    Hebrew calendar > numeric/explicit > relative ("this Thursday").
    """
    text = when_raw.strip()
    if not text:
        return None
    local_date = sent_at_local.date()

    hebrew = resolve_hebrew_date(text, after=local_date)
    if hebrew is not None:
        day, _hebrew_month = hebrew
        named = temporal._find_weekday(text.lower())
        return _with_conflict(ResolvedWhen(day=day, when_raw=text, kind="hebrew"), named)

    year_hint = local_date.year
    explicit = temporal._find_explicit_date(text, year_hint)
    if explicit is not None:
        if explicit < local_date - timedelta(days=365):
            explicit = explicit.replace(year=year_hint + 1)
        named = temporal._find_weekday(text.lower())
        return _with_conflict(ResolvedWhen(day=explicit, when_raw=text, kind="numeric"), named)

    relative = temporal._find_relative_day(text.lower(), sent_at_local)
    kind = "relative"
    if relative is None:
        weekday = temporal._find_weekday(text.lower())
        if weekday is not None:
            offset = (weekday - local_date.weekday()) % 7
            relative = local_date + timedelta(days=offset)
            kind = "weekday"
            return _with_conflict(
                ResolvedWhen(day=relative, when_raw=text, kind=kind, named_weekday=weekday),
                weekday,
            )
    if relative is not None:
        return ResolvedWhen(day=relative, when_raw=text, kind=kind)
    return None


def _with_conflict(resolved: ResolvedWhen, named_weekday: int | None) -> ResolvedWhen:
    """Flag weekday-vs-date disagreement instead of silently picking (item 2b)."""
    if named_weekday is None or named_weekday == resolved.day.weekday():
        return resolved
    named_name = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"][
        named_weekday
    ]
    actual_name = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"][
        resolved.day.weekday()
    ]
    return ResolvedWhen(
        day=resolved.day,
        when_raw=resolved.when_raw,
        kind=resolved.kind,
        named_weekday=named_weekday,
        conflict_note=(
            f"text says {named_name}, {resolved.day.isoformat()} is a {actual_name}"
        ),
    )


def combine_local(day: date, clock: time | None, tz: ZoneInfo) -> datetime:
    """A local wall-clock date(+time) -> aware UTC datetime (item 41).

    Constructing the datetime IN ``tz`` (not with a fixed offset) lets
    ZoneInfo pick the right UTC offset per date — 13:00 on Sun 25.10.2026 is
    11:00Z (IST), 13:00 on Thu 22.10 is 10:00Z (IDT). All-day events get the
    local midnight-to-midnight span folded into ``start`` only; end spans are
    the runner's business.
    """
    local = datetime.combine(day, clock or time(0, 0), tzinfo=tz)
    return local.astimezone(UTC)


def extract_clock(when_raw: str) -> time | None:
    """'13:35' / '9:40' -> time; None otherwise.

    COLON only: the dotted form ('13.35') collides with day-first numeric
    dates ('22.10' must never become 22:10), which dominate Hebrew text.
    """
    match = re.search(r"(?<!\d)([01]?\d|2[0-3]):([0-5]\d)(?!\d)", when_raw)
    if match is None:
        return None
    return time(int(match.group(1)), int(match.group(2)))


def utc_now() -> datetime:
    return datetime.now(tz=UTC)
