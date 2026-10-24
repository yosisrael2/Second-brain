"""Paste-aware capture: detect the pasted format and extract metadata.

Pure, framework-free functions. :func:`parse` returns a :class:`ParsedPaste`
with one :class:`ParsedChunk` per item to create; ``extracted`` is a
JSON-serializable summary for the API response. Unknown formats fall back to
``note`` — detection never raises.

Timezone rule: ``occurred_at`` is always an aware UTC datetime. Zone-less
pasted times (Gmail, WhatsApp, copied Google/Outlook event text) are
interpreted in the ``zone`` passed by the caller (the app's ``TIMEZONE``
setting); explicit offsets (Outlook ``+0300``, ICS ``TZID`` / ``Z``) are
honored as given. The original raw strings stay in ``meta`` for audit
(``meta["date"]``, ``meta["at_raw"]``, ``meta["start_raw"]``).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any
from zoneinfo import ZoneInfo

from brain.config import local_timezone_name

KIND_EMAIL = "email"
KIND_WHATSAPP = "whatsapp"
KIND_CALENDAR = "calendar"
KIND_NOTE = "note"

KINDS = (KIND_EMAIL, KIND_WHATSAPP, KIND_CALENDAR, KIND_NOTE)

# Bidi control marks pasted from RTL (Hebrew) WhatsApp / Mail copies.
_RTL_MARKS = ("\u200e", "\u200f", "\u202a", "\u202b", "\u202c", "\u202d", "\u202e")

_APPLE_MAIL_MARKER = "begin forwarded message"
_HEBREW_MAIL_MARKER = "תחילת הודעה מועברת"

# ---- email ------------------------------------------------------------------

_EMAIL_HEADER_RE = re.compile(
    r"^(from|to|cc|subject|date|מאת|אל|נושא|תאריך)\s*:\s*(.*)$", re.IGNORECASE
)
_GMAIL_DATE_RE = re.compile(
    r"^(?:[A-Za-z]{3},\s+)?([A-Za-z]{3})\s+(\d{1,2}),?\s+(\d{4})"
    r"(?:\s+at\s+|\s+)(\d{1,2}):(\d{2})\s*([AaPp]\.?[Mm]\.?)?$"
)
_FORWARDED_BANNER_RE = re.compile(r"^-+\s*forwarded message\s*-+$", re.IGNORECASE)

# ---- whatsapp ----------------------------------------------------------------

# [DD/MM/YYYY, HH:MM[:SS] [am|pm]] Name: text   (iOS) — brackets optional,
# separator "- " or ": " after the timestamp; 12h/24h; DD/MM/YYYY (Israel
# order) with 2- or 4-digit years.
_WA_LINE_RE = re.compile(
    r"^\[?(\d{1,2})/(\d{1,2})/(\d{2,4}),?\s+"
    r"(\d{1,2}):(\d{2})(?::(\d{2}))?\s*([AaPp]\.?[Mm]\.?)?\]?\s*"
    r"(?:[-–]\s*)?(?:([^:\n]{1,64}):\s)?(.*)$"
)

# ---- calendar ----------------------------------------------------------------

_WEEKDAY = r"(?:mon|tues|wednes|thurs|fri|satur|sun)day"
# "Tuesday, October 21 · 10:00 – 11:00am" (Google Calendar copy; the date may
# carry a year, the start may carry am/pm, the end must carry a time).
_CAL_EVENT_RE = re.compile(
    rf"^{_WEEKDAY},?\s+([A-Za-z]+\s+\d{{1,2}}(?:,?\s+\d{{4}})?)\s*[·•]\s*"
    r"(\d{1,2}:\d{2})\s*([AaPp]\.?[Mm]\.?)?\s*[–—-]\s*"
    r"(\d{1,2}:\d{2})\s*([AaPp]\.?[Mm]\.?)",
    re.IGNORECASE,
)
# "Thursday, October 22, 2026" (Outlook copy; year required to disambiguate).
_CAL_DATE_RE = re.compile(
    rf"^{_WEEKDAY},?\s+([A-Za-z]+\s+\d{{1,2}},?\s+\d{{4}})\.?$", re.IGNORECASE
)
# "9:30 AM - 10:00 AM" (Outlook) / "10:00 - 11:00" — needs a known date line.
_CAL_TIME_RE = re.compile(
    r"^(\d{1,2}):(\d{2})\s*([AaPp]\.?[Mm]\.?)?\s*[–—-]\s*"
    r"(\d{1,2}):(\d{2})\s*([AaPp]\.?[Mm]\.?)?$",
    re.IGNORECASE,
)
_EMAIL_ADDRESS_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")

_MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6, "jul": 7,
    "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
    "january": 1, "february": 2, "march": 3, "april": 4, "june": 6,
    "july": 7, "august": 8, "september": 9, "october": 10, "november": 11,
    "december": 12,
}


@dataclass(frozen=True)
class ParsedChunk:
    """One item's worth of paste: text, when it happened, extracted metadata."""

    raw_text: str
    occurred_at: datetime | None = None
    meta: dict[str, Any] = field(default_factory=dict)
    source: str = KIND_NOTE


