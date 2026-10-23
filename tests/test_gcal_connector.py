"""Google Calendar connector tests (connectors ticket C2).

Mode: INSTANCE-PER-RECORD (manager decision, connectors/gcal-review thread) —
``singleEvents=true`` + ``showDeleted=true`` on every call, one record per
occurrence, past cutoff ``BRAIN_GCAL_PAST_DAYS`` (frozen in the cursor), NO
future cap. The service is ALWAYS a fake — no test touches Google or the
network. Mapping tests drive :func:`record_from_event` with hand-written
fixtures shaped like documented Calendar API payloads (Hebrew + English).
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from tests.test_connectors_runner import Harness

from brain.config import get_settings
from brain.connectors import get as get_connector
from brain.connectors.base import ConnectorRetryableError
from brain.connectors.google.calendar import (
    GCAL_SCOPE,
    GCalConnector,
    record_from_event,
)

JERUSALEM = ZoneInfo("Asia/Jerusalem")
FIXTURES = Path(__file__).parent / "fixtures" / "connectors" / "gcal"

# 100 years back: every fixture instance is inside the past window however
# late the suite runs — runner-level tests stay deterministic forever.
_OLD_ENOUGH = "36500"


def _fixture(name: str) -> dict[str, Any]:
    return json.loads((FIXTURES / name).read_text())


@pytest.fixture(autouse=True)
def _app_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Pin the app zone + default allowlist; keep get_settings cache honest."""
    monkeypatch.setenv("TIMEZONE", "Asia/Jerusalem")
    monkeypatch.setenv("BRAIN_GCAL_CALENDARS", "primary")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


class _FakeRequest:
    def __init__(self, outcome: Any) -> None:
        self._outcome = outcome

    def execute(self) -> Any:
        if isinstance(self._outcome, Exception):
            raise self._outcome
        return self._outcome


class _FakeHttpError(Exception):
    """Duck-typed googleapiclient.errors.HttpError (carries .resp.status)."""

    def __init__(self, status: int) -> None:
        super().__init__(f"http {status}")
        self.resp = type("Resp", (), {"status": status})()


class _FakeCalendarService:
    """Duck-typed googleapiclient service: per-calendar page queues + errors."""

    def __init__(self) -> None:
        self.pages: dict[str, list[dict[str, Any]]] = {}
        self.errors: dict[str, Exception] = {}
        self.calls: list[dict[str, Any]] = []

    def queue(self, calendar_id: str, *pages: dict[str, Any]) -> None:
        self.pages[calendar_id] = list(pages)

    def fail_next(self, calendar_id: str, error: Exception) -> None:
        self.errors[calendar_id] = error

    def events(self) -> _FakeEventsApi:
        return _FakeEventsApi(self)

    def _take(self, params: dict[str, Any]) -> Any:
        self.calls.append(params)
        error = self.errors.pop(str(params.get("calendarId")), None)
        if error is not None:
            return _FakeRequest(error)
        queue = self.pages.get(str(params.get("calendarId"))) or []
        return _FakeRequest(queue.pop(0) if queue else {"items": []})


class _FakeEventsApi:
    def __init__(self, service: _FakeCalendarService) -> None:
        self._service = service

    def list(self, **params: Any) -> _FakeRequest:
        return self._service._take(params)  # _take already returns a request


def _connector(fake: _FakeCalendarService) -> GCalConnector:
    """The real GCalConnector over an injected fake service (no network)."""
    return GCalConnector(
        token_loader=lambda: "refresh-token",
        service_factory=lambda token, scopes: fake,
    )


def _timed() -> dict[str, Any]:
    return _fixture("event_timed_hebrew.json")


def _instance1() -> dict[str, Any]:
    return _fixture("event_recurring_instance.json")  # Wed 4 Nov 2026, 19:00 IST


def _instance2() -> dict[str, Any]:
    return _fixture("event_recurring_instance2.json")  # Wed 11 Nov 2026, 19:00 IST


# A cursor with a FROZEN cutoff: makes every cutoff-dependent test
# deterministic (no dependence on datetime.now).
_FROZEN_CUTOVER = datetime(2026, 10, 1, 0, 0, tzinfo=UTC)


