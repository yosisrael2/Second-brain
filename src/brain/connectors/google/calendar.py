"""Google Calendar connector (source name ``gcal``, scope ``calendar.readonly``).

Incremental sync uses the Calendar API's native ``syncToken`` mechanism
(``events.list``): the cursor is a JSON object mapping calendar id ->
nextSyncToken for every allowlisted calendar (``BRAIN_GCAL_CALENDARS``,
default ``"primary"``). A cursor carries ONE token per calendar; when a
calendar's token is rejected (HTTP 410 — expired), that calendar is fully
re-listed without a token and the batch reports ``reset=True``.

RECURRING EVENTS — expansion decision (connectors contract requires an
explicit pick): one record per SERIES, the raw recurrence rules surfaced in
``text`` ("Repeats: RRULE:...") and ``meta["recurrence"]``. NOT
``singleEvents=True`` instance expansion, because:

1. ``syncToken``-based incremental sync never expands recurrences (the API
   returns the series master with its ``recurrence``, and only individually
   touched instances as instance-id events) — instance records produced on a
   full sync would have no incremental counterpart, and external_ids would
   flip between series-form and instance-form across sync modes, breaking
   the runner's stable-``external_id`` idempotency.
2. Expansion needs a bounded window, but open-ended RRULEs have no safe
   horizon: occurrences past the window would silently vanish, and every
   full resync would re-materialize the whole (unbounded) occurrence set.

Cost of the pick: a single cancelled OCCURRENCE arrives as a ``cancelled``
instance record (id ``<seriesId>_<time>``) with no prior series-internal
record; it is mapped to a ``deleted=True`` record per the contract, and the
series record itself stays (the series still exists). The runner decides how
unknown deleted ids are recorded — flagged in the PR for the runner owner.

All-day events (``date``, no ``dateTime``) are interpreted in the app
timezone and rendered date-only; ``meta["end"]`` keeps Google's exclusive
end date exactly as sent (RFC 5545 semantics, same as the ICS paste path).
``occurred_at`` is the event start (aware, canonical UTC).

Text renders in the app timezone (``TIMEZONE``, Asia/Jerusalem in
production) reusing the repo's existing calendar one-liner
(``brain.services.paste._calendar_one_liner``, the same renderer the ICS
paste path and ``brain normalize-calendar`` use), then adds the raw
recurrence, description, organizer and attendee display names:

    <title> — Thu 29 Oct 17:00–18:30 @ <location>
    Repeats: RRULE:FREQ=WEEKLY;BYDAY=TH
    <description>
    Organizer: <name>
    Attendees: <name>, <name>

Events carry no byte attachments in this scope (event file attachments are
Drive files; fetching them needs ``drive.readonly`` — out of this ticket).
"""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo

from brain.connectors import register
from brain.connectors.base import ConnectorError, FetchResult, SourceRecord
from brain.connectors.google.base import GoogleConnector
from brain.services.paste import _calendar_one_liner

GCAL_SCOPE = "https://www.googleapis.com/auth/calendar.readonly"

# events.list page size cap used per request (the API's own page cap is
# higher; small pages keep a bounded-memory walk and honor ``limit`` fast).
_MAX_PAGE_RESULTS = 250

_TITLE_FALLBACK = "Calendar event"


class _SyncTokenExpired(ConnectorError):
    """A calendar's syncToken was rejected (HTTP 410) — full re-list needed."""


def _person_name(entry: dict[str, Any] | None) -> str:
    """Display name of an organizer/attendee entry; falls back to email."""
    if not entry:
        return ""
    return str(entry.get("displayName") or "").strip() or str(entry.get("email") or "").strip()


