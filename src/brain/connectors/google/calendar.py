"""Google Calendar connector (source name ``gcal``, scope ``calendar.readonly``).

RECURRING EVENTS — instance-per-record (manager decision, connectors/gcal-review):
``events.list`` runs with ``singleEvents=true`` + ``showDeleted=true`` on every
call, so records are single one-off events and INSTANCES — the KB gets one
item per occurrence and temporal questions ("what is on Wednesday?") work
natively. Rationale and consequences:

- The sync guide's own paginated-incremental example uses
  ``singleEvents=true&syncToken=...``; the parameters that may NOT ride with
  ``syncToken`` (iCalUID, orderBy, privateExtendedProperty, q,
  sharedExtendedProperty, timeMin, timeMax, updatedMin) exclude singleEvents
  and showDeleted. The SAME parameter set is used on full and incremental
  calls (the guide's consistency rule), minus the banned ones.
- A deleted whole series arrives as one cancelled record per instance
  (showDeleted+singleEvents doc), so every instance item is correctly marked
  ``deleted=True``. Series masters never appear (singleEvents=true excludes
  them) — no double representation. Instances carry no ``recurrence`` field;
  if one ever appears in a payload it is kept in ``meta`` only, never in
  ``text``.
- Instance ids (``<seriesId>_<start>``) are the external_ids; a moved
  occurrence arrives as a cancelled old-id record + a new-id record, which
  the runner applies as mark-deleted + create.

PAST CUTOFF (``BRAIN_GCAL_PAST_DAYS``, default 60): the initial full sync
adds ``timeMin = now - past_days`` (legal on a full sync; banned with
``syncToken``), and a client-side filter drops instances that ENDED before
that cutoff from every batch (incremental syncs can still deliver changes
for old instances because timeMin cannot ride along). The cutoff is frozen
at the first sync and persisted inside the cursor.

NO client-side FUTURE cap, ever: a future instance dropped today would never
be re-delivered unless it changed again, so it would be silently lost when
its date finally arrives. Only the past cutoff is safe, because the past
never becomes relevant again.

Cursor: JSON ``{"cutoff": "<iso instant>", "calendars": {calendarId:
nextSyncToken}}`` — one token per allowlisted calendar (``BRAIN_GCAL_CALENDARS``,
default ``"primary"``). When a calendar's token is rejected (HTTP 410 —
expired), that calendar is fully re-listed without a token (``timeMin``
re-applied from the frozen cutoff) and the batch reports ``reset=True``.
A bad/absent cursor means full resync with a fresh cutoff.

All-day events (``date``, no ``dateTime``) are interpreted in the app
timezone and rendered date-only; ``meta["end"]`` keeps Google's exclusive
end date exactly as sent (RFC 5545 semantics, same as the ICS paste path).
``occurred_at`` is the event start (aware, canonical UTC).

Text renders in the app timezone (``TIMEZONE``, Asia/Jerusalem in
production) reusing the repo's existing calendar one-liner
(``brain.services.paste._calendar_one_liner``, the same renderer the ICS
paste path and ``brain normalize-calendar`` use), then adds description,
organizer and attendee display names:

    <title> — Thu 29 Oct 17:00–18:30 @ <location>
    <description>
    Organizer: <name>
    Attendees: <name>, <name>

(The ICS paste path still stores a recurring series as one item dated at its
start — known mismatch, backlog ticket; do not "fix" it here.)

Events carry no byte attachments in this scope (event file attachments are
Drive files; fetching them needs ``drive.readonly`` — out of this ticket).
"""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any, ClassVar
from zoneinfo import ZoneInfo

from brain.connectors import register
from brain.connectors.base import ConnectorError, ConnectorRetryableError, FetchResult, SourceRecord
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


