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


def _parse_cursor(
    cursor: str | None,
) -> tuple[datetime | None, dict[str, dict[str, Any]]]:
    """Decode the gcal cursor: (frozen cutoff, {calendarId: walk state}).

    Per-calendar state: {"sync_token": str|None, "page_token": str|None,
    "mode": "full"|"incr"}; ``mode`` is derived from ``sync_token`` presence
    (``incr`` iff a sync token is held) and stored for the review-spec shape.
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
    states: dict[str, dict[str, Any]] = {}
    for key, value in calendars.items():
        if not isinstance(value, dict):
            continue  # unknown shape -> that calendar restarts from scratch
        sync_token = str(value.get("sync_token") or "").strip() or None
        page_token = str(value.get("page_token") or "").strip() or None
        states[str(key)] = {
            "sync_token": sync_token,
            "page_token": page_token,
            "mode": "incr" if sync_token is not None else "full",
        }
    cutoff: datetime | None = None
    raw_cutoff = str(decoded.get("cutoff") or "").strip()
    if raw_cutoff:
        try:
            parsed = datetime.fromisoformat(raw_cutoff)
        except ValueError:
            parsed = None
        if parsed is not None and parsed.tzinfo is not None:
            cutoff = parsed
    return cutoff, states


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
        frozen_cutoff, calendar_states = _parse_cursor(cursor)
        service = self._service()
        zone = self._zone()
        cutoff = frozen_cutoff or datetime.now(UTC) - timedelta(
            days=self._settings().brain_gcal_past_days
        )
        records: list[SourceRecord] = []
        next_states: dict[str, dict[str, Any]] = {}
        reset = False
        for calendar_id in self._calendar_ids():
            if len(records) >= limit:
                break
            state = calendar_states.get(calendar_id) or {}
            sync_token = state.get("sync_token")
            page_token = state.get("page_token")
            remaining = limit - len(records)
            try:
                events, next_state = self._walk_calendar(
                    service, calendar_id, sync_token, page_token, cutoff, zone, remaining
                )
            except _SyncTokenExpired:
                # SyncToken expired/invalid (410): drop it and fully re-list
                # this calendar from the frozen cutoff. Other calendars'
                # states stay valid.
                reset = True
                events, next_state = self._walk_calendar(
                    service, calendar_id, None, None, cutoff, zone, remaining
                )
            except ConnectorError as exc:
                if page_token is None or exc.status not in (400, 410):
                    raise
                # A STORED page_token was rejected (expired page token —
                # pageTokens are short-lived): restart THIS calendar's walk
                # from page 1 with the same mode/token (the sync guide's
                # consistency rule) and tell the runner this is a resync.
                reset = True
                events, next_state = self._walk_calendar(
                    service, calendar_id, sync_token, None, cutoff, zone, remaining
                )
            records.extend(self._map_events(events, calendar_id, zone))
            next_states[calendar_id] = next_state
        next_cursor = json.dumps(
            {"cutoff": cutoff.isoformat(), "calendars": next_states}, sort_keys=True
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

    def _walk_calendar(
        self,
        service: Any,
        calendar_id: str,
        sync_token: str | None,
        resume_page_token: str | None,
        cutoff: datetime,
        zone: ZoneInfo,
        budget: int,
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        """Walk one calendar's instances; returns (events, next walk state).

        The walk is RESUMABLE: request parameters are identical for every
        page of a walk (the sync guide's consistency rule; fixed
        ``maxResults``), only ``pageToken`` advances. The walk stops at PAGE
        boundaries only — never slicing a page mid-way, so a batch may exceed
        ``budget`` by up to one page — and the interrupted position is stored
        (``page_token``) so the next run resumes instead of restarting.

        Returned state: ``{"sync_token": <held token>, "page_token":
        <resume token or None>, "mode": "full"|"incr"}``. Only a walk whose
        last page delivered ``nextSyncToken`` switches to a held sync token;
        if the final page lacks one (should not happen per the API), the
        PREVIOUS sync token is kept so nothing is marked consumed that was
        not fully read.
        """
        events: list[dict[str, Any]] = []

        def build_request(page_token: str | None) -> Any:
            params: dict[str, Any] = {
                "calendarId": calendar_id,
                "showDeleted": True,  # cancellations must surface as deleted records
                "singleEvents": True,  # instance-per-record (see module docstring)
                # FIXED page size for the whole walk (consistency rule); the
                # batch may exceed the runner's limit by up to one page.
                "maxResults": _MAX_PAGE_RESULTS,
            }
            if sync_token is not None:
                params["syncToken"] = sync_token
            else:
                # Full sync: bound the past server-side (timeMin = exclusive
                # lower bound on the event END). Illegal with a syncToken —
                # the frozen client-side cutoff covers incremental calls.
                params["timeMin"] = cutoff.isoformat()
            token = page_token or resume_page_token  # first call resumes the walk
            if token:
                params["pageToken"] = token
            return service.events().list(**params)

        last_sync_token: str | None = None
        stop_at_page: str | None = None
        try:
            for page in self.iter_pages(build_request):
                for payload in page.get("items") or []:
                    if not _ends_before_cutoff(payload, zone, cutoff):
                        events.append(payload)
                if page.get("nextSyncToken"):
                    last_sync_token = str(page["nextSyncToken"])
                next_page = page.get("nextPageToken")
                if next_page and len(events) >= budget:
                    # Budget reached at a PAGE boundary: store the position
                    # and stop. The next run resumes from that page token —
                    # nothing is re-fetched forever, nothing is dropped.
                    stop_at_page = str(next_page)
                    break
        except ConnectorRetryableError as exc:
            # The base translation marks an expired sync cursor as a retryable
            # with status 410 (the contract's cursor-invalidation signal);
            # anything else stays a retryable for the runner's backoff.
            if exc.status != 410:
                raise
            raise _SyncTokenExpired(f"syncToken for {calendar_id!r} expired") from exc
        if stop_at_page is not None:
            return events, {
                "sync_token": sync_token,
                "page_token": stop_at_page,
                "mode": "incr" if sync_token is not None else "full",
            }
        # Complete walk: hold the new sync token (fall back to the previous
        # one if the last page surprisingly carried none — never advance to
        # "no token", that would turn the next run into a full re-list).
        return events, {
            "sync_token": last_sync_token or sync_token,
            "page_token": None,
            "mode": "incr" if (last_sync_token or sync_token) is not None else "full",
        }