@dataclass(frozen=True)
class ParsedPaste:
    """Detection result: the kind, one chunk per item, and a JSON summary."""

    kind: str
    chunks: list[ParsedChunk]
    extracted: dict[str, Any] = field(default_factory=dict)


def detect_kind(text: str, kind_hint: str = "auto") -> str:
    """Return the paste kind: an explicit hint wins, else the best heuristic."""
    if kind_hint in KINDS:
        return kind_hint
    lines = [line.strip() for line in _strip_rtl(text).strip().lower().splitlines()]
    if "begin:vevent" in text.lower() or any(
        line.startswith(("dtstart", "dtend")) or _CAL_EVENT_RE.match(line)
        for line in lines
    ):
        return KIND_CALENDAR
    # Outlook copy style: a bare "Thursday, October 22, 2026" date line
    # followed by a bare time-range line.
    for current, following in zip(lines, lines[1:], strict=False):
        if _CAL_DATE_RE.match(current) and _CAL_TIME_RE.match(following):
            return KIND_CALENDAR
    if any(_WA_LINE_RE.match(line) for line in lines):
        return KIND_WHATSAPP
    if _looks_like_email(lines):
        return KIND_EMAIL
    return KIND_NOTE


def parse(
    text: str,
    kind_hint: str = "auto",
    split: bool = False,
    tz: ZoneInfo | None = None,
    now: datetime | None = None,
) -> ParsedPaste:
    """Detect the paste kind and return its chunks (``split`` fans out).

    ``tz`` is the IANA zone used to interpret zone-less times (defaults to
    the machine's local zone; the /capture route passes the ``TIMEZONE``
    setting). All ``occurred_at`` values are returned aware (UTC for
    zone-less times, the explicit offset otherwise). ``now`` is the paste
    instant (defaults to the real clock); a recurring ICS event fans out
    into one item per occurrence within [now, now + BRAIN_PASTE_RRULE_DAYS].
    """
    zone = tz if tz is not None else ZoneInfo(local_timezone_name())
    paste_time = now if now is not None else datetime.now(UTC)
    kind = detect_kind(text, kind_hint)
    if kind == KIND_WHATSAPP:
        return _parse_whatsapp(text, split=split, zone=zone)
    parsed = {
        KIND_EMAIL: _parse_email,
        KIND_CALENDAR: _parse_calendar,
        KIND_NOTE: _parse_note,
    }[kind](text, split=split, zone=zone, now=paste_time)
    if (
        not split
        and len(parsed.chunks) > 1
        and not (kind == KIND_CALENDAR and parsed.extracted.get("rrule_raw"))
    ):
        # thread pastes collapse into one item; an RRULE fan-out must NOT —
        # its chunks are independent occurrences, not thread messages
        return _join_chunks(parsed)
    return parsed


def _strip_rtl(text: str) -> str:
    """Remove bidi control marks (U+200E/U+200F/U+202A-U+202E) from a paste."""
    for mark in _RTL_MARKS:
        text = text.replace(mark, "")
    return text


def _looks_like_email(lines: list[str]) -> bool:
    if any(
        _APPLE_MAIL_MARKER in line
        or _HEBREW_MAIL_MARKER in line
        or _FORWARDED_BANNER_RE.match(line)
        for line in lines
    ):
        return True
    return sum(1 for line in lines if _EMAIL_HEADER_RE.match(line)) >= 2