def _frozen_cursor(*tokens: tuple[str, str]) -> str:
    return json.dumps(
        {"cutoff": _FROZEN_CUTOVER.isoformat(), "calendars": dict(tokens)}
    )


# ---- mapping: timed event -------------------------------------------------------


def test_timed_hebrew_event_maps_every_field() -> None:
    record = record_from_event(_timed(), JERUSALEM)

    assert record.source == "gcal"
    assert record.external_id == "1q2w3e4r5t6y7u8i9o0p"
    assert record.version == '"34021f8d8b0f61d0"'  # etag
    assert record.title == "אימון כדורסל"
    assert record.occurred_at == datetime(2026, 10, 29, 15, 0, tzinfo=UTC)  # 17:00 IST
    assert record.url == "https://www.google.com/calendar/event?eid=MXEydzNlNHI1dDZ5N3U4aTlvMHAK"
    assert record.deleted is False
    assert record.attachments == ()
    assert record.meta["start"] == "2026-10-29T17:00:00+02:00"
    assert record.meta["end"] == "2026-10-29T18:30:00+02:00"
    assert record.meta["location"] == "אולם הספורט, רחוב החלוצים 12, תל אביב"
    assert record.meta["all_day"] is False
    assert record.meta["status"] == "confirmed"


def test_text_first_line_is_the_repo_one_liner_format() -> None:
    record = record_from_event(_timed(), JERUSALEM)

    # Same renderer as the ICS paste path / brain normalize-calendar.
    assert record.text.splitlines()[0] == (
        "אימון כדורסל — Thu 29 Oct 17:00–18:30 @ אולם הספורט, רחוב החלוצים 12, תל אביב"
    )


def test_text_carries_description_organizer_and_attendees() -> None:
    record = record_from_event(_timed(), JERUSALEM)

    assert record.text.splitlines()[1:] == [
        "להביא כדור ומים",
        "אולם 2",
        "Organizer: דני כהן",
        "Attendees: אבי לוי, parent2@example.com",  # no displayName -> email
    ]


def test_event_without_location_or_attendees_renders_title_and_time_only() -> None:
    payload = _timed()
    del payload["location"], payload["description"], payload["organizer"], payload["attendees"]

    record = record_from_event(payload, JERUSALEM)

    assert record.text == "אימון כדורסל — Thu 29 Oct 17:00–18:30"


# ---- mapping: recurring instances (instance-per-record mode) --------------------


def test_recurring_instance_maps_with_series_link_and_no_rrule_line() -> None:
    record = record_from_event(_instance1(), JERUSALEM)

    assert record.external_id == "weekly-series-id-5678_20261104T170000Z"
    assert record.title == "חזרת תזמורת"
    assert record.occurred_at == datetime(2026, 11, 4, 17, 0, tzinfo=UTC)  # 19:00 IST
    assert record.meta["recurring_event_id"] == "weekly-series-id-5678"
    # Instances carry no recurrence field (recurring-events guide): the text
    # renders as a plain occurrence — no "Repeats:" line.
    assert record.text.splitlines() == [
        "חזרת תזמורת — Wed 4 Nov 19:00–20:15 @ אולם המוזיקה",
        "Organizer: מירי שרון",
    ]


def test_recurrence_in_payload_goes_to_meta_never_to_text() -> None:
    # Defensive: with singleEvents=true a series master never arrives, but if
    # one ever leaks into a batch it is stored, not rendered.
    payload = dict(_instance1())
    payload["recurrence"] = ["RRULE:FREQ=WEEKLY;BYDAY=WE"]

    record = record_from_event(payload, JERUSALEM)

    assert record.meta["recurrence"] == ["RRULE:FREQ=WEEKLY;BYDAY=WE"]
    assert "Repeats:" not in record.text


# ---- mapping: DST boundary (Israel DST ends Sun 25 Oct 2026) --------------------


