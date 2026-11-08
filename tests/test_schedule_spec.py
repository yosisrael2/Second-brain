"""brain.services.schedule_spec: cron parsing, next fire in a timezone, DST, words.

Israel's 2026 changes (asserted below, not assumed): clocks jump 02:00 ->
03:00 on Friday 27 March (00:00Z) and fall back 02:00 -> 01:00 on Sunday
25 October (23:00Z on the 24th).
"""

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from brain.services.schedule_spec import (
    ScheduleError,
    describe,
    next_fire,
    parse_cron,
    validate,
)

IL = ZoneInfo("Asia/Jerusalem")


def _fires(expr: str, start: datetime, count: int, tz: ZoneInfo = IL) -> list[datetime]:
    spec = parse_cron(expr)
    out: list[datetime] = []
    moment = start
    for _ in range(count):
        moment = next_fire(spec, tz, moment)
        out.append(moment)
    return out


def test_israel_2026_transitions_are_where_the_tests_assume():
    assert datetime(2026, 3, 26, 23, 59, tzinfo=UTC).astimezone(IL).utcoffset() == timedelta(
        hours=2
    )
    assert datetime(2026, 3, 27, 0, 0, tzinfo=UTC).astimezone(IL).utcoffset() == timedelta(hours=3)
    assert datetime(2026, 10, 24, 22, 59, tzinfo=UTC).astimezone(IL).utcoffset() == timedelta(
        hours=3
    )
    assert datetime(2026, 10, 24, 23, 0, tzinfo=UTC).astimezone(IL).utcoffset() == timedelta(
        hours=2
    )


def test_weekly_friday_seven_in_israel_time():
    start = datetime(2026, 11, 8, 12, 0, tzinfo=UTC)  # Sunday
    fires = _fires("0 7 * * 5", start, 2)
    assert [f.astimezone(IL).isoformat() for f in fires] == [
        "2026-11-13T07:00:00+02:00",
        "2026-11-20T07:00:00+02:00",
    ]
    assert all(f.tzinfo is UTC for f in fires)


def test_next_fire_is_strictly_after():
    at = datetime(2026, 11, 13, 5, 0, tzinfo=UTC)  # exactly Fri 07:00 IST
    assert next_fire(parse_cron("0 7 * * 5"), IL, at).astimezone(IL).day == 20


def test_timezone_changes_the_instant():
    start = datetime(2026, 11, 8, 0, 0, tzinfo=UTC)
    il = next_fire(parse_cron("0 6 * * *"), IL, start)
    ny = next_fire(parse_cron("0 6 * * *"), ZoneInfo("America/New_York"), start)
    assert il == datetime(2026, 11, 8, 4, 0, tzinfo=UTC)
    assert ny == datetime(2026, 11, 8, 11, 0, tzinfo=UTC)


def test_daily_across_spring_forward_keeps_wall_time_no_skip_no_double():
    fires = _fires("0 6 * * *", datetime(2026, 3, 25, 12, 0, tzinfo=UTC), 4)
    assert [f.astimezone(IL).strftime("%d %H:%M %z") for f in fires] == [
        "26 06:00 +0200",
        "27 06:00 +0300",
        "28 06:00 +0300",
        "29 06:00 +0300",
    ]


def test_daily_across_fall_back_keeps_wall_time_no_skip_no_double():
    fires = _fires("0 6 * * *", datetime(2026, 10, 23, 12, 0, tzinfo=UTC), 4)
    assert [f.astimezone(IL).strftime("%d %H:%M %z") for f in fires] == [
        "24 06:00 +0300",
        "25 06:00 +0200",
        "26 06:00 +0200",
        "27 06:00 +0200",
    ]


def test_time_inside_the_spring_gap_fires_once_after_the_gap():
    # 02:30 does not exist on 27 March: it runs once at 03:30 IDT, then
    # back to 02:30 the next night — never skipped.
    fires = _fires("30 2 * * *", datetime(2026, 3, 26, 12, 0, tzinfo=UTC), 2)
    assert [f.astimezone(IL).strftime("%d %H:%M") for f in fires] == ["27 03:30", "28 02:30"]


def test_gap_time_colliding_with_a_real_time_fires_once():
    # 02:30 (gap) maps onto 03:30 — also scheduled: one run at 03:30, no double.
    fires = _fires("30 2,3 * * *", datetime(2026, 3, 26, 22, 0, tzinfo=UTC), 2)
    local = [f.astimezone(IL).strftime("%d %H:%M") for f in fires]
    assert local == ["27 03:30", "28 02:30"]
    assert len(set(fires)) == 2