def _is_forward_preamble(line: str) -> bool:
    """True for blank lines and forwarded-message banners/headers to skip."""
    stripped = line.strip()
    if not stripped:
        return True
    lowered = stripped.lower()
    if _APPLE_MAIL_MARKER in lowered or _HEBREW_MAIL_MARKER in stripped:
        return True
    if _FORWARDED_BANNER_RE.match(lowered):
        return True
    return stripped.endswith(":") and _EMAIL_HEADER_RE.match(stripped) is None


def _chunk(
    text: str, occurred_at: datetime | None, meta: dict[str, Any], source: str
) -> ParsedChunk:
    return ParsedChunk(raw_text=text.strip(), occurred_at=occurred_at, meta=meta, source=source)


def _join_chunks(parsed: ParsedPaste) -> ParsedPaste:
    """Collapse a multi-chunk paste into one item (whole-thread capture)."""
    messages = [
        entry for chunk in parsed.chunks for entry in chunk.meta.get("messages", [])
    ]
    meta: dict[str, Any] = {
        "count": len(messages),
        "messages": messages,
        "senders": list(dict.fromkeys(m["sender"] for m in messages if m["sender"])),
    }
    raw = "\n\n".join(chunk.raw_text for chunk in parsed.chunks)
    return ParsedPaste(
        kind=parsed.kind,
        chunks=[_chunk(raw, parsed.chunks[0].occurred_at, meta, parsed.chunks[0].source)],
        extracted=dict(meta),
    )


# ---- email -------------------------------------------------------------------


def _parse_email(
    text: str, *, split: bool = False, zone: ZoneInfo, now: datetime | None = None
) -> ParsedPaste:
    clean = _strip_rtl(text).strip()
    meta: dict[str, str] = {}
    lines = clean.splitlines()
    # Skip leading banner/marker/blank lines ("---------- Forwarded message
    # ---------", "Begin forwarded message:", Hebrew equivalent) so the
    # header block that follows is captured.
    while lines and _is_forward_preamble(lines[0]):
        lines = lines[1:]
    for line in lines:
        match = _EMAIL_HEADER_RE.match(line.strip())
        if match is None:
            break
        key = match.group(1).lower()
        key = {"מאת": "from", "אל": "to", "נושא": "subject", "תאריך": "date"}.get(key, key)
        meta[key] = match.group(2).strip()
        if len(meta) > 12:  # safety valve against pathological pastes
            break
    occurred_at = _parse_any_date(meta.get("date", "")) if "date" in meta else None
    if occurred_at is not None:
        if occurred_at.tzinfo is None:
            # Zone-less (Gmail copy): interpret in TIMEZONE, store as aware UTC.
            occurred_at = occurred_at.replace(tzinfo=zone).astimezone(UTC)
        meta["date_iso"] = occurred_at.isoformat()  # the instant as parsed
    return ParsedPaste(
        kind=KIND_EMAIL,
        chunks=[_chunk(clean, occurred_at, meta, KIND_EMAIL)],
        extracted=dict(meta),
    )


def _parse_any_date(value: str) -> datetime | None:
    """Parse an email date: Gmail's copy format first, then RFC 2822/5322."""
    value = value.strip()
    if not value:
        return None
    match = _GMAIL_DATE_RE.match(value)
    if match is not None:
        month = _MONTHS[match.group(1).lower()[:3]]
        hour = int(match.group(4))
        if (match.group(6) or "").lower().startswith("p") and hour < 12:
            hour += 12
        try:
            return datetime(
                int(match.group(3)), month, int(match.group(2)), hour, int(match.group(5))
            )
        except ValueError:
            return None
    try:
        return parsedate_to_datetime(value)
    except (TypeError, ValueError, IndexError):
        return None


# ---- whatsapp ------------------------------------------------------------------