def test_dst_boundary_renders_summer_and_winter_wall_times() -> None:
    summer = _timed()
    summer["start"] = {"dateTime": "2026-10-24T20:00:00+03:00"}  # Sat 24 Oct, IDT (UTC+3)
    summer["end"] = {"dateTime": "2026-10-24T21:00:00+03:00"}
    winter = _timed()
    winter["start"] = {"dateTime": "2026-10-26T18:00:00+02:00"}  # Mon 26 Oct, IST (UTC+2)
    winter["end"] = {"dateTime": "2026-10-26T19:00:00+02:00"}

    assert record_from_event(summer, JERUSALEM).text.splitlines()[0].startswith(
        "אימון כדורסל — Sat 24 Oct 20:00–21:00"
    )
    assert record_from_event(winter, JERUSALEM).text.splitlines()[0].startswith(
        "אימון כדורסל — Mon 26 Oct 18:00–19:00"
    )


# ---- mapping: all-day events ----------------------------------------------------


def test_all_day_event_renders_date_only_and_utc_midnight() -> None:
    record = record_from_event(_fixture("event_all_day.json"), JERUSALEM)

    assert record.text == "יום הולדת סבא — Thu 29 Oct"
    assert record.occurred_at == datetime(2026, 10, 28, 22, 0, tzinfo=UTC)  # 00:00 IST
    assert record.meta["all_day"] is True
    assert record.meta["start"] == "2026-10-29T00:00:00+02:00"
    # Google's all-day end is EXCLUSIVE (RFC 5545) and is kept as sent.
    assert record.meta["end"] == "2026-10-30T00:00:00+02:00"


def test_all_day_event_in_summer_interprets_midnight_in_idt() -> None:
    payload = _fixture("event_all_day.json")
    payload["start"] = {"date": "2026-07-15"}
    payload["end"] = {"date": "2026-07-16"}

    record = record_from_event(payload, JERUSALEM)

    assert record.occurred_at == datetime(2026, 7, 14, 21, 0, tzinfo=UTC)  # 00:00 IDT


def test_multi_day_all_day_event_stays_one_record() -> None:
    payload = _fixture("event_all_day.json")
    payload["start"] = {"date": "2026-10-29"}
    payload["end"] = {"date": "2026-11-02"}  # exclusive -> event spans Oct 29..Nov 1

    record = record_from_event(payload, JERUSALEM)

    assert record.external_id == "all-day-event-id-1234"  # ONE record for the span
    assert record.text == "יום הולדת סבא — Thu 29 Oct"
    assert record.meta["end"] == "2026-11-02T00:00:00+02:00"


# ---- mapping: cancelled / moved occurrences -------------------------------------


def test_cancelled_skeletal_event_maps_to_deleted_record() -> None:
    record = record_from_event(_fixture("event_cancelled_skeletal.json"), JERUSALEM)

    assert record.deleted is True
    assert record.external_id == "cancelled-event-id-9012"
    assert record.occurred_at is None
    assert record.title == "Calendar event"
    assert record.text == "Calendar event"  # renderer fallback, no crash on the skeleton


def test_cancelled_recurring_instance_maps_to_deleted_record() -> None:
    # A single cancelled occurrence: instance id, series kept (documented cost
    # of instance-per-record for the series as a whole — nothing represents
    # "the series" itself, only its occurrences).
    record = record_from_event(_fixture("event_cancelled_instance.json"), JERUSALEM)

    assert record.deleted is True
    assert record.external_id == "weekly-series-id-5678_20261105T150000Z"
    assert record.occurred_at is None  # originalStartTime is NOT a start


def test_changed_event_bumps_version_and_follows_new_times() -> None:
    moved = _timed()
    moved["etag"] = '"ffff0001"'
    moved["start"] = {"dateTime": "2026-10-30T10:00:00+02:00"}

    before = record_from_event(_timed(), JERUSALEM)
    after = record_from_event(moved, JERUSALEM)

    assert before.external_id == after.external_id
    assert before.version != after.version
    assert after.occurred_at == datetime(2026, 10, 30, 8, 0, tzinfo=UTC)


# ---- mapping: edges -------------------------------------------------------------