def _event_instant(value: dict[str, Any] | None, tz: ZoneInfo) -> tuple[datetime | None, bool]:
    """Parse an event start/end object -> (aware instant, all_day).

    ``dateTime`` values carry their own offset (Google always sends one); a
    zone-less value would be interpreted in ``tz``. ``date`` values (all-day)
    are zone-less by definition and are interpreted in ``tz``.
    """
    if not value:
        return None, False
    date_time = str(value.get("dateTime") or "").strip()
    if date_time:
        try:
            instant = datetime.fromisoformat(date_time)
        except ValueError:
            return None, False
        if instant.tzinfo is None:
            instant = instant.replace(tzinfo=tz)
        return instant, False
    day = str(value.get("date") or "").strip()
    if day:
        try:
            return datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=tz), True
        except ValueError:
            return None, False
    return None, False


def record_from_event(payload: dict[str, Any], tz: ZoneInfo) -> SourceRecord:
    """Map one Calendar API event payload -> :class:`SourceRecord` (pure).

    Raises ValueError when the payload carries no event id — a record without
    a stable external id can never be synced idempotently, so it is refused
    instead of silently duplicated.
    """
    event_id = str(payload.get("id") or "").strip()
    if not event_id:
        raise ValueError("gcal event payload is missing 'id'")

    start, all_day = _event_instant(payload.get("start"), tz)
    end, _ = _event_instant(payload.get("end"), tz)
    title = str(payload.get("summary") or "").strip() or _TITLE_FALLBACK
    location = str(payload.get("location") or "").strip()
    description = str(payload.get("description") or "").strip()
    recurrence = [str(r).strip() for r in (payload.get("recurrence") or []) if str(r).strip()]
    version = str(payload.get("etag") or payload.get("updated") or "") or None
    url = str(payload.get("htmlLink") or "") or None

    meta: dict[str, Any] = {"title": title, "all_day": all_day}
    if start is not None:
        meta["start"] = start.isoformat()
    if end is not None:
        meta["end"] = end.isoformat()  # all-day: exclusive end date, as Google sends it
    if location:
        meta["location"] = location
    if recurrence:
        meta["recurrence"] = recurrence
    status = str(payload.get("status") or "").strip()
    if status:
        meta["status"] = status

    lines = [_calendar_one_liner(meta, tz, date_only=all_day)]
    if recurrence:
        lines.append("Repeats: " + "; ".join(recurrence))
    if description:
        lines.append(description)
    organizer = _person_name(payload.get("organizer"))
    if organizer:
        lines.append(f"Organizer: {organizer}")
    attendees = [name for a in (payload.get("attendees") or []) if (name := _person_name(a))]
    if attendees:
        lines.append("Attendees: " + ", ".join(attendees))

    return SourceRecord(
        source="gcal",
        external_id=event_id,
        version=version,
        title=title,
        text="\n".join(lines),
        occurred_at=start.astimezone(UTC) if start is not None else None,
        url=url,
        meta=meta,
        attachments=(),  # no byte attachments in the calendar.readonly scope
        deleted=payload.get("status") == "cancelled",
    )


def _parse_cursor(cursor: str | None) -> dict[str, str]:
    """Decode the gcal cursor: JSON {calendarId: syncToken}. Bad cursor -> {}."""
    if not cursor:
        return {}
    try:
        decoded = json.loads(cursor)
    except ValueError:
        return {}
    if not isinstance(decoded, dict):
        return {}
    return {str(k): str(v) for k, v in decoded.items() if str(v).strip()}


def _is_gone(exc: BaseException) -> bool:
    """Duck-typed 410 detection (syncToken expired/invalid).

    Walks the exception chain because the base GoogleConnector's error
    translation may wrap the original HttpError; HttpError carries
    ``.resp.status`` (ai/gmail.py's established duck-typing).
    """
    seen = 0
    current: BaseException | None = exc
    while current is not None and seen < 5:
        status = getattr(getattr(current, "resp", None), "status", None)
        if status is None:
            status = getattr(current, "status_code", None)
        if status == 410:
            return True
        if "sync token" in str(current).lower():
            return True  # translated errors that only keep the message
        current = current.__cause__ or current.__context__
        seen += 1
    return False