def _parse_whatsapp(
    text: str, *, split: bool = False, zone: ZoneInfo
) -> ParsedPaste:
    clean = _strip_rtl(text).strip()
    messages: list[dict[str, Any]] = []
    occurred: list[datetime | None] = []
    message_lines: list[list[str]] = []

    for line in clean.splitlines():
        match = _WA_LINE_RE.match(line.strip())
        if match is None:
            if message_lines:
                message_lines[-1].append(line)  # continuation of the previous message
            continue
        day, month, year, hour, minute, second, meridiem, sender, _body = match.groups()
        at_utc = _wa_occurred(day, month, year, hour, minute, second, meridiem, zone)
        if at_utc is None:  # unparseable stamp: skip the line defensively
            continue
        messages.append(
            {
                "sender": sender or "",
                "at": at_utc.isoformat(),
                "at_raw": _wa_raw_stamp(
                    day, month, year, hour, minute, second, meridiem
                ),
            }
        )
        occurred.append(at_utc)
        message_lines.append([line.strip()])

    def chunk_for(index: int) -> ParsedChunk:
        message = messages[index]
        return ParsedChunk(
            raw_text="\n".join(message_lines[index]).strip(),
            occurred_at=occurred[index],
            meta={"messages": [dict(message)]},
            source=KIND_WHATSAPP,
        )

    if split:
        chunks = [chunk_for(index) for index in range(len(messages))]
        extracted = {"count": len(messages), "messages": messages}
    else:
        chunks = [
            _chunk(
                clean,
                occurred[0] if occurred else None,
                {"count": len(messages), "messages": messages},
                KIND_WHATSAPP,
            )
        ]
        extracted = {
            "count": len(messages),
            "messages": messages,
            "senders": list(dict.fromkeys(m["sender"] for m in messages if m["sender"])),
        }
    return ParsedPaste(kind=KIND_WHATSAPP, chunks=chunks, extracted=extracted)


def _wa_raw_stamp(
    day: str, month: str, year: str, hour: str, minute: str,
    second: str | None, meridiem: str | None,
) -> str:
    """The timestamp exactly as written in the paste (for meta audit)."""
    stamp = f"{day}/{month}/{year}, {hour}:{minute}"
    if second:
        stamp += f":{second}"
    if meridiem:
        stamp += f" {meridiem}"
    return stamp


def _wa_occurred(
    day: str, month: str, year: str, hour: str, minute: str,
    second: str | None, meridiem: str | None, zone: ZoneInfo,
) -> datetime | None:
    """WhatsApp timestamp (zone-less) -> aware UTC instant in ``zone``."""
    naive = _wa_datetime(
        _wa_iso(day, month, year, hour, minute, second, meridiem)
    )
    if naive is None:
        return None
    return naive.replace(tzinfo=zone).astimezone(UTC)


def _wa_iso(day: str, month: str, year: str, hour: str, minute: str,
            second: str | None, meridiem: str | None) -> str:
    """Normalize a WhatsApp timestamp to 'YYYY-MM-DDTHH:MM[:SS]' (local time)."""
    year_int = int(year) + (2000 if int(year) < 100 else 0)
    hour_int = int(hour)
    if meridiem and meridiem.lower().startswith("p") and hour_int < 12:
        hour_int += 12
    if meridiem and meridiem.lower().startswith("a") and hour_int == 12:
        hour_int = 0
    second_part = f":{int(second):02d}" if second else ""
    return (
        f"{year_int:04d}-{int(month):02d}-{int(day):02d}"
        f"T{hour_int:02d}:{int(minute):02d}{second_part}"
    )


def _wa_datetime(iso: str) -> datetime | None:
    try:
        return datetime.fromisoformat(iso)
    except ValueError:
        return None


# ---- calendar ------------------------------------------------------------------


def _parse_calendar(
    text: str, *, split: bool = False, zone: ZoneInfo, now: datetime
) -> ParsedPaste:
    clean = _strip_rtl(text).strip()
    looks_like_ics = "begin:vevent" in clean.lower() or any(
        line.strip().upper().startswith(("DTSTART", "DTEND", "SUMMARY"))
        for line in clean.splitlines()
    )
    if looks_like_ics:
        return _parse_ics(clean, zone=zone, now=now)
    return _parse_calendar_text(clean, zone=zone)


def _unfold(lines: list[str]) -> list[str]:
    """Join iCalendar folded lines (continuations start with a space/tab)."""
    unfolded: list[str] = []
    for line in lines:
        if line[:1] in (" ", "\t") and unfolded:
            unfolded[-1] += line[1:]
        else:
            unfolded.append(line)
    return unfolded


