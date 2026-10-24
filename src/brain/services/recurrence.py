"""Recurrence expansion for calendar events (P1; shared by paste + #55 gcal).

ONE occurrence expander: an iCalendar RRULE (plus EXDATE) against a DTSTART
becomes the list of occurrence starts inside a bounded window. The paste
path (services/paste) uses it to fan a recurring VEVENT out into one item
per occurrence; the gcal connector (#55) can import the same function so
both paths expand identically.

Semantics that matter:
- WALL-CLOCK STABLE: DTSTART with a TZID keeps its local wall time across
  DST changes (a 10:00 Jerusalem meeting stays 10:00 local every week; the
  UTC instant shifts with the offset). dateutil's rrule re-attaches the
  DTSTART's tzinfo to every occurrence, which is exactly ICS semantics.
- COUNT/UNTIL are the rule's own bounds and are honoured as given (UNTIL
  inclusive; a producer-bug naive UNTIL with an aware DTSTART is interpreted
  in the DTSTART's timezone rather than failing the paste).
- EXDATE instants are skipped (matched as absolute instants).
- The window is [window_start, window_start + window_days] — occurrences
  strictly before the paste instant are not created.
- The cap bounds the returned occurrences per series (applied AFTER the
  window; the fan-out never exceeds the cap).
- Returns None only when the rule itself cannot be parsed (the caller falls
  back to the single DTSTART item — a paste is never lost to a malformed
  rule); a valid rule with an empty window returns an empty list.
- A MISSING python-dateutil fails LOUDLY: the import is module-level, so an
  env that skipped the editable install errors on the first recurring paste
  instead of silently degrading every series to one item (worker_10 hit
  exactly that). The broad except below is ONLY for malformed rule text.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta

from dateutil.rrule import rrulestr

_UNTIL_RE = re.compile(r"(^|;)UNTIL=([^;]+)", re.IGNORECASE)


def expand_occurrences(
    *,
    dtstart: datetime,
    rrule_text: str,
    exdates: Sequence[datetime] = (),
    window_start: datetime,
    window_days: int,
    cap: int,
) -> list[datetime] | None:
    """Occurrence starts for one series, windowed and capped (never raises).

    ``dtstart`` is the VEVENT's DTSTART (aware; the anchor of the series —
    COUNT and WEEKLY anchor on it, so it is NOT windowed here). Returns
    ascending aware starts, or None when the rule cannot be parsed (the
    caller falls back to the single DTSTART item).
    """
    if not rrule_text.strip() or cap <= 0 or window_days < 0:
        return None
    window_end = window_start + timedelta(days=window_days)
    try:
        rule = _build_rule(dtstart, rrule_text)
        # between() is bounded — iterating the rule directly would walk an
        # infinite rule to year 10000
        occurrences = [
            instant
            for instant in rule.between(window_start, window_end, inc=True)
            if not any(instant == skipped for skipped in exdates)
        ]
    except Exception:  # noqa: BLE001 — a malformed rule never fails a paste
        return None
    return occurrences[:cap]


def _build_rule(dtstart: datetime, rrule_text: str):
    """dateutil rule for the RRULE value; UNTIL normalized to the DTSTART's zone."""
    text = rrule_text.strip()

    def _until_naive(match: re.Match) -> str:
        prefix, value = match.group(1), match.group(2)
        if value.endswith("Z") or dtstart.tzinfo is None:
            return match.group(0)
        # producer bug (RFC requires UTC UNTIL with an aware DTSTART):
        # interpret the naive UNTIL in the DTSTART's timezone — at any
        # position in the value (leading UNTIL=... has no ';' prefix)
        try:
            aware = datetime.fromisoformat(value).replace(tzinfo=dtstart.tzinfo)
        except ValueError:
            return match.group(0)
        return f"{prefix}UNTIL={aware.astimezone(UTC).strftime('%Y%m%dT%H%M%SZ')}"

    text = _UNTIL_RE.sub(_until_naive, text)
    return rrulestr(text, dtstart=dtstart)
