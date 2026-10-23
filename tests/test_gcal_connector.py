"""Google Calendar connector tests (connectors ticket C2).

The service is ALWAYS a fake — no test touches Google or the network. Mapping
tests drive :func:`record_from_event` with hand-written fixtures shaped like
documented Calendar API payloads (Hebrew + English); fetch() tests inject the
fake at the documented seam (``self._service()``) and monkeypatch
``iter_pages`` with the contract's documented walk until the base framework
implements it (worker_8 step 2). Runner-level semantics (new/unchanged/
changed/deleted) are covered once ``brain.connectors.testing`` lands.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import UTC, datetime
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


def _two_calendar_pages() -> dict[str, list[dict[str, Any]]]:
    timed = _fixture("event_timed_hebrew.json")
    return {
        "primary": [
            {"items": [timed], "nextSyncToken": "sync-primary-1"},
        ],
        "work@example.com": [
            {"items": [_fixture("event_recurring_series.json")], "nextSyncToken": "sync-work-9"},
        ],
    }


# ---- mapping: timed event -------------------------------------------------------


def test_timed_hebrew_event_maps_every_field() -> None:
    record = record_from_event(_fixture("event_timed_hebrew.json"), JERUSALEM)

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
    record = record_from_event(_fixture("event_timed_hebrew.json"), JERUSALEM)

    # Same renderer as the ICS paste path / brain normalize-calendar.
    assert record.text.splitlines()[0] == (
        "אימון כדורסל — Thu 29 Oct 17:00–18:30 @ אולם הספורט, רחוב החלוצים 12, תל אביב"
    )


def test_text_carries_description_organizer_and_attendees() -> None:
    record = record_from_event(_fixture("event_timed_hebrew.json"), JERUSALEM)

    assert record.text.splitlines()[1:] == [
        "להביא כדור ומים",
        "אולם 2",
        "Organizer: דני כהן",
        "Attendees: אבי לוי, parent2@example.com",  # no displayName -> email
    ]


def test_event_without_location_or_attendees_renders_title_and_time_only() -> None:
    payload = _fixture("event_timed_hebrew.json")
    del payload["location"], payload["description"], payload["organizer"], payload["attendees"]

    record = record_from_event(payload, JERUSALEM)

    assert record.text == "אימון כדורסל — Thu 29 Oct 17:00–18:30"


# ---- mapping: DST boundary (Israel DST ends Sun 25 Oct 2026) --------------------


def test_dst_boundary_renders_summer_and_winter_wall_times() -> None:
    summer = _fixture("event_timed_hebrew.json")
    summer["start"] = {"dateTime": "2026-10-24T20:00:00+03:00"}  # Sat 24 Oct, IDT (UTC+3)
    summer["end"] = {"dateTime": "2026-10-24T21:00:00+03:00"}
    winter = _fixture("event_timed_hebrew.json")
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


# ---- mapping: recurring events --------------------------------------------------


def test_recurring_series_is_one_record_with_rrule_in_text() -> None:
    # Decision (connectors contract): one record per SERIES, raw RRULE in
    # text — syncToken incremental sync never expands instances, so per-
    # occurrence records would break stable external_id across sync modes.
    record = record_from_event(_fixture("event_recurring_series.json"), JERUSALEM)

    assert record.external_id == "weekly-series-id-5678"
    assert record.text.splitlines() == [
        "חזרת תזמורת — Thu 29 Oct 17:00–18:15 @ אולם המוזיקה",
        "Repeats: RRULE:FREQ=WEEKLY;BYDAY=TH;COUNT=12",
        "Organizer: מירי שרון",
    ]
    assert record.meta["recurrence"] == ["RRULE:FREQ=WEEKLY;BYDAY=TH;COUNT=12"]
    assert record.occurred_at == datetime(2026, 10, 29, 15, 0, tzinfo=UTC)  # series start


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
    # of the series-as-record decision — flagged to the runner owner).
    record = record_from_event(_fixture("event_cancelled_instance.json"), JERUSALEM)

    assert record.deleted is True
    assert record.external_id == "weekly-series-id-5678_20261105T150000Z"
    assert record.occurred_at is None  # originalStartTime is NOT a start


def test_changed_event_bumps_version_and_follows_new_times() -> None:
    moved = _fixture("event_timed_hebrew.json")
    moved["etag"] = '"ffff0001"'
    moved["start"] = {"dateTime": "2026-10-30T10:00:00+02:00"}

    before = record_from_event(_fixture("event_timed_hebrew.json"), JERUSALEM)
    after = record_from_event(moved, JERUSALEM)

    assert before.external_id == after.external_id
    assert before.version != after.version
    assert after.occurred_at == datetime(2026, 10, 30, 8, 0, tzinfo=UTC)


# ---- mapping: edges -------------------------------------------------------------


def test_meta_is_json_safe() -> None:
    for name in (
        "event_timed_hebrew.json",
        "event_all_day.json",
        "event_recurring_series.json",
    ):
        record = record_from_event(_fixture(name), JERUSALEM)
        json.dumps(record.meta, ensure_ascii=False)  # must not raise


def test_event_without_start_renders_title_only() -> None:
    record = record_from_event({"id": "no-start", "summary": "מסומן בלי שעה"}, JERUSALEM)

    assert record.occurred_at is None
    assert record.text == "מסומן בלי שעה"


def test_event_crossing_midnight_renders_end_time_only() -> None:
    payload = _fixture("event_timed_hebrew.json")
    payload["start"] = {"dateTime": "2026-10-29T23:00:00+02:00"}
    payload["end"] = {"dateTime": "2026-10-30T01:00:00+02:00"}

    record = record_from_event(payload, JERUSALEM)

    assert record.text.splitlines()[0].startswith("אימון כדורסל — Thu 29 Oct 23:00–01:00")


def test_missing_id_raises() -> None:
    with pytest.raises(ValueError, match="missing 'id'"):
        record_from_event({"summary": "no id"}, JERUSALEM)


def test_record_source_matches_connector_name() -> None:
    assert get_connector("gcal") is GCalConnector
    record = record_from_event(_fixture("event_timed_hebrew.json"), JERUSALEM)
    assert record.source == GCalConnector.name
    assert GCalConnector.SCOPES == (GCAL_SCOPE,)
    assert GCAL_SCOPE == "https://www.googleapis.com/auth/calendar.readonly"


# ---- fetch(): fake service, no network -------------------------------------------


def test_fetch_full_sync_returns_records_and_cursor_with_both_tokens(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("BRAIN_GCAL_CALENDARS", "primary, work@example.com")
    get_settings.cache_clear()
    fake = _FakeCalendarService()
    for calendar_id, pages in _two_calendar_pages().items():
        fake.queue(calendar_id, *pages)
    conn = _connector(fake)

    result = conn.fetch(None, limit=50)

    assert [r.external_id for r in result.records] == [
        "1q2w3e4r5t6y7u8i9o0p",
        "weekly-series-id-5678",
    ]
    assert result.reset is False
    assert result.next_cursor is not None
    assert json.loads(result.next_cursor) == {
        "primary": "sync-primary-1",
        "work@example.com": "sync-work-9",
    }
    # meta carries the source calendar (dataclasses.replace injection)
    assert result.records[0].meta["calendar_id"] == "primary"
    assert result.records[1].meta["calendar_id"] == "work@example.com"


def test_fetch_incremental_sends_saved_sync_tokens(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BRAIN_GCAL_CALENDARS", "primary")
    get_settings.cache_clear()
    fake = _FakeCalendarService()
    fake.queue("primary", {"items": [], "nextSyncToken": "sync-primary-2"})
    conn = _connector(fake)
    cursor = json.dumps({"primary": "sync-primary-1"})

    result = conn.fetch(cursor, limit=50)

    assert result.records == []
    assert fake.calls[0]["syncToken"] == "sync-primary-1"
    assert json.loads(result.next_cursor or "{}") == {"primary": "sync-primary-2"}


def test_fetch_410_resets_the_calendar_and_relists_without_sync_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("BRAIN_GCAL_CALENDARS", "primary")
    get_settings.cache_clear()
    fake = _FakeCalendarService()
    fake.queue(
        "primary",
        {"items": [_fixture("event_timed_hebrew.json")], "nextSyncToken": "fresh"},
    )
    fake.fail_next("primary", _FakeHttpError(410))  # the incremental call fails
    conn = _connector(fake)
    cursor = json.dumps({"primary": "expired-token"})

    result = conn.fetch(cursor, limit=50)

    assert result.reset is True
    assert [r.external_id for r in result.records] == ["1q2w3e4r5t6y7u8i9o0p"]
    assert fake.calls[0]["syncToken"] == "expired-token"
    assert "syncToken" not in fake.calls[1]  # full re-list of that calendar
    assert json.loads(result.next_cursor or "{}") == {"primary": "fresh"}


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
        {"items": [_fixture("event_timed_hebrew.json")], "nextPageToken": "page-2"},
        {"items": [_fixture("event_recurring_series.json")], "nextSyncToken": "sync-end"},
    )
    conn = _connector(fake)

    result = conn.fetch(None, limit=50)

    assert [r.external_id for r in result.records] == [
        "1q2w3e4r5t6y7u8i9o0p",
        "weekly-series-id-5678",
    ]
    assert fake.calls[0].get("pageToken") is None
    assert fake.calls[1]["pageToken"] == "page-2"
    assert fake.calls[1]["maxResults"] == 50  # budget respected, not the 250 page cap
    assert json.loads(result.next_cursor or "{}") == {"primary": "sync-end"}


def test_fetch_limit_truncates_and_keeps_the_old_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BRAIN_GCAL_CALENDARS", "primary")
    get_settings.cache_clear()
    fake = _FakeCalendarService()
    fake.queue(
        "primary",
        {
            "items": [_fixture("event_timed_hebrew.json"), _fixture("event_recurring_series.json")],
            "nextPageToken": "page-2",
        },
    )
    conn = _connector(fake)
    cursor = json.dumps({"primary": "old-token"})

    result = conn.fetch(cursor, limit=1)

    assert len(result.records) == 1
    assert fake.calls[0]["maxResults"] == 1
    # Truncated mid-walk: the OLD token stays current; the next run re-lists.
    assert json.loads(result.next_cursor or "{}") == {"primary": "old-token"}


def test_fetch_limit_zero_is_a_no_op_that_preserves_the_cursor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("BRAIN_GCAL_CALENDARS", "primary")
    get_settings.cache_clear()
    fake = _FakeCalendarService()
    conn = _connector(fake)
    cursor = json.dumps({"primary": "keep-me"})

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
    assert result.next_cursor is None
    assert fake.calls == []


def test_fetch_caps_page_size_at_250(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BRAIN_GCAL_CALENDARS", "primary")
    get_settings.cache_clear()
    fake = _FakeCalendarService()
    fake.queue("primary", {"items": [], "nextSyncToken": "sync-1"})
    conn = _connector(fake)

    conn.fetch(None, limit=10_000)

    assert fake.calls[0]["maxResults"] == 250


# ---- settings: allowlist ---------------------------------------------------------


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


def test_gcal_through_the_real_runner_new_unchanged_changed_deleted(runner_db):
    """C2 runner-level leg: new/unchanged/changed/deleted with the real
    ConnectorRunner + IngestService (test DB) and MY connector whose Google
    service is a fake — no network, no Google account, real DB evidence."""
    h = Harness(runner_db)
    timed = _fixture("event_timed_hebrew.json")
    recurring = _fixture("event_recurring_series.json")
    event_id = "1q2w3e4r5t6y7u8i9o0p"

    def sync_with(*pages: dict[str, Any]):
        fake = _FakeCalendarService()
        fake.queue("primary", *pages)
        return h.runner(_connector(fake)).run()

    # new -> items created through the standard capture path
    summary = sync_with({"items": [timed, recurring], "nextSyncToken": "sync-1"})
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
    summary = sync_with({"items": [timed, recurring], "nextSyncToken": "sync-2"})
    assert (summary.created, summary.updated, summary.deleted, summary.skipped) == (0, 0, 0, 2)

    # changed -> same external_id, new etag: updated in place
    moved = dict(timed)
    moved["etag"] = '"moved-1"'
    moved["start"] = {"dateTime": "2026-10-30T10:00:00+02:00"}
    summary = sync_with({"items": [moved], "nextSyncToken": "sync-3"})
    assert (summary.updated, summary.skipped) == (1, 0)
    updated = h.item(event_id)
    assert updated is not None
    assert updated.occurred_at == datetime(2026, 10, 30, 8, 0, tzinfo=UTC)

    # deleted -> cancelled (same etag, deleted flag flips): marked + hidden,
    # NEVER hard-deleted (runner semantics apply to the gcal record as-is).
    gone = dict(timed)
    gone["status"] = "cancelled"
    summary = sync_with({"items": [gone], "nextSyncToken": "sync-4"})
    assert summary.deleted == 1
    item_id = updated.id
    still_there = h.repo.get(item_id)
    assert still_there is not None
    assert still_there.meta["source_deleted"] is True
    assert still_there.superseded_by == item_id
    # hidden from the default listing paths (the recurring series stays live)
    listed = h.repo.list(item_type=None, project=None, status=None, limit=10, offset=0)
    assert str(item_id) not in {str(i.id) for i in listed}