def _parse_ics(text: str, *, split: bool = False, zone: ZoneInfo, now: datetime) -> ParsedPaste:
    clean = _strip_rtl(text).strip()
    meta: dict[str, Any] = {"attendees": []}
    occurred_at: datetime | None = None
    date_only = False
    tzid = ""
    uid = ""
    rrule_text = ""
    exdates: list[datetime] = []
    for raw_line in _unfold(clean.replace("\r\n", "\n").split("\n")):
        if ":" not in raw_line:
            continue
        left, value = raw_line.split(":", 1)
        name = left.split(";", 1)[0].strip().upper()
        if match := re.search(r"TZID=([^:;]+)", left):
            tzid = match.group(1).strip()
        if name == "UID":
            uid = value.strip()
        elif name == "SUMMARY":
            meta["title"] = value.strip()
        elif name in ("LOCATION", "ORGANIZER"):
            meta[name.lower()] = value.strip()
        elif name == "RRULE":
            rrule_text = value.strip()
        elif name == "EXDATE":
            # may repeat; the values are a comma list; an EXDATE-specific
            # TZID param wins over the line-level tzid picked up above
            ex_tzid = tzid
            if tz_match := re.search(r"TZID=([^:;]+)", left):
                ex_tzid = tz_match.group(1).strip()
            for part in value.split(","):
                parsed_ex = _ics_datetime(part, ex_tzid, zone)
                if parsed_ex is not None:
                    exdates.append(parsed_ex)
        elif name in ("DTSTART", "DTEND"):
            parsed_at = _ics_datetime(value, tzid, zone)
            if parsed_at is not None:
                # Z/TZID offsets are explicit and honored as given; meta also
                # keeps the raw value for audit.
                meta["start" if name == "DTSTART" else "end"] = parsed_at.isoformat()
                meta["start_raw" if name == "DTSTART" else "end_raw"] = value.strip()
                if name == "DTSTART":
                    occurred_at = parsed_at
                    date_only = len(value.strip()) == 8 and value.strip().isdigit()
        elif name == "ATTENDEE":
            if email := _EMAIL_ADDRESS_RE.search(value):
                meta["attendees"].append(email.group(0))
    meta = {key: value for key, value in meta.items() if value not in (None, [], "")}
    meta["ics_raw"] = clean  # the whole ICS: raw_text below becomes a one-liner
    recurring = _recurring_chunks(
        meta,
        occurred_at,
        date_only,
        zone,
        now,
        uid=uid,
        rrule_text=rrule_text,
        exdates=exdates,
    )
    if recurring is not None:
        return ParsedPaste(
            kind=KIND_CALENDAR,
            chunks=recurring,
            extracted={
                "series_uid": uid or None,
                "rrule_raw": rrule_text,
                "count": len(recurring),
            },
        )
    return ParsedPaste(
        kind=KIND_CALENDAR,
        chunks=[
            _chunk(
                _calendar_one_liner(meta, zone, date_only=date_only),
                occurred_at,
                meta,
                KIND_CALENDAR,
            )
        ],
        extracted=dict(meta),
    )


def _recurring_chunks(
    meta: dict[str, Any],
    occurred_at: datetime | None,
    date_only: bool,
    zone: ZoneInfo,
    now: datetime,
    *,
    uid: str,
    rrule_text: str,
    exdates: list[datetime],
) -> list[ParsedChunk] | None:
    """One chunk per RRULE occurrence, or None when this paste is not a
    recurring series (no/blank RRULE, no DTSTART, or an unparseable rule —
    the caller keeps the single-item behaviour in every fallback case)."""
    from brain.config import get_settings  # noqa: PLC0415 — cheap, local
    from brain.services.recurrence import expand_occurrences  # noqa: PLC0415

    if not rrule_text or occurred_at is None:
        return None
    settings = get_settings()
    end = _meta_instant(meta.get("end"))
    # the ANCHOR must be the parsed DTSTART (its tzinfo is the ICS-declared
    # zone — ZoneInfo for TZID, UTC for Z), NOT the iso-re-parsed meta value
    # (a fixed offset would freeze the UTC offset across DST changes)
    duration = (end - occurred_at) if end is not None and end > occurred_at else None
    occurrences = expand_occurrences(
        dtstart=occurred_at,
        rrule_text=rrule_text,
        exdates=exdates,
        window_start=now,
        window_days=settings.paste_rrule_days,
        cap=settings.paste_rrule_max_occurrences,
    )
    if occurrences is None:
        return None  # unparseable rule: fall back to the single DTSTART item
    drop = ("ics_raw", "start", "end", "start_raw", "end_raw")
    shared = {k: v for k, v in meta.items() if k not in drop}
    chunks: list[ParsedChunk] = []
    for instant in occurrences:
        occ_meta = dict(shared)
        occ_meta["start"] = instant.isoformat()
        if duration is not None:
            occ_meta["end"] = (instant + duration).isoformat()
        if uid:
            occ_meta["series_uid"] = uid
        occ_meta["rrule_raw"] = rrule_text
        chunks.append(
            _chunk(
                _calendar_one_liner(occ_meta, zone, date_only=date_only),
                instant,
                occ_meta,
                KIND_CALENDAR,
            )
        )
    return chunks