def test_gap_day_fires_are_ordered_and_unique():
    # 02:15/02:45 map onto 03:15/03:45 IDT (also scheduled): two runs, in order.
    fires = _fires("15,45 2,3 * * *", datetime(2026, 3, 26, 22, 0, tzinfo=UTC), 3)
    local = [f.astimezone(IL).strftime("%d %H:%M") for f in fires]
    assert local == ["27 03:15", "27 03:45", "28 02:15"]


def test_repeated_fall_back_hour_fires_once():
    # 01:30 happens twice on 25 October: run on the first occurrence only.
    fires = _fires("30 1 * * *", datetime(2026, 10, 24, 12, 0, tzinfo=UTC), 2)
    assert [f.astimezone(IL).strftime("%d %H:%M %z") for f in fires] == [
        "25 01:30 +0300",
        "26 01:30 +0200",
    ]


def test_hourly_over_fall_back_never_doubles():
    fires = _fires("0 * * * *", datetime(2026, 10, 24, 20, 30, tzinfo=UTC), 6)
    assert len(set(fires)) == 6 and fires == sorted(fires)
    walls = [f.astimezone(IL).strftime("%H:%M") for f in fires]
    assert walls.count("01:00") == 1


def test_dom_and_dow_both_restricted_is_either():
    # 1st of the month OR any Monday (Vixie semantics)
    fires = _fires("0 9 1 * 1", datetime(2026, 11, 28, 0, 0, tzinfo=UTC), 3)
    assert [f.astimezone(IL).strftime("%a %d") for f in fires] == ["Mon 30", "Tue 01", "Mon 07"]


def test_names_ranges_steps_and_sunday_seven():
    assert parse_cron("0 7 * * mon-fri").weekdays == frozenset({1, 2, 3, 4, 5})
    assert parse_cron("0 7 * * 7").weekdays == frozenset({0})
    assert parse_cron("0 */6 * * *").hours == (0, 6, 12, 18)
    assert parse_cron("0 8 * jan,jul *").months == frozenset({1, 7})
    assert parse_cron("@daily").expr == "0 0 * * *"
    assert parse_cron("  0   7 * * 5 ").expr == "0 7 * * 5"


@pytest.mark.parametrize(
    ("expr", "message"),
    [
        ("", "empty"),
        ("0 7 * *", "5 fields"),
        ("0 24 * * *", "out of range"),
        ("60 7 * * *", "out of range"),
        ("0 7 * * 8", "out of range"),
        ("0 7 * * fri-mon", "backwards"),
        ("x 7 * * *", "invalid minute"),
        ("0 ² * * *", "invalid hour"),  # "²".isdigit() is True; must be a 422, not a 500
        ("0 7 */² * *", "invalid step"),
        ("0 7 */0 * *", "invalid step"),
        ("* * * * *", "too often"),
        ("*/5 * * * *", "too often"),
        ("0 0 30 2 *", "never fires"),
    ],
)
def test_invalid_schedules_have_readable_errors(expr, message):
    with pytest.raises(ScheduleError, match=message):
        validate(expr, "Asia/Jerusalem")


def test_unknown_timezone_rejected():
    with pytest.raises(ScheduleError, match="unknown timezone"):
        validate("0 7 * * *", "Mars/Olympus")


def test_feb_29_is_a_valid_rare_schedule():
    spec, tz = validate("0 9 29 2 *", "Asia/Jerusalem")
    fire = next_fire(spec, tz, datetime(2026, 11, 8, tzinfo=UTC))
    assert fire.astimezone(IL).date().isoformat() == "2028-02-29"


@pytest.mark.parametrize(
    ("expr", "en", "he"),
    [
        ("0 7 * * 5", "Every Friday at 07:00", "כל יום שישי ב-07:00"),
        ("0 6 * * *", "Every day at 06:00", "כל יום ב-06:00"),
        ("30 7 * * 0,3", "Every Sunday, Wednesday at 07:30", "בימים ראשון, רביעי ב-07:30"),
        ("0 7 * * 0-4", "Sun–Thu at 07:00", "בימים ראשון–חמישי ב-07:00"),
        ("0 9 1 * *", "Monthly on day 1 at 09:00", "כל חודש ב-1 לחודש ב-09:00"),
        ("15 * * * *", "Every hour at :15", "כל שעה ב-:15"),
        ("0 7,19 * * *", "Cron: 0 7,19 * * *", "Cron: 0 7,19 * * *"),
    ],
)
def test_describe_in_words(expr, en, he):
    assert describe(expr, "en") == en
    assert describe(expr, "he") == he
