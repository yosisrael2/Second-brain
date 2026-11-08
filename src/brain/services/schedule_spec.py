"""Schedule specs for scheduled tasks: 5-field cron in the task's timezone.

Format: the standard ``minute hour day-of-month month day-of-week`` cron
string (``*``, ``a-b``, ``*/n``, ``a-b/n``, ``a,b,c``; month names
``jan``..``dec``; weekday names ``sun``..``sat``, 0 and 7 are both Sunday;
the macros ``@hourly`` ``@daily`` ``@weekly`` ``@monthly`` ``@yearly``).
When both day fields are restricted a day matches either one (Vixie cron
semantics). Times are WALL-CLOCK times in the task's IANA timezone.

DST (tested around Israel's March/October changes):
- a wall time skipped by a spring-forward gap (02:30 on the change night)
  fires once, at the equivalent instant after the gap (03:30) — never
  skipped; when that instant is also a scheduled time it fires once.
- a wall time repeated by a fall-back (01:30 twice) fires on its FIRST
  occurrence only — never twice.

No third-party cron library: the evaluator is ~100 lines, fully tested,
and owns the DST policy above explicitly.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from brain import i18n

DEFAULT_TIMEZONE = "Asia/Jerusalem"
# A task runs the LLM: refuse schedules that could fire more often than this.
MIN_INTERVAL_MINUTES = 15
# Search horizon for the next fire (covers Feb 29 + a restricted weekday).
_HORIZON_DAYS = 366 * 8 + 2
SCHEDULE_MAX_LENGTH = 120

_MACROS = {
    "@hourly": "0 * * * *",
    "@daily": "0 0 * * *",
    "@midnight": "0 0 * * *",
    "@weekly": "0 0 * * 0",
    "@monthly": "0 0 1 * *",
    "@yearly": "0 0 1 1 *",
    "@annually": "0 0 1 1 *",
}
_MONTHS = {
    name: n
    for n, name in enumerate(
        ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"),
        start=1,
    )
}
_DAYS = {name: n for n, name in enumerate(("sun", "mon", "tue", "wed", "thu", "fri", "sat"))}
# (name, low, high, names)
_FIELDS: tuple[tuple[str, int, int, dict[str, int]], ...] = (
    ("minute", 0, 59, {}),
    ("hour", 0, 23, {}),
    ("day of month", 1, 31, {}),
    ("month", 1, 12, _MONTHS),
    ("day of week", 0, 7, _DAYS),
)


class ScheduleError(ValueError):
    """An invalid schedule or timezone; the message is shown to the user."""


@dataclass(frozen=True)
class CronSpec:
    """A parsed cron expression (weekdays 0=Sunday..6=Saturday)."""

    expr: str
    minutes: tuple[int, ...]
    hours: tuple[int, ...]
    days: frozenset[int]
    months: frozenset[int]
    weekdays: frozenset[int]
    dom_any: bool
    dow_any: bool

    def matches_day(self, day: date) -> bool:
        if day.month not in self.months:
            return False
        dom_ok = day.day in self.days
        dow_ok = day.isoweekday() % 7 in self.weekdays
        if self.dom_any and self.dow_any:
            return True
        if self.dom_any:
            return dow_ok
        if self.dow_any:
            return dom_ok
        return dom_ok or dow_ok


def _value(token: str, field: str, low: int, high: int, names: dict[str, int]) -> int:
    lowered = token.lower()
    if lowered in names:
        return names[lowered]
    if not (token.isascii() and token.isdigit()):  # "²".isdigit() is True
        raise ScheduleError(f"invalid {field} value '{token}'")
    value = int(token)
    if not low <= value <= high:
        raise ScheduleError(f"{field} value {value} is out of range {low}-{high}")
    return value


def _parse_field(text: str, index: int) -> tuple[set[int], bool]:
    """(values, is_wildcard) for one cron field."""
    field, low, high, names = _FIELDS[index]
    values: set[int] = set()
    wildcard = text == "*"
    for part in text.split(","):
        if not part:
            raise ScheduleError(f"empty entry in the {field} field")
        base, slash, step_text = part.partition("/")
        step = 1
        if slash:
            if not (step_text.isascii() and step_text.isdigit()) or int(step_text) < 1:
                raise ScheduleError(f"invalid step '{step_text}' in the {field} field")
            step = int(step_text)
        if base == "*":
            start, end = low, high
        elif "-" in base:
            first, _, last = base.partition("-")
            start = _value(first, field, low, high, names)
            end = _value(last, field, low, high, names)
            if start > end:
                raise ScheduleError(f"range {base} is backwards in the {field} field")
        else:
            start = _value(base, field, low, high, names)
            end = high if slash else start
        values.update(range(start, end + 1, step))
    return values, wildcard


def _min_gap_minutes(minutes: tuple[int, ...]) -> int:
    """Smallest gap between consecutive minute marks (wrapping the hour)."""
    if len(minutes) == 1:
        return 60
    gaps = [b - a for a, b in zip(minutes, minutes[1:], strict=False)]
    gaps.append(60 - minutes[-1] + minutes[0])
    return min(gaps)


def parse_cron(expr: str) -> CronSpec:
    """Parse a 5-field cron string (raises ScheduleError); macros expand.

    Syntax only — :func:`validate` also proves the schedule ever fires.
    """
    text = " ".join((expr or "").split())
    if not text:
        raise ScheduleError("the schedule is empty")
    if len(text) > SCHEDULE_MAX_LENGTH:
        raise ScheduleError(f"the schedule is longer than {SCHEDULE_MAX_LENGTH} characters")
    text = _MACROS.get(text.lower(), text)
    parts = text.split(" ")
    if len(parts) != 5:
        raise ScheduleError(
            "a schedule needs 5 fields: minute hour day-of-month month day-of-week "
            f"(got {len(parts)})"
        )
    fields = [_parse_field(part, n) for n, part in enumerate(parts)]
    minutes = tuple(sorted(fields[0][0]))
    weekdays = {0 if day == 7 else day for day in fields[4][0]}
    if _min_gap_minutes(minutes) < MIN_INTERVAL_MINUTES:
        raise ScheduleError(f"runs too often: at most once every {MIN_INTERVAL_MINUTES} minutes")
    spec = CronSpec(
        expr=text,
        minutes=minutes,
        hours=tuple(sorted(fields[1][0])),
        days=frozenset(fields[2][0]),
        months=frozenset(fields[3][0]),
        weekdays=frozenset(weekdays),
        dom_any=fields[2][1],
        dow_any=fields[4][1],
    )
    return spec


def get_zone(name: str) -> ZoneInfo:
    """The IANA zone (raises ScheduleError for an unknown name)."""
    try:
        return ZoneInfo((name or "").strip())
    except (ZoneInfoNotFoundError, ValueError):
        raise ScheduleError(f"unknown timezone '{name}'") from None


def next_fire(spec: CronSpec, tz: ZoneInfo, after: datetime) -> datetime:
    """The first fire STRICTLY after ``after``, as an aware UTC datetime.

    Wall times are resolved with fold=0: a gap time maps forward past the
    gap, a repeated time resolves to its first occurrence (see module doc).
    Within one day the earliest instant wins (a gap-mapped time can land
    after a later wall time).
    """
    if after.tzinfo is None:
        raise ValueError("after must be timezone-aware")
    after_utc = after.astimezone(UTC)
    day = after_utc.astimezone(tz).date()
    for _ in range(_HORIZON_DAYS):
        if spec.matches_day(day):
            best: datetime | None = None
            for hour in spec.hours:
                for minute in spec.minutes:
                    wall = datetime(day.year, day.month, day.day, hour, minute, tzinfo=tz)
                    instant = wall.astimezone(UTC)
                    if instant > after_utc and (best is None or instant < best):
                        best = instant
            if best is not None:
                return best
        day += timedelta(days=1)
    raise ScheduleError("this schedule never fires (no such date)")


def validate(expr: str, timezone: str) -> tuple[CronSpec, ZoneInfo]:
    """Parse both and prove the schedule fires at least once."""
    spec = parse_cron(expr)
    tz = get_zone(timezone)
    next_fire(spec, tz, datetime.now(tz=UTC))
    return spec, tz


# --- in words -------------------------------------------------------------------

_DAY_NAMES = {
    "en": ("Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday"),
    "he": ("ראשון", "שני", "שלישי", "רביעי", "חמישי", "שישי", "שבת"),
}


def describe(expr: str, lang: str | None = i18n.DEFAULT) -> str:
    """The schedule in words ('Every Friday at 07:00'); cron text otherwise."""
    code = i18n.normalize(lang)
    try:
        spec = parse_cron(expr)
    except ScheduleError:
        return expr
    all_months = len(spec.months) == 12
    single_time = len(spec.minutes) == 1 and len(spec.hours) == 1
    time_text = f"{spec.hours[0]:02d}:{spec.minutes[0]:02d}" if single_time else ""
    if all_months and spec.dom_any and spec.dow_any:
        if single_time:
            return i18n.t(code, "sched_daily", time=time_text)
        if len(spec.minutes) == 1 and len(spec.hours) == 24:
            return i18n.t(code, "sched_hourly", minute=f"{spec.minutes[0]:02d}")
    if all_months and spec.dom_any and not spec.dow_any and single_time:
        days = sorted(spec.weekdays)  # Sunday first (the Israeli week)
        if days == [1, 2, 3, 4, 5]:
            return i18n.t(code, "sched_weekdays", time=time_text)
        if days == [0, 1, 2, 3, 4]:
            return i18n.t(code, "sched_sun_thu", time=time_text)
        names = ", ".join(_DAY_NAMES[code][d] for d in days)
        key = "sched_weekly_one" if len(days) == 1 else "sched_weekly"
        return i18n.t(code, key, days=names, time=time_text)
    if all_months and spec.dow_any and not spec.dom_any and single_time and len(spec.days) == 1:
        return i18n.t(code, "sched_monthly", day=next(iter(spec.days)), time=time_text)
    return i18n.t(code, "sched_cron", expr=spec.expr)