def _ics_datetime(value: str, tzid: str = "", zone: ZoneInfo | None = None) -> datetime | None:
    """Parse an iCalendar date/date-time: with Z, with TZID, or date-only.

    Z and TZID values are honored as given (aware, original offset); the
    date-only form is zone-less and is interpreted in ``zone`` (aware UTC).
    """
    value = value.strip()
    fallback = zone if zone is not None else ZoneInfo(local_timezone_name())
    try:
        if value.endswith("Z"):
            return datetime.fromisoformat(value[:-1]).replace(tzinfo=UTC)
        if "T" in value:
            naive = datetime.fromisoformat(value)
            if tzid:
                return naive.replace(tzinfo=ZoneInfo(tzid))
            if fallback is not None:
                return naive.replace(tzinfo=fallback).astimezone(UTC)
            return naive
        if len(value) == 8 and value.isdigit():
            naive = datetime.strptime(value, "%Y%m%d")
            return naive.replace(tzinfo=fallback).astimezone(UTC)
    except (ValueError, KeyError):
        return None
    return None


def _calendar_one_liner(
    meta: dict[str, Any], zone: ZoneInfo, *, date_only: bool = False
) -> str:
    """Human one-liner for a calendar event, in the app's timezone.

    Format: ``<title> — <Day DD Mon HH:MM–HH:MM> [@ location]`` (en dash
    between the times, em dash after the title). Date-only events (ICS
    ``VALUE=DATE``) render the day without times. Unparseable instants
    degrade gracefully: title only.
    """
    title = str(meta.get("title") or "").strip() or "Calendar event"
    start = _meta_instant(meta.get("start"))
    end = _meta_instant(meta.get("end"))
    parts: list[str] = []
    if start is not None:
        local = start.astimezone(zone)
        parts.append(f"{local:%a} {local.day} {local:%b}")
        if not date_only:
            parts.append(f"{local:%H:%M}")
            if end is not None:
                parts.append(f"–{end.astimezone(zone):%H:%M}")
    line = title
    if parts:
        day = parts[0]
        times = "".join(parts[1:])
        line = f"{title} — {day} {times}".rstrip() if times else f"{title} — {day}"
    if location := str(meta.get("location") or "").strip():
        line += f" @ {location}"
    return line


def _meta_instant(value: Any) -> datetime | None:
    """Parse an isoformat instant from extracted meta; None when absent/bad."""
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value))
    except ValueError:
        return None