@register
class GCalConnector(GoogleConnector):
    """Google Calendar source: incremental ``events.list`` over the allowlist."""

    name = "gcal"

    def fetch(self, cursor: str | None, *, limit: int) -> FetchResult:
        """Sync the allowlisted calendars incrementally.

        A ``limit=0`` call is a no-op that preserves the cursor (returning
        ``next_cursor=None`` here would silently downgrade the next sync to a
        full re-list).
        """
        if limit <= 0:
            return FetchResult(records=[], next_cursor=cursor, reset=False)
        tokens = _parse_cursor(cursor)
        service = self._service()
        records: list[SourceRecord] = []
        next_tokens: dict[str, str] = {}
        reset = False
        for calendar_id in self._calendar_ids():
            if len(records) >= limit:
                break
            token = tokens.get(calendar_id)
            remaining = limit - len(records)
            try:
                events, sync_token = self._sync_calendar(service, calendar_id, token, remaining)
            except _SyncTokenExpired:
                # Token expired/invalid (410): drop it and fully re-list this
                # calendar. Other calendars' tokens stay valid.
                reset = True
                events, sync_token = self._sync_calendar(service, calendar_id, None, remaining)
            records.extend(self._map_events(events, calendar_id))
            if sync_token is not None:
                next_tokens[calendar_id] = sync_token
            elif token is not None:
                # The walk stopped at the limit mid-way: keep the OLD token so
                # the next run re-lists from the last complete position
                # (idempotent — the runner dedupes by external_id/version).
                next_tokens[calendar_id] = token
        next_cursor = json.dumps(next_tokens, sort_keys=True) if next_tokens else None
        return FetchResult(records=records, next_cursor=next_cursor, reset=reset)

    # -- internals ---------------------------------------------------------

    def _settings(self) -> Any:
        # Lazy: config never imports connectors (same pattern as ai/gmail.py).
        from brain.config import get_settings  # noqa: PLC0415

        return get_settings()

    def _calendar_ids(self) -> tuple[str, ...]:
        return self._settings().gcal_calendar_ids

    def _zone(self) -> ZoneInfo:
        return ZoneInfo(self._settings().timezone)

    def _map_events(self, events: list[dict[str, Any]], calendar_id: str) -> list[SourceRecord]:
        zone = self._zone()
        records: list[SourceRecord] = []
        for payload in events:
            record = record_from_event(payload, zone)
            records.append(replace(record, meta={**record.meta, "calendar_id": calendar_id}))
        return records

    def _sync_calendar(
        self, service: Any, calendar_id: str, sync_token: str | None, budget: int
    ) -> tuple[list[dict[str, Any]], str | None]:
        """Walk one calendar's events; returns (events, nextSyncToken).

        ``nextSyncToken`` is None when the walk stopped at ``budget`` before
        the source signaled a complete sync — the caller then keeps the old
        token so nothing is marked consumed that was not fully read.
        """
        events: list[dict[str, Any]] = []

        def build_request(page_token: str | None) -> Any:
            params: dict[str, Any] = {
                "calendarId": calendar_id,
                "showDeleted": True,  # cancellations must surface as deleted records
                "maxResults": min(budget, _MAX_PAGE_RESULTS),
            }
            if sync_token is not None:
                params["syncToken"] = sync_token
            if page_token:
                params["pageToken"] = page_token
            return service.events().list(**params)

        last_sync_token: str | None = None
        try:
            for page in self.iter_pages(build_request):
                events.extend(page.get("items") or [])
                if page.get("nextSyncToken"):
                    last_sync_token = str(page["nextSyncToken"])
                if len(events) >= budget and page.get("nextPageToken"):
                    # Budget reached with more pages pending: stop and cap the
                    # batch; the caller keeps the OLD token so the un-read
                    # remainder re-fetches next run (idempotent by
                    # external_id/version at the runner).
                    return events[:budget], None
        except Exception as exc:  # noqa: BLE001 — translated below
            if _is_gone(exc):
                raise _SyncTokenExpired(f"syncToken for {calendar_id!r} expired") from exc
            raise
        return events, last_sync_token