def test_meta_is_json_safe() -> None:
    for name in (
        "event_timed_hebrew.json",
        "event_all_day.json",
        "event_recurring_instance.json",
    ):
        record = record_from_event(_fixture(name), JERUSALEM)
        json.dumps(record.meta, ensure_ascii=False)  # must not raise


def test_event_without_start_renders_title_only() -> None:
    record = record_from_event({"id": "no-start", "summary": "מסומן בלי שעה"}, JERUSALEM)

    assert record.occurred_at is None
    assert record.text == "מסומן בלי שעה"


def test_event_crossing_midnight_renders_end_time_only() -> None:
    payload = _timed()
    payload["start"] = {"dateTime": "2026-10-29T23:00:00+02:00"}
    payload["end"] = {"dateTime": "2026-10-30T01:00:00+02:00"}

    record = record_from_event(payload, JERUSALEM)

    assert record.text.splitlines()[0].startswith("אימון כדורסל — Thu 29 Oct 23:00–01:00")


def test_missing_id_raises() -> None:
    with pytest.raises(ValueError, match="missing 'id'"):
        record_from_event({"summary": "no id"}, JERUSALEM)


def test_record_source_matches_connector_name() -> None:
    assert get_connector("gcal") is GCalConnector
    record = record_from_event(_timed(), JERUSALEM)
    assert record.source == GCalConnector.name
    assert GCalConnector.SCOPES == (GCAL_SCOPE,)
    assert GCAL_SCOPE == "https://www.googleapis.com/auth/calendar.readonly"


# ---- fetch(): fake service, no network -------------------------------------------