def _parse_calendar_text(
    text: str, *, split: bool = False, zone: ZoneInfo
) -> ParsedPaste:
    """Parse Google/Outlook copied event text (title, date · time range, …)."""
    lines = [line.strip() for line in _strip_rtl(text).strip().splitlines() if line.strip()]
    meta: dict[str, Any] = {}
    occurred_at: datetime | None = None
    date_text = ""
    title: str | None = None
    time_range_end = -1

    for index, line in enumerate(lines):
        event_match = _CAL_EVENT_RE.match(line)
        date_match = _CAL_DATE_RE.match(line)
        time_match = _CAL_TIME_RE.match(line)
        if event_match is not None:
            event_date = event_match.group(1)
            # "10:00 – 11:00am" / "9:00 – 12:30pm" / "2:00 – 3:00pm": a start
            # without meridiem inherits am/pm from the end — pm only when that
            # still leaves start before end (9am–12:30pm stays morning).
            start_time, start_meridiem = event_match.group(2), event_match.group(3)
            end_time, end_meridiem = event_match.group(4), event_match.group(5)
            end_at = _date_with_time(event_date, end_time, end_meridiem)
            if start_meridiem is None and end_meridiem is not None:
                end_mer = end_meridiem.lower().strip(". ")
                if end_mer.startswith("a"):
                    start_meridiem = end_meridiem
                elif (
                    end_at is not None
                    and int(start_time.split(":")[0]) + 12 < end_at.hour
                ):
                    start_meridiem = end_meridiem
            start_naive = _date_with_time(event_date, start_time, start_meridiem)
            occurred_at = _aware(start_naive, zone) if start_naive else None
            if occurred_at is not None:
                meta["start"] = occurred_at.isoformat()
                meta["start_raw"] = line
                if end_at is not None:
                    meta["end"] = _aware(end_at, zone).isoformat()
            if index > 0:
                title = lines[0]
            time_range_end = index
            break
        if date_match := _CAL_DATE_RE.match(line):
            date_text = date_match.group(1)
            time_range_end = index
            continue
        if (time_match := _CAL_TIME_RE.match(line)) and date_text:
            start_naive = _date_with_time(
                date_text,
                f"{time_match.group(1)}:{time_match.group(2)}",
                time_match.group(3),
            )
            occurred_at = _aware(start_naive, zone) if start_naive else None
            if occurred_at is not None:
                meta["start"] = occurred_at.isoformat()
                meta["start_raw"] = line
            if time_match.group(4):
                end_naive = _date_with_time(
                    date_text,
                    f"{time_match.group(4)}:{time_match.group(5)}",
                    time_match.group(6) or time_match.group(3),
                )
                if end_naive is not None:
                    meta["end"] = _aware(end_naive, zone).isoformat()
            time_range_end = index
            continue
        if line.lower().startswith(("location:", "מקום:")):
            meta["location"] = line.split(":", 1)[1].strip()
        elif line.lower().startswith(("attendees:", "participants:", "משתתפים:")):
            meta["attendees"] = _EMAIL_ADDRESS_RE.findall(line)
        elif index == 0 and title is None:
            title = line
    if time_range_end >= 0:
        for line in lines[time_range_end + 1 :]:
            if line.lower().startswith(("attendees:", "participants:", "משתתפים:")):
                meta["attendees"] = _EMAIL_ADDRESS_RE.findall(line)
            elif "location" not in meta:
                meta["location"] = line
    if title:
        meta["title"] = title
    meta["attendees"] = meta.get("attendees") or _EMAIL_ADDRESS_RE.findall(text)
    meta = {key: value for key, value in meta.items() if value not in (None, [], "")}
    meta["original_text"] = text.strip()  # raw_text below becomes a one-liner
    return ParsedPaste(
        kind=KIND_CALENDAR,
        chunks=[
            _chunk(_calendar_one_liner(meta, zone), occurred_at, meta, KIND_CALENDAR)
        ],
        extracted=dict(meta),
    )


def _aware(naive: datetime, zone: ZoneInfo) -> datetime:
    """Interpret a zone-less local time in ``zone`` and return it as aware UTC."""
    return naive.replace(tzinfo=zone).astimezone(UTC)


def _parse_month_day(text: str) -> datetime | None:
    """Parse 'October 21' / 'October 21, 2026'; no year means the current one."""
    match = re.match(r"([A-Za-z]+)\s+(\d{1,2})(?:,?\s+(\d{4}))?$", text.strip())
    if match is None:
        return None
    month = _MONTHS.get(match.group(1).lower())
    if month is None:
        return None
    year = int(match.group(3)) if match.group(3) else datetime.now().year
    try:
        return datetime(year, month, int(match.group(2)))
    except ValueError:
        return None


def _date_with_time(
    date_text: str, time_str: str, meridiem: str | None = None
) -> datetime | None:
    """Combine a month-day date with 'HH:MM' (+optional am/pm) into a datetime."""
    date = _parse_month_day(date_text)
    match = re.match(r"^(\d{1,2}):(\d{2})$", (time_str or "").strip())
    if date is None or match is None:
        return None
    hour, minute = int(match.group(1)), int(match.group(2))
    if meridiem:
        meridiem = meridiem.lower().strip(". ")
        if meridiem.startswith("p") and hour < 12:
            hour += 12
        elif meridiem.startswith("a") and hour == 12:
            hour = 0
    try:
        return date.replace(hour=hour, minute=minute)
    except ValueError:
        return None


def _parse_note(
    text: str, *, split: bool = False, zone: ZoneInfo, now: datetime | None = None
) -> ParsedPaste:
    clean = _strip_rtl(text).strip()
    return ParsedPaste(
        kind=KIND_NOTE,
        chunks=[_chunk(clean, None, {}, KIND_NOTE)],
        extracted={},
    )
