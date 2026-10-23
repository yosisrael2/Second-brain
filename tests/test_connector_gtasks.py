"""Google Tasks connector (gtasks) — mapping, cursor and fetch behavior.

Tests are red-first for feature/connectors-gtasks. No test touches the
network or the google libraries: the service is a duck-typed fake that
serves the hand-written fixtures under tests/fixtures/connectors/gtasks/
(realistic users.tasks.list / tasklists.list response shapes).
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from brain.connectors.base import ConnectorError, ConnectorRetryableError
from brain.connectors.google.tasks import (
    GTASKS_SCOPE,
    GTasksConnector,
    build_cursor,
    parse_cursor,
    parse_list_allowlist,
    record_from_task,
    select_lists,
)

TZ = ZoneInfo("Asia/Jerusalem")
FIXTURES = Path(__file__).parent / "fixtures" / "connectors" / "gtasks"

PERSONAL = "MDAwMDAwMDAwMDAwMDAwMDAwMDA6MDow"
HOME = "MTExMTExMTExMTExMTExMTExMTE6MDow"
WORK = "MjIyMjIyMjIyMjIyMjIyMjIyMjI6MDow"


def _fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


def _task(name: str, task_id: str) -> dict:
    """One task dict from a fixture page (by id)."""
    page = _fixture(name)
    return next(t for t in page["items"] if t["id"] == task_id)


# --- fake service (duck-typed googleapiclient; no google import) -------------


class FakeHttpError(Exception):
    """Duck-typed googleapiclient.errors.HttpError: carries .resp.status."""

    def __init__(self, status: int, message: str = "") -> None:
        super().__init__(message or f"http {status}")
        self.resp = SimpleNamespace(status=status)


class _FakeRequest:
    def __init__(self, svc: FakeTasksService, kind: str, params: dict) -> None:
        self._svc = svc
        self._kind = kind
        self._params = params

    def execute(self) -> dict:
        if self._kind == "tasklists":
            self._svc.tasklists_params.append(self._params)
            if self._svc.tasklists_error:
                raise FakeHttpError(self._svc.tasklists_error)
            pages = self._svc.tasklists_pages
            return pages.pop(0) if pages else {"items": []}
        self._svc.tasks_params.append(self._params)
        list_id = self._params["tasklist"]
        status = self._svc.tasks_errors.pop(list_id, None)
        if status:
            raise FakeHttpError(status, f"list {list_id} fails with {status}")
        pages = self._svc.tasks_pages.get(list_id)
        if pages is None:  # a list that no longer exists
            raise FakeHttpError(404, f"list {list_id} not found")
        return pages.pop(0) if pages else {"items": []}


class _FakeCollection:
    def __init__(self, svc: FakeTasksService, kind: str) -> None:
        self._svc = svc
        self._kind = kind

    def list(self, **params: object) -> _FakeRequest:
        return _FakeRequest(self._svc, self._kind, params)


class FakeTasksService:
    """Serves canned pages per call, recording list params for assertions."""

    def __init__(
        self,
        *,
        tasklists_pages: list[dict] | None = None,
        tasks_pages: dict[str, list[dict]] | None = None,
        tasklists_error: int | None = None,
        tasks_errors: dict[str, int] | None = None,
    ) -> None:
        self.tasklists_pages = list(tasklists_pages or [])
        self.tasks_pages = {k: list(v) for k, v in (tasks_pages or {}).items()}
        self.tasklists_error = tasklists_error
        self.tasks_errors = dict(tasks_errors or {})
        self.tasklists_params: list[dict] = []
        self.tasks_params: list[dict] = []

    def tasklists(self) -> _FakeCollection:
        return _FakeCollection(self, "tasklists")

    def tasks(self) -> _FakeCollection:
        return _FakeCollection(self, "tasks")


def _connector(service: FakeTasksService, *, allowlist: str = "") -> GTasksConnector:
    """Build a connector around a fake service without the OAuth wiring —
    instance attributes stand in for the base __init__ state."""
    conn = object.__new__(GTasksConnector)
    conn._lists_allowlist = allowlist
    conn._service = lambda: service  # type: ignore[method-assign]
    return conn


def _all_lists_pages() -> list[dict]:
    return [_fixture("tasklists_page1.json"), _fixture("tasklists_page2.json")]


def _personal_pages() -> list[dict]:
    return [_fixture("tasks_personal_page1.json"), _fixture("tasks_personal_page2.json")]


# --- pure mapping ------------------------------------------------------------


def test_open_task_with_due_maps_due_to_local_midnight():
    record = record_from_task(_task("tasks_personal_page1.json", "task-order-cake"), TZ)
    assert record.source == "gtasks"
    assert record.external_id == "task-order-cake"
    assert record.version == "2026-03-10T09:30:00.000Z"
    assert record.title == "Order birthday cake"
    assert record.url == "https://tasks.google.com/task/task-order-cake"
    assert record.deleted is False
    # Google drops the time: due is date-only -> local midnight, tz-aware.
    assert record.occurred_at == datetime(2026, 3, 15, 0, 0, tzinfo=TZ)
    assert (
        record.occurred_at is not None
        and record.occurred_at.utcoffset().total_seconds() == 2 * 3600
    )
    assert record.meta["due_date_only"] is True
    assert record.meta["status"] == "needsAction"
    assert "Order birthday cake" in record.text
    assert "Due: 2026-03-15" in record.text


def test_due_stored_with_offset_still_yields_picked_date():
    # If Google ever returns local-midnight with an offset, the picked date is
    # the date part in the timestamp's OWN offset — still March 15 local
    # midnight, never shifted a day by a UTC conversion.
    record = record_from_task(
        {
            "id": "t",
            "title": "x",
            "updated": "2026-03-01T00:00:00Z",
            "due": "2026-03-15T00:00:00+02:00",
        },
        TZ,
    )
    assert record.occurred_at == datetime(2026, 3, 15, 0, 0, tzinfo=TZ)


def test_task_without_due_has_no_occurred_at():
    record = record_from_task(_task("tasks_personal_page2.json", "task-no-due"), TZ)
    assert record.occurred_at is None
    assert "due_date_only" not in record.meta
    assert record.meta["status"] == "needsAction"


def test_completed_task_is_a_record_not_deleted():
    record = record_from_task(_task("tasks_personal_page1.json", "task-gifts"), TZ)
    assert record.deleted is False
    assert record.meta["status"] == "completed"
    assert record.meta["completed_at"] == "2026-03-11T14:05:00+00:00"
    assert "Completed: 2026-03-11 16:05 (Asia/Jerusalem)" in record.text


def test_deleted_task_maps_to_deleted_record():
    record = record_from_task(_task("tasks_personal_page1.json", "task-deleted-old"), TZ)
    assert record.deleted is True
    assert record.external_id == "task-deleted-old"
    assert record.version == "2026-03-07T12:00:00.000Z"


def test_hidden_task_marks_meta_hidden():
    record = record_from_task(_task("tasks_home_page1.json", "task-home-shed"), TZ)
    assert record.meta["hidden"] is True


def test_completed_and_hidden_stays_completed():
    # hidden+completed (cleared) is still a completed record, not deleted.
    record = record_from_task(_task("tasks_home_page1.json", "task-home-shed"), TZ)
    assert record.deleted is False
    assert record.meta["status"] == "completed"
    assert record.meta["hidden"] is True


def test_subtask_carries_parent_id_and_parent_title():
    record = record_from_task(
        _task("tasks_personal_page1.json", "task-sub-signup"),
        TZ,
        list_id=PERSONAL,
        list_title="Personal",
        parent_title="School week prep",
    )
    assert record.meta["parent_external_id"] == "task-parent-school"
    assert "Part of: School week prep" in record.text
    assert "הרשמה לחוג כדורסל" in record.text
    assert "List: Personal" in record.text


def test_subtask_with_unknown_parent_falls_back_to_id_in_text():
    task = _task("tasks_personal_page1.json", "task-sub-signup")
    record = record_from_task(task, TZ)
    assert record.meta["parent_external_id"] == "task-parent-school"
    assert "Part of: task-parent-school" in record.text


def test_notes_and_links_rendered_and_in_meta():
    record = record_from_task(_task("tasks_personal_page1.json", "task-order-cake"), TZ)
    assert "Chocolate, feeds 12." in record.text
    assert "Pickup before 16:00." in record.text
    assert "Links:" in record.text
    assert "- Bakery order: https://example.com/bakery/order/123" in record.text
    assert record.meta["links"] == [
        {
            "type": "related",
            "description": "Bakery order",
            "url": "https://example.com/bakery/order/123",
        }
    ]


def test_link_without_description_renders_url_only():
    record = record_from_task(_task("tasks_home_page1.json", "task-home-shed"), TZ)
    assert record.meta["links"] == [
        {"type": "related", "url": "https://example.com/shelves/ikea-bror"}
    ]
    assert "- https://example.com/shelves/ikea-bror" in record.text


def test_hebrew_task_round_trips():
    record = record_from_task(
        _task("tasks_home_page1.json", "task-home-tap"),
        TZ,
        list_id=HOME,
        list_title="בית",
    )
    assert record.title == "לתקן את ברז המטבח"
    assert "List: בית" in record.text
    assert "להזמין שירות. טלפון 02-555-1234." in record.text
    assert record.meta["list_title"] == "בית"


def test_task_without_title_gets_placeholder():
    record = record_from_task({"id": "t1", "updated": "2026-03-01T00:00:00Z"}, TZ)
    assert record.title == "(untitled task)"


def test_completed_at_missing_still_completed_status():
    record = record_from_task(
        {"id": "t1", "updated": "2026-03-01T00:00:00Z", "status": "completed"}, TZ
    )
    assert record.meta["status"] == "completed"
    assert "completed_at" not in record.meta


def test_garbage_completed_timestamp_ignored():
    record = record_from_task(
        {
            "id": "t1",
            "updated": "2026-03-01T00:00:00Z",
            "status": "completed",
            "completed": "not-a-date",
        },
        TZ,
    )
    assert "completed_at" not in record.meta
    assert "Completed:" not in record.text


# --- allowlist + cursor helpers ----------------------------------------------


def test_parse_list_allowlist_sorted_unique():
    assert parse_list_allowlist(" ב, a, , a") == ("a", "ב")
    assert parse_list_allowlist("") == ()


def test_select_lists_empty_allowlist_selects_all():
    lists = _fixture("tasklists_page1.json")["items"]
    assert select_lists(lists, ()) == lists


def test_select_lists_by_id_and_case_insensitive_title():
    lists = _all_lists_pages()[0]["items"]
    by_id = select_lists(lists, ("MTExMTExMTExMTExMTExMTExMTE6MDow",))
    assert [lst["title"] for lst in by_id] == ["בית"]
    by_title = select_lists(lists, ("personal",))
    assert [lst["id"] for lst in by_title] == [PERSONAL]


def test_cursor_roundtrip_and_fingerprint():
    cursor = build_cursor("2026-03-10T09:30:00.000Z", ("Personal",))
    updated, lists = parse_cursor(cursor)
    assert updated == "2026-03-10T09:30:00.000Z"
    assert lists == ("Personal",)
    # empty allowlist is stored as a wildcard, not an empty fingerprint
    updated, lists = parse_cursor(build_cursor(None, ()))
    assert updated is None
    assert lists == ("*",)


def test_parse_cursor_garbage_returns_none_pair():
    assert parse_cursor(None) == (None, None)
    assert parse_cursor("") == (None, None)
    assert parse_cursor("{not json") == (None, None)
    assert parse_cursor('{"unexpected": 1}') == (None, None)


# --- fetch -------------------------------------------------------------------


def test_first_fetch_walks_all_lists_and_pages():
    service = FakeTasksService(
        tasklists_pages=_all_lists_pages(),
        tasks_pages={PERSONAL: _personal_pages(), HOME: [_fixture("tasks_home_page1.json")]},
    )
    result = _connector(service).fetch(None, limit=100)
    # 5 + 1 tasks from Personal (2 pages) + 2 from Home = 8 records
    assert len(result.records) == 8
    assert result.reset is False
    assert [r.external_id for r in result.records][:5] == [
        "task-order-cake",
        "task-gifts",
        "task-parent-school",
        "task-sub-signup",
        "task-deleted-old",
    ]
    # every tasks.list call asked for the full window: completed, hidden, deleted
    assert service.tasks_params
    for params in service.tasks_params:
        assert params["showCompleted"] is True
        assert params["showHidden"] is True
        assert params["showDeleted"] is True
        assert params["maxResults"] == 100
        assert "updatedMin" not in params
    # cursor = max updated seen across ALL lists
    updated, _lists = parse_cursor(result.next_cursor)
    assert updated == "2026-03-10T09:30:00.000Z"
    # tasks.list was called once per list per page (Personal has 2 pages)
    personal_calls = [p for p in service.tasks_params if p["tasklist"] == PERSONAL]
    assert len(personal_calls) == 2
    assert personal_calls[1]["pageToken"] == "tasks-personal-page-token-2"


def test_incremental_fetch_sends_updatedmin():
    service = FakeTasksService(
        tasklists_pages=_all_lists_pages(),
        tasks_pages={PERSONAL: [_fixture("tasks_personal_page2.json")]},
    )
    cursor = build_cursor("2026-03-09T00:00:00.000Z", ())
    _connector(service).fetch(cursor, limit=100)
    assert all(p["updatedMin"] == "2026-03-09T00:00:00.000Z" for p in service.tasks_params)


def test_unchanged_cursor_when_nothing_new():
    service = FakeTasksService(
        tasklists_pages=_all_lists_pages(),
        tasks_pages={PERSONAL: [{"items": []}], HOME: [{"items": []}]},
    )
    cursor = build_cursor("2026-03-10T09:30:00.000Z", ())
    result = _connector(service).fetch(cursor, limit=100)
    assert result.records == []
    assert result.reset is False
    updated, lists = parse_cursor(result.next_cursor)
    assert updated == "2026-03-10T09:30:00.000Z"
    assert lists == ("*",)


def test_updatedmin_boundary_is_inclusive_runner_skips_unchanged():
    # updatedMin is inclusive: a task whose updated == cursor comes back again.
    # It carries version == that same 'updated', so the runner skips it by
    # version — no duplicate, no churn (runner semantics test in base).
    service = FakeTasksService(
        tasklists_pages=[
            _fixture("tasklists_page1.json")["items"] and _fixture("tasklists_page1.json")
        ],
        tasks_pages={
            PERSONAL: [{"items": [_task("tasks_personal_page1.json", "task-order-cake")]}]
        },
    )
    boundary = "2026-03-10T09:30:00.000Z"
    result = _connector(service).fetch(build_cursor(boundary, ()), limit=100)
    assert [r.external_id for r in result.records] == ["task-order-cake"]
    assert result.records[0].version == boundary  # exactly the stored version


def test_limit_truncates_and_cursor_stays_safe():
    # Three tasks with distinct updates; limit 2 emits the first two in walk
    # order; the cursor falls back to the earliest unemitted 'updated' so the
    # next run refetches everything not yet emitted (at-least-once safety).
    tasks = [
        {
            "id": "t1",
            "title": "one",
            "updated": "2026-03-01T00:00:00.000Z",
            "status": "needsAction",
        },
        {
            "id": "t2",
            "title": "two",
            "updated": "2026-03-02T00:00:00.000Z",
            "status": "needsAction",
        },
        {
            "id": "t3",
            "title": "three",
            "updated": "2026-03-03T00:00:00.000Z",
            "status": "needsAction",
        },
    ]
    service = FakeTasksService(
        tasklists_pages=[_fixture("tasklists_page1.json")],
        tasks_pages={PERSONAL: [{"items": tasks}]},
    )
    result = _connector(service).fetch(None, limit=2)
    assert [r.external_id for r in result.records] == ["t1", "t2"]
    updated, _lists = parse_cursor(result.next_cursor)
    assert updated == "2026-03-03T00:00:00.000Z"  # earliest unemitted


def test_allowlist_limits_lists_and_marks_cursor():
    service = FakeTasksService(
        tasklists_pages=_all_lists_pages(),
        tasks_pages={HOME: [_fixture("tasks_home_page1.json")]},
    )
    result = _connector(service, allowlist="בית").fetch(None, limit=100)
    assert {r.meta["list_title"] for r in result.records} == {"בית"}
    assert all(p["tasklist"] == HOME for p in service.tasks_params)
    _updated, lists = parse_cursor(result.next_cursor)
    assert lists == ("בית",)


def test_allowlist_change_resets_to_full_sync():
    service = FakeTasksService(
        tasklists_pages=_all_lists_pages(),
        tasks_pages={PERSONAL: [_personal_pages()[0]], HOME: [_fixture("tasks_home_page1.json")]},
    )
    old_cursor = build_cursor("2026-03-09T00:00:00.000Z", ("Personal",))
    result = _connector(service, allowlist="Personal, בית").fetch(old_cursor, limit=100)
    assert result.reset is True
    assert {r.meta["list_title"] for r in result.records} == {"Personal", "בית"}


def test_garbage_cursor_resets():
    service = FakeTasksService(
        tasklists_pages=_all_lists_pages(), tasks_pages={PERSONAL: [_personal_pages()[0]]}
    )
    result = _connector(service).fetch("{corrupt", limit=100)
    assert result.reset is True
    assert result.records


def test_missing_list_skipped_without_reset():
    # A vanished list (404) is skipped with a warning; the sync continues.
    service = FakeTasksService(
        tasklists_pages=_all_lists_pages(),
        tasks_pages={
            PERSONAL: [_personal_pages()[0]],
            HOME: [_fixture("tasks_home_page1.json")],
            # WORK intentionally absent: an unknown list id -> 404 from the fake
        },
        tasks_errors={WORK: 404},
    )
    result = _connector(service).fetch(None, limit=100)
    assert result.reset is False
    titles = {r.meta["list_title"] for r in result.records}
    assert "Work" not in titles
    assert "Personal" in titles


def test_invalid_updatedmin_resets_full_sync():
    # A 400 on the incremental call invalidates the cursor: retry full, reset.
    service = FakeTasksService(
        tasklists_pages=_all_lists_pages(),
        tasks_pages={
            PERSONAL: [_personal_pages()[0]],
            HOME: [_fixture("tasks_home_page1.json")],
        },
        tasks_errors={PERSONAL: 400},
    )
    cursor = build_cursor("2026-03-09T00:00:00.000Z", ())
    result = _connector(service).fetch(cursor, limit=100)
    assert result.reset is True
    assert [r.external_id for r in result.records][:1] == ["task-order-cake"]
    # the full retry did NOT send updatedMin — only the one rejected
    # incremental attempt ever carried it
    incremental_calls = [p for p in service.tasks_params if "updatedMin" in p]
    assert len(incremental_calls) == 1


def test_cursor_invalid_410_resets_full_sync():
    # 410 (Gone) on an incremental call is the other cursor-invalid status —
    # same handling as Calendar's expired syncToken: full resync, reset=True.
    service = FakeTasksService(
        tasklists_pages=_all_lists_pages(),
        tasks_pages={
            PERSONAL: [_personal_pages()[0]],
            HOME: [_fixture("tasks_home_page1.json")],
        },
        tasks_errors={PERSONAL: 410},
    )
    cursor = build_cursor("2026-03-09T00:00:00.000Z", ())
    result = _connector(service).fetch(cursor, limit=100)
    assert result.reset is True
    assert result.records


def test_cursor_invalid_400_on_full_walk_bubbles():
    # A 400 with NO watermark is not a cursor problem — it must surface.
    service = FakeTasksService(
        tasklists_pages=_all_lists_pages(),
        tasks_pages={PERSONAL: [_personal_pages()[0]]},
        tasks_errors={PERSONAL: 400},
    )
    with pytest.raises(ConnectorError, match="google http 400"):
        _connector(service).fetch(None, limit=10)


def test_404_on_incremental_list_does_not_reset():
    service = FakeTasksService(
        tasklists_pages=_all_lists_pages(),
        tasks_pages={
            PERSONAL: [_personal_pages()[0]],
            HOME: [_fixture("tasks_home_page1.json")],
        },
        tasks_errors={HOME: 404},
    )
    cursor = build_cursor("2026-03-09T00:00:00.000Z", ())
    result = _connector(service).fetch(cursor, limit=100)
    assert result.reset is False
    assert {r.meta["list_title"] for r in result.records} == {"Personal"}


def test_server_error_bubbles_as_retryable():
    # 5xx -> the base translation yields ConnectorRetryableError; the RUNNER
    # owns the backoff, the adapter must not swallow it.
    service = FakeTasksService(
        tasklists_pages=_all_lists_pages(),
        tasks_pages={PERSONAL: [_personal_pages()[0]]},
        tasks_errors={PERSONAL: 500},
    )
    with pytest.raises(ConnectorRetryableError, match="google http 500") as excinfo:
        _connector(service).fetch(None, limit=10)
    assert excinfo.value.status == 500


def test_tasklists_enumeration_failure_bubbles():
    service = FakeTasksService(tasklists_error=503)
    with pytest.raises(ConnectorRetryableError, match="google http 503"):
        _connector(service).fetch(None, limit=10)


def test_scope_is_readonly():
    assert GTASKS_SCOPE == "https://www.googleapis.com/auth/tasks.readonly"


def test_meta_is_json_safe():
    record = record_from_task(
        _task("tasks_personal_page1.json", "task-order-cake"),
        TZ,
        list_id=PERSONAL,
        list_title="Personal",
        parent_title=None,
    )
    json.dumps(record.meta)  # must not raise


def test_version_falls_back_to_etag_without_updated():
    record = record_from_task({"id": "t1", "etag": 'W/"etag-1"'}, TZ)
    assert record.version == 'W/"etag-1"'


def test_deleted_completed_task_is_deleted_but_keeps_status():
    # deleted wins over completed (the runner hides the item), and the meta
    # still records what the status was.
    record = record_from_task(
        {
            "id": "t1",
            "updated": "2026-03-01T00:00:00Z",
            "status": "completed",
            "completed": "2026-03-01T10:00:00Z",
            "deleted": True,
        },
        TZ,
    )
    assert record.deleted is True
    assert record.meta["status"] == "completed"
    assert record.meta["completed_at"] == "2026-03-01T10:00:00+00:00"


def test_registry_registration():
    from brain.connectors import get

    assert get("gtasks") is GTasksConnector