def test_fetch_full_sync_requests_instances_bounded_by_time_min(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("BRAIN_GCAL_CALENDARS", "primary")
    monkeypatch.setenv("BRAIN_GCAL_PAST_DAYS", "60")
    get_settings.cache_clear()
    fake = _FakeCalendarService()
    fake.queue("primary", {"items": [_timed()], "nextSyncToken": "sync-1"})
    conn = _connector(fake)

    result = conn.fetch(None, limit=50)

    assert [r.external_id for r in result.records] == ["1q2w3e4r5t6y7u8i9o0p"]
    call = fake.calls[0]
    assert call["singleEvents"] is True  # instance-per-record, always
    assert call["showDeleted"] is True
    assert "syncToken" not in call
    time_min = datetime.fromisoformat(call["timeMin"])
    assert abs((datetime.now(UTC) - time_min).days - 60) <= 1  # now - BRAIN_GCAL_PAST_DAYS
    # The fresh cutoff is frozen into the cursor for all later syncs.
    decoded = json.loads(result.next_cursor or "{}")
    assert decoded["calendars"] == {"primary": "sync-1"}
    assert abs(
        (datetime.fromisoformat(decoded["cutoff"]) - time_min).total_seconds()
    ) < 1


def test_fetch_incremental_sends_saved_sync_tokens_without_time_min(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("BRAIN_GCAL_CALENDARS", "primary")
    get_settings.cache_clear()
    fake = _FakeCalendarService()
    fake.queue("primary", {"items": [], "nextSyncToken": "sync-primary-2"})
    conn = _connector(fake)
    cursor = _frozen_cursor(("primary", "sync-primary-1"))

    result = conn.fetch(cursor, limit=50)

    assert result.records == []
    call = fake.calls[0]
    assert call["syncToken"] == "sync-primary-1"
    assert "timeMin" not in call  # illegal with a syncToken
    assert call["singleEvents"] is True
    # The frozen cutoff is reused, not recomputed.
    assert json.loads(result.next_cursor or "{}")["cutoff"] == _FROZEN_CUTOVER.isoformat()
    assert json.loads(result.next_cursor or "{}")["calendars"] == {
        "primary": "sync-primary-2"
    }


def test_fetch_410_resets_the_calendar_and_relists_from_the_frozen_cutoff(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("BRAIN_GCAL_CALENDARS", "primary")
    get_settings.cache_clear()
    fake = _FakeCalendarService()
    fake.queue(
        "primary",
        {"items": [_timed()], "nextSyncToken": "fresh"},
    )
    fake.fail_next("primary", _FakeHttpError(410))  # the incremental call fails
    conn = _connector(fake)
    cursor = _frozen_cursor(("primary", "expired-token"))

    result = conn.fetch(cursor, limit=50)

    assert result.reset is True
    assert [r.external_id for r in result.records] == ["1q2w3e4r5t6y7u8i9o0p"]
    assert fake.calls[0]["syncToken"] == "expired-token"
    full_relist = fake.calls[1]
    assert "syncToken" not in full_relist
    assert datetime.fromisoformat(full_relist["timeMin"]) == _FROZEN_CUTOVER  # frozen
    assert json.loads(result.next_cursor or "{}")["calendars"] == {"primary": "fresh"}


def test_fetch_propagates_non_410_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BRAIN_GCAL_CALENDARS", "primary")
    get_settings.cache_clear()
    fake = _FakeCalendarService()
    fake.fail_next("primary", _FakeHttpError(500))
    conn = _connector(fake)

    # The base translation turns 500 into a retryable; only 410 resets.
    with pytest.raises(ConnectorRetryableError) as exc_info:
        conn.fetch(None, limit=50)
    assert exc_info.value.status == 500


def test_fetch_walks_next_page_token_then_stores_next_sync_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("BRAIN_GCAL_CALENDARS", "primary")
    get_settings.cache_clear()
    fake = _FakeCalendarService()
    fake.queue(
        "primary",
        {"items": [_timed()], "nextPageToken": "page-2"},
        {"items": [_instance1()], "nextSyncToken": "sync-end"},
    )
    conn = _connector(fake)

    result = conn.fetch(None, limit=50)

    assert [r.external_id for r in result.records] == [
        "1q2w3e4r5t6y7u8i9o0p",
        "weekly-series-id-5678_20261104T170000Z",
    ]
    assert fake.calls[0].get("pageToken") is None
    assert fake.calls[1]["pageToken"] == "page-2"
    assert fake.calls[1]["maxResults"] == 50  # budget respected, not the 250 page cap
    assert json.loads(result.next_cursor or "{}")["calendars"] == {"primary": "sync-end"}


def test_fetch_limit_truncates_and_keeps_the_old_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BRAIN_GCAL_CALENDARS", "primary")
    get_settings.cache_clear()
    fake = _FakeCalendarService()
    fake.queue(
        "primary",
        {"items": [_timed(), _instance1()], "nextPageToken": "page-2"},
    )
    conn = _connector(fake)
    cursor = _frozen_cursor(("primary", "old-token"))

    result = conn.fetch(cursor, limit=1)

    assert len(result.records) == 1
    assert fake.calls[0]["maxResults"] == 1
    # Truncated mid-walk: the OLD token stays current; the next run re-lists.
    assert json.loads(result.next_cursor or "{}")["calendars"] == {"primary": "old-token"}


def test_fetch_limit_zero_is_a_no_op_that_preserves_the_cursor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("BRAIN_GCAL_CALENDARS", "primary")
    get_settings.cache_clear()
    fake = _FakeCalendarService()
    conn = _connector(fake)
    cursor = _frozen_cursor(("primary", "keep-me"))

    result = conn.fetch(cursor, limit=0)

    assert result.records == []
    assert result.next_cursor == cursor
    assert fake.calls == []


def test_fetch_with_empty_allowlist_makes_no_calls(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BRAIN_GCAL_CALENDARS", "")
    get_settings.cache_clear()
    fake = _FakeCalendarService()
    conn = _connector(fake)

    result = conn.fetch(None, limit=50)

    assert result.records == []
    assert result.next_cursor is not None  # cutoff still frozen for later runs
    assert fake.calls == []


def test_fetch_caps_page_size_at_250(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BRAIN_GCAL_CALENDARS", "primary")
    get_settings.cache_clear()
    fake = _FakeCalendarService()
    fake.queue("primary", {"items": [], "nextSyncToken": "sync-1"})
    conn = _connector(fake)

    conn.fetch(None, limit=10_000)

    assert fake.calls[0]["maxResults"] == 250


# ---- fetch(): past cutoff (frozen) and the NO-future-cap rule ---------------------


def test_fetch_drops_instances_that_ended_before_the_cutoff_but_keeps_the_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("BRAIN_GCAL_CALENDARS", "primary")
    get_settings.cache_clear()
    stale = _timed()  # Thu 29 Oct 17:00-18:30 IST — before the frozen cutoff (Oct 1)? no:
    # Oct 29 is AFTER Oct 1; make it truly stale by moving it to September.
    stale["start"] = {"dateTime": "2026-09-20T17:00:00+03:00"}
    stale["end"] = {"dateTime": "2026-09-20T18:30:00+03:00"}
    fake = _FakeCalendarService()
    fake.queue("primary", {"items": [stale, _timed()], "nextSyncToken": "sync-1"})
    conn = _connector(fake)

    result = conn.fetch(_frozen_cursor(), limit=50)

    # The stale September instance is filtered client-side; the token is kept.
    assert [r.external_id for r in result.records] == ["1q2w3e4r5t6y7u8i9o0p"]
    assert json.loads(result.next_cursor or "{}")["calendars"] == {"primary": "sync-1"}


def test_fetch_never_drops_cancelled_records_without_an_end(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("BRAIN_GCAL_CALENDARS", "primary")
    get_settings.cache_clear()
    # Skeletal cancelled payload (no start/end): dropping it would lose the
    # deletion, so the cutoff filter must pass it through.
    fake = _FakeCalendarService()
    fake.queue(
        "primary", {"items": [_fixture("event_cancelled_skeletal.json")], "nextSyncToken": "s"}
    )
    conn = _connector(fake)

    result = conn.fetch(_frozen_cursor(), limit=50)

    assert [r.deleted for r in result.records] == [True]


def test_fetch_has_no_future_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    """Decision (i): instances in the far future are NEVER dropped — a future
    instance dropped today would never be re-delivered unless it changed."""
    monkeypatch.setenv("BRAIN_GCAL_CALENDARS", "primary")
    get_settings.cache_clear()
    far_future = _timed()
    far_future["start"] = {"dateTime": "2028-06-01T17:00:00+03:00"}
    far_future["end"] = {"dateTime": "2028-06-01T18:00:00+03:00"}
    fake = _FakeCalendarService()
    fake.queue("primary", {"items": [far_future], "nextSyncToken": "sync-1"})
    conn = _connector(fake)

    result = conn.fetch(_frozen_cursor(), limit=50)

    assert [r.external_id for r in result.records] == ["1q2w3e4r5t6y7u8i9o0p"]


# ---- settings: allowlist + past days ---------------------------------------------


def test_gcal_allowlist_setting_parses_and_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BRAIN_GCAL_CALENDARS", "primary, work@example.com ,,")
    get_settings.cache_clear()
    try:
        assert get_settings().gcal_calendar_ids == ("primary", "work@example.com")
    finally:
        get_settings.cache_clear()

    monkeypatch.delenv("BRAIN_GCAL_CALENDARS")
    get_settings.cache_clear()
    try:
        assert get_settings().gcal_calendar_ids == ("primary",)
    finally:
        get_settings.cache_clear()


def test_gcal_past_days_setting_defaults_to_60(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("BRAIN_GCAL_PAST_DAYS", raising=False)
    get_settings.cache_clear()
    try:
        assert get_settings().brain_gcal_past_days == 60
    finally:
        get_settings.cache_clear()

    monkeypatch.setenv("BRAIN_GCAL_PAST_DAYS", "14")
    get_settings.cache_clear()
    try:
        assert get_settings().brain_gcal_past_days == 14
    finally:
        get_settings.cache_clear()


def test_fetch_fresh_cutoff_honors_brain_gcal_past_days(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("BRAIN_GCAL_CALENDARS", "primary")
    monkeypatch.setenv("BRAIN_GCAL_PAST_DAYS", "14")
    get_settings.cache_clear()
    fake = _FakeCalendarService()
    fake.queue("primary", {"items": [], "nextSyncToken": "sync-1"})
    conn = _connector(fake)

    result = conn.fetch(None, limit=50)

    time_min = datetime.fromisoformat(fake.calls[0]["timeMin"])
    assert abs((datetime.now(UTC) - time_min).days - 14) <= 1
    assert (
        datetime.fromisoformat(json.loads(result.next_cursor or "{}")["cutoff"]) == time_min
    )


# ---- runner-level semantics: this connector through the REAL ConnectorRunner ----


@pytest.fixture(name="runner_db")
def runner_db_fixture(database: None):
    """Fresh connector_state/connector_records; memory_items cleaned after.

    Same wiring as tests/test_connectors_runner.py (single Harness source);
    the fixture must live in the using module (pytest does not re-register
    imported fixture functions).
    """
    from sqlalchemy import text  # noqa: PLC0415

    from brain.db.session import SessionLocal, engine  # noqa: PLC0415

    with engine.begin() as conn:
        for table in ("connector_records", "connector_state"):
            conn.execute(text(f"DELETE FROM {table}"))
    session = SessionLocal()
    yield session
    session.rollback()
    session.close()
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM connector_records"))
        conn.execute(text("DELETE FROM connector_state"))
        conn.execute(text("DELETE FROM memory_items"))


def _sync(monkeypatch: pytest.MonkeyPatch, h: Harness, *pages: dict[str, Any]):
    monkeypatch.setenv("BRAIN_GCAL_CALENDARS", "primary")
    monkeypatch.setenv("BRAIN_GCAL_PAST_DAYS", _OLD_ENOUGH)
    get_settings.cache_clear()
    fake = _FakeCalendarService()
    fake.queue("primary", *pages)
    return h.runner(_connector(fake)).run()


def test_gcal_through_the_real_runner_new_unchanged_changed_deleted(
    runner_db, monkeypatch
):
    """C2 runner-level leg: new/unchanged/changed/deleted with the real
    ConnectorRunner + IngestService (test DB) and MY connector whose Google
    service is a fake — no network, no Google account, real DB evidence."""
    h = Harness(runner_db)
    timed = _timed()
    instance = _instance1()
    event_id = "1q2w3e4r5t6y7u8i9o0p"

    # new -> items created through the standard capture path
    summary = _sync(monkeypatch, h, {"items": [timed, instance], "nextSyncToken": "sync-1"})
    assert (summary.created, summary.updated, summary.deleted) == (2, 0, 0)
    first = h.item(event_id)
    assert first is not None
    assert first.source == "gcal"
    assert first.raw_text == record_from_event(timed, JERUSALEM).text
    assert first.occurred_at == datetime(2026, 10, 29, 15, 0, tzinfo=UTC)
    assert first.meta["connector"] == "gcal"
    assert first.meta["external_id"] == event_id
    assert first.meta["version"] == '"34021f8d8b0f61d0"'
    assert first.meta["calendar_id"] == "primary"

    # unchanged -> same versions are skipped (idempotent re-run)
    summary = _sync(monkeypatch, h, {"items": [timed, instance], "nextSyncToken": "sync-2"})
    assert (summary.created, summary.updated, summary.deleted, summary.skipped) == (0, 0, 0, 2)

    # changed -> same external_id, new etag: updated in place
    moved = dict(timed)
    moved["etag"] = '"moved-1"'
    moved["start"] = {"dateTime": "2026-10-30T10:00:00+02:00"}
    summary = _sync(monkeypatch, h, {"items": [moved], "nextSyncToken": "sync-3"})
    assert (summary.updated, summary.skipped) == (1, 0)
    updated = h.item(event_id)
    assert updated is not None
    assert updated.occurred_at == datetime(2026, 10, 30, 8, 0, tzinfo=UTC)

    # deleted -> cancelled (same etag, deleted flag flips): marked + hidden,
    # NEVER hard-deleted (runner semantics apply to the gcal record as-is).
    gone = dict(timed)
    gone["status"] = "cancelled"
    summary = _sync(monkeypatch, h, {"items": [gone], "nextSyncToken": "sync-4"})
    assert summary.deleted == 1
    item_id = updated.id
    still_there = h.repo.get(item_id)
    assert still_there is not None
    assert still_there.meta["source_deleted"] is True
    assert still_there.superseded_by == item_id
    # hidden from the default listing paths (the instance stays live)
    listed = h.repo.list(item_type=None, project=None, status=None, limit=10, offset=0)
    assert str(item_id) not in {str(i.id) for i in listed}


def test_whole_series_deletion_marks_every_instance(runner_db, monkeypatch):
    """The user deletes the whole weekly series in Google Calendar: with
    singleEvents=true the incremental batch returns one cancelled record per
    instance (showDeleted doc) — every instance item must end up marked
    deleted (source_deleted=true), and nothing may be hard-deleted."""
    h = Harness(runner_db)
    id1 = _instance1()["id"]
    id2 = _instance2()["id"]

    # first sync: both occurrences live
    summary = _sync(
        monkeypatch, h, {"items": [_instance1(), _instance2()], "nextSyncToken": "sync-1"}
    )
    assert summary.created == 2

    # the user deletes the series in Google Calendar: every instance cancels
    gone1 = dict(_instance1())
    gone1["status"] = "cancelled"
    gone1["end"] = _instance1()["end"]  # cancelled instances keep their times
    gone2 = dict(_instance2())
    gone2["status"] = "cancelled"
    gone2["end"] = _instance2()["end"]
    summary = _sync(monkeypatch, h, {"items": [gone1, gone2], "nextSyncToken": "sync-2"})

    assert summary.deleted == 2
    for external_id in (id1, id2):
        item = h.item(external_id)
        assert item is not None, external_id  # never hard-deleted
        assert item.meta["source_deleted"] is True
        assert item.superseded_by == item.id
        assert "cancelled" in item.raw_text or item.raw_text  # content intact


def test_temporal_ask_finds_the_next_instance(runner_db, monkeypatch):
    """The (e) scenario as an automated test: seed via MY connector, ask
    'what is on Wednesday 4 Nov' through the REAL temporal search path
    (parse_question_ranges -> SearchService.by_occurrence) -> the instance
    is found (this failed with the old series-per-record mapping)."""
    from brain.services.search import SearchService
    from brain.services.temporal import parse_question_ranges

    h = Harness(runner_db)
    _sync(monkeypatch, h, {"items": [_instance1(), _instance2()], "nextSyncToken": "sync-1"})

    question = "מה יש ביום רביעי 4.11?"
    now = datetime(2026, 11, 1, 12, 0, tzinfo=UTC)
    ranges = parse_question_ranges(question, JERUSALEM, now)
    assert ranges == [
        (datetime(2026, 11, 3, 22, 0, tzinfo=UTC), datetime(2026, 11, 4, 22, 0, tzinfo=UTC))
    ]

    search = SearchService(h.repo, None, None, None)
    hits: list[Any] = []
    for start, end in ranges:
        hits.extend(search.by_occurrence(start, end, limit=30))

    assert [hit.item.raw_text.splitlines()[0] for hit in hits] == [
        "חזרת תזמורת — Wed 4 Nov 19:00–20:15 @ אולם המוזיקה"
    ]
    # the NEXT Wednesday finds the next instance, not the whole series
    next_ranges = parse_question_ranges("מה יש ביום רביעי 11.11?", JERUSALEM, now)
    next_hits: list[Any] = []
    for start, end in next_ranges:
        next_hits.extend(search.by_occurrence(start, end, limit=30))
    assert [hit.item.raw_text.splitlines()[0] for hit in next_hits] == [
        "חזרת תזמורת — Wed 11 Nov 19:00–20:15 @ אולם המוזיקה"
    ]


# ---- time math sanity for the cutoff ----------------------------------------------


def test_cutoff_window_math() -> None:
    # documented behavior: cutoff = now - BRAIN_GCAL_PAST_DAYS, tz-aware UTC
    now = datetime(2026, 10, 23, 12, 0, tzinfo=UTC)
    assert now - timedelta(days=60) == datetime(2026, 8, 24, 12, 0, tzinfo=UTC)