def _ends_before_cutoff(payload: dict[str, Any], tz: ZoneInfo, cutoff: datetime) -> bool:
    """True when the instance definitely ended before the past cutoff.

    Records without a parseable end (e.g. skeletal cancelled instances) are
    ALWAYS kept: dropping them would silently lose deletions. See the module
    docstring — the past cutoff is the only client-side filter; there is no
    future cap (a dropped future instance would never come back).
    """
    end, _ = _event_instant(payload.get("end"), tz)
    return end is not None and end <= cutoff


def record_from_event(payload: dict[str, Any], tz: ZoneInfo) -> SourceRecord:
    """Map one Calendar API event payload (one-off event or instance)
    -> :class:`SourceRecord` (pure).

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
    version = str(payload.get("etag") or payload.get("updated") or "") or None
    url = str(payload.get("htmlLink") or "") or None

    meta: dict[str, Any] = {"title": title, "all_day": all_day}
    if start is not None:
        meta["start"] = start.isoformat()
    if end is not None:
        meta["end"] = end.isoformat()  # all-day: exclusive end date, as Google sends it
    if location:
        meta["location"] = location
    # Instances never carry a recurrence field (recurring-events guide); if a
    # payload ever has one, keep it for debugging — but out of the text.
    recurrence = [str(r).strip() for r in (payload.get("recurrence") or []) if str(r).strip()]
    if recurrence:
        meta["recurrence"] = recurrence
    if payload.get("recurringEventId"):
        meta["recurring_event_id"] = str(payload["recurringEventId"])
    status = str(payload.get("status") or "").strip()
    if status:
        meta["status"] = status

    lines = [_calendar_one_liner(meta, tz, date_only=all_day)]
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


def _parse_cursor(cursor: str | None) -> tuple[datetime | None, dict[str, str]]:
    """Decode the gcal cursor: (frozen cutoff, {calendarId: syncToken}).

    A bad/absent cursor means full resync: (None, {}).
    """
    if not cursor:
        return None, {}
    try:
        decoded = json.loads(cursor)
    except ValueError:
        return None, {}
    if not isinstance(decoded, dict):
        return None, {}
    calendars = decoded.get("calendars")
    if not isinstance(calendars, dict):
        calendars = {}
    tokens = {str(k): str(v) for k, v in calendars.items() if str(v).strip()}
    cutoff: datetime | None = None
    raw_cutoff = str(decoded.get("cutoff") or "").strip()
    if raw_cutoff:
        try:
            parsed = datetime.fromisoformat(raw_cutoff)
        except ValueError:
            parsed = None
        if parsed is not None and parsed.tzinfo is not None:
            cutoff = parsed
    return cutoff, tokens


@register
class GCalConnector(GoogleConnector):
    """Google Calendar source: instance-per-record ``events.list`` sync."""

    name = "gcal"

    # The base error translation (google/base.py, single source of truth)
    # turns an expired sync cursor (HTTP 410) into a retryable with
    # ``.status == 410``; fetch() catches exactly that and reports reset=True.
    SCOPES: ClassVar[tuple[str, ...]] = (GCAL_SCOPE,)

    def fetch(self, cursor: str | None, *, limit: int) -> FetchResult:
        """Sync the allowlisted calendars incrementally, instance-per-record.

        A ``limit=0`` call is a no-op that preserves the cursor (returning
        ``next_cursor=None`` here would silently downgrade the next sync to a
        full re-list).
        """
        if limit <= 0:
            return FetchResult(records=[], next_cursor=cursor, reset=False)
        frozen_cutoff, tokens = _parse_cursor(cursor)
        service = self._service()
        zone = self._zone()
        cutoff = frozen_cutoff or datetime.now(UTC) - timedelta(
            days=self._settings().brain_gcal_past_days
        )
        records: list[SourceRecord] = []
        next_tokens: dict[str, str] = {}
        reset = False
        for calendar_id in self._calendar_ids():
            if len(records) >= limit:
                break
            token = tokens.get(calendar_id)
            remaining = limit - len(records)
            try:
                events, sync_token = self._sync_calendar(
                    service, calendar_id, token, cutoff, zone, remaining
                )
            except _SyncTokenExpired:
                # Token expired/invalid (410): drop it and fully re-list this
                # calendar from the frozen cutoff. Other calendars' tokens
                # stay valid.
                reset = True
                events, sync_token = self._sync_calendar(
                    service, calendar_id, None, cutoff, zone, remaining
                )
            records.extend(self._map_events(events, calendar_id, zone))
            if sync_token is not None:
                next_tokens[calendar_id] = sync_token
            elif token is not None:
                # The walk stopped at the limit mid-way: keep the OLD token so
                # the next run re-lists from the last complete position
                # (idempotent — the runner dedupes by external_id/version).
                next_tokens[calendar_id] = token
        next_cursor = json.dumps(
            {"cutoff": cutoff.isoformat(), "calendars": next_tokens}, sort_keys=True
        )
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

    def _map_events(
        self, events: list[dict[str, Any]], calendar_id: str, zone: ZoneInfo
    ) -> list[SourceRecord]:
        records: list[SourceRecord] = []
        for payload in events:
            record = record_from_event(payload, zone)
            records.append(replace(record, meta={**record.meta, "calendar_id": calendar_id}))
        return records

    def _sync_calendar(
        self,
        service: Any,
        calendar_id: str,
        sync_token: str | None,
        cutoff: datetime,
        zone: ZoneInfo,
        budget: int,
    ) -> tuple[list[dict[str, Any]], str | None]:
        """Walk one calendar's instances; returns (events, nextSyncToken).

        ``singleEvents=true`` + ``showDeleted=true`` always (instance-per-
        record; parameter set identical on full and incremental calls per the
        sync guide). The full sync bounds the past server-side with
        ``timeMin`` (banned only WITH a syncToken); the client-side cutoff
        filter covers incremental deliveries of old instances. Dropped
        records do not consume budget.

        ``nextSyncToken`` is None when the walk stopped at ``budget`` before
        the source signaled a complete sync — the caller then keeps the old
        token so nothing is marked consumed that was not fully read.
        """
        events: list[dict[str, Any]] = []

        def build_request(page_token: str | None) -> Any:
            params: dict[str, Any] = {
                "calendarId": calendar_id,
                "showDeleted": True,  # cancellations must surface as deleted records
                "singleEvents": True,  # instance-per-record (see module docstring)
                "maxResults": min(budget, _MAX_PAGE_RESULTS),
            }
            if sync_token is not None:
                params["syncToken"] = sync_token
            else:
                # Full sync: bound the past server-side (timeMin = exclusive
                # lower bound on the event END). Illegal with a syncToken —
                # the frozen client-side cutoff covers incremental calls.
                params["timeMin"] = cutoff.isoformat()
            if page_token:
                params["pageToken"] = page_token
            return service.events().list(**params)

        last_sync_token: str | None = None
        try:
            for page in self.iter_pages(build_request):
                for payload in page.get("items") or []:
                    if not _ends_before_cutoff(payload, zone, cutoff):
                        events.append(payload)
                if page.get("nextSyncToken"):
                    last_sync_token = str(page["nextSyncToken"])
                if len(events) >= budget and page.get("nextPageToken"):
                    # Budget reached with more pages pending: stop and cap the
                    # batch; the caller keeps the OLD token so the un-read
                    # remainder re-fetches next run (idempotent by
                    # external_id/version at the runner).
                    return events[:budget], None
        except ConnectorRetryableError as exc:
            # The base translation marks an expired sync cursor as a retryable
            # with status 410 (the contract's cursor-invalidation signal);
            # anything else stays a retryable for the runner's backoff.
            if exc.status != 410:
                raise
            raise _SyncTokenExpired(f"syncToken for {calendar_id!r} expired") from exc
        return events, last_sync_token
