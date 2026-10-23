"""Runner-level tests for the gtasks connector (contract semantics).

The REAL GTasksConnector (fetch backed by the duck-typed FakeTasksService)
runs through the REAL ConnectorRunner + IngestService on the brain_w11_test
database: new / unchanged / changed / deleted must behave per the runner
semantics for gtasks records, and the updatedMin cursor must round-trip.
No network anywhere; the fake service is the only Google seam.
"""

from __future__ import annotations

import copy
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text
from tests.test_connector_gtasks import (
    HOME,
    PERSONAL,
    FakeTasksService,
    _fixture,
)
from tests.test_connectors_runner import Harness

from brain.connectors.google.tasks import GTasksConnector


@pytest.fixture(name="runner_db")
def runner_db_fixture(database: None):
    """Fresh connector_state/connector_records; memory_items cleaned after."""
    from brain.db.session import SessionLocal, engine

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


class GTasksHarness(Harness):
    """The production runner wiring, keyed to the gtasks source name."""

    def item(self, external_id: str):  # noqa: ANN201 (matches the base shape)
        row = self.records.get("gtasks", external_id)
        return None if row is None else self.repo.get(row.item_id)


def _connector(service: FakeTasksService) -> GTasksConnector:
    """A real GTasksConnector whose service is the fake (no OAuth wiring)."""
    conn = object.__new__(GTasksConnector)
    conn._lists_allowlist = ""
    conn._service = lambda: service  # type: ignore[method-assign]
    return conn


def _full_service() -> FakeTasksService:
    """Fake serving the full fixture corpus (both lists, all pages)."""
    return FakeTasksService(
        tasklists_pages=[_fixture("tasklists_page1.json"), _fixture("tasklists_page2.json")],
        tasks_pages={
            PERSONAL: [
                _fixture("tasks_personal_page1.json"),
                _fixture("tasks_personal_page2.json"),
            ],
            HOME: [_fixture("tasks_home_page1.json")],
        },
    )


def _stamp(minutes_after_midnight: int) -> str:
    """A distinct RFC 3339 stamp: 2026-03-01T00:MM:00.000Z (UTC)."""
    moment = datetime(2026, 3, 1, tzinfo=UTC) + timedelta(minutes=minutes_after_midnight)
    return moment.strftime("%Y-%m-%dT%H:%M:%S.000Z")


def test_full_sync_truncation_ingests_everything_within_three_runs(runner_db):
    # REGRESSION (review of a15bc56): records were emitted in API order (list
    # position) while the cursor resumed at the earliest UNEMITTED 'updated'.
    # When the oldest stamp sat among the unemitted tasks, every run re-fetched
    # all 150 tasks and re-emitted the same first 100 — the cursor never moved
    # and the last 50 were never ingested (livelock). The fixture below is in
    # REVERSED updated order (position 0 = newest), so the oldest stamp is the
    # LAST task and lands among the unemitted.
    tasks = [
        {
            "id": f"t{position:03d}",
            "title": f"task {position}",
            "updated": _stamp(149 - position),  # reversed vs API order
            "status": "needsAction",
        }
        for position in range(150)
    ]
    service = FakeTasksService(
        tasklists_pages=[_fixture("tasklists_page1.json")],
        tasks_pages={PERSONAL: [{"items": tasks}]},
    )
    h = GTasksHarness(runner_db)
    connector = _connector(service)
    runner = h.runner(connector)
    first = runner.run(limit=100)
    second_service = FakeTasksService(
        tasklists_pages=[_fixture("tasklists_page1.json")],
        tasks_pages={PERSONAL: [{"items": tasks}]},
    )
    connector._service = lambda: second_service  # type: ignore[method-assign]
    second = runner.run(limit=100)
    third_service = FakeTasksService(
        tasklists_pages=[_fixture("tasklists_page1.json")],
        tasks_pages={PERSONAL: [{"items": tasks}]},
    )
    connector._service = lambda: third_service  # type: ignore[method-assign]
    third = runner.run(limit=100)

    assert first.created == 100
    # every task ingested within 2-3 fetches, and the loop terminates
    assert second.created + third.created + first.created == 150
    assert third.created == 0


def test_tie_group_larger_than_limit_is_emitted_whole(runner_db):
    # 120 tasks sharing ONE 'updated': resuming at that watermark (inclusive)
    # re-yields the whole tie group, so it must be emitted past the limit in
    # one batch — otherwise the cursor would resume at its own value forever.
    tasks = [
        {
            "id": f"tie{position:03d}",
            "title": f"tie {position}",
            "updated": "2026-03-01T00:00:00.000Z",
            "status": "needsAction",
        }
        for position in range(120)
    ]
    service = FakeTasksService(
        tasklists_pages=[_fixture("tasklists_page1.json")],
        tasks_pages={PERSONAL: [{"items": tasks}]},
    )
    h = GTasksHarness(runner_db)
    connector = _connector(service)
    first = h.runner(connector).run(limit=100)
    second_service = FakeTasksService(
        tasklists_pages=[_fixture("tasklists_page1.json")],
        tasks_pages={PERSONAL: [{"items": tasks}]},
    )
    connector._service = lambda: second_service  # type: ignore[method-assign]
    second = h.runner(connector).run(limit=100)

    assert first.created == 120  # tie group extended past the limit
    assert (second.created, second.errors) == (0, 0)
    assert second.skipped == 120  # all re-delivered, all skipped by version


def test_new_tasks_create_items_with_framework_meta(runner_db):
    h = GTasksHarness(runner_db)
    summary = h.runner(_connector(_full_service())).run()

    assert summary.created == 7  # 6 Personal + 2 Home, minus the deleted task:
    # a deleted=True record for an unknown id creates nothing (runner rule).
    assert summary.skipped == 1
    row = h.records.get("gtasks", "task-order-cake")
    assert row is not None and row.version == "2026-03-10T09:30:00.000Z"
    item = h.repo.get(row.item_id)
    assert item is not None
    assert item.source == "gtasks"
    assert item.meta["connector"] == "gtasks"
    assert item.meta["external_id"] == "task-order-cake"
    assert item.meta["version"] == "2026-03-10T09:30:00.000Z"
    assert item.meta["url"] == "https://tasks.google.com/task/task-order-cake"
    # adapter meta rides along
    assert item.meta["status"] == "needsAction"
    assert item.meta["due_date_only"] is True
    assert "completed_at" not in item.meta  # needsAction: no completion time
    assert "Order birthday cake" in item.raw_text


def test_updatedmin_cursor_round_trips_and_rerun_is_idempotent(runner_db):
    # updatedMin is INCLUSIVE on Google's side: the second run re-fetches the
    # tasks at the watermark. The runner must skip them by version — no new
    # items, no updates, no churn.
    h = GTasksHarness(runner_db)
    connector = _connector(_full_service())
    first = h.runner(connector).run()
    second_service = _full_service()
    connector._service = lambda: second_service  # type: ignore[method-assign]
    second = h.runner(connector).run()

    assert first.created == 7
    assert (second.created, second.updated, second.errors) == (0, 0, 0)
    # The fake filters server-side like Google: only the task AT the watermark
    # (inclusive) is re-delivered — and skipped by version. Idempotent.
    assert (second.fetched, second.skipped) == (1, 1)
    # The saved cursor (max updated across lists) reached Google as updatedMin.
    incremental = [p for p in second_service.tasks_params if "updatedMin" in p]
    assert incremental, "second sync must be incremental"
    assert all(p["updatedMin"] == "2026-03-10T09:30:00.000Z" for p in incremental)


def test_changed_task_updates_item_in_place_and_reindexes(runner_db):
    h = GTasksHarness(runner_db)
    connector = _connector(_full_service())
    h.runner(connector).run()
    item_before = h.item("task-order-cake")
    assert item_before is not None
    item_id = item_before.id

    changed = copy.deepcopy(_fixture("tasks_personal_page1.json"))
    cake = next(t for t in changed["items"] if t["id"] == "task-order-cake")
    cake["notes"] = "Chocolate, feeds 14."  # bumped content...
    cake["updated"] = "2026-03-11T08:00:00.000Z"  # ...and version
    second_service = FakeTasksService(
        tasklists_pages=[_fixture("tasklists_page1.json")],
        tasks_pages={PERSONAL: [changed]},
    )
    connector._service = lambda: second_service  # type: ignore[method-assign]
    summary = h.runner(connector).run()

    assert (summary.updated, summary.created) == (1, 0)
    item_after = h.item("task-order-cake")
    assert item_after is not None and item_after.id == item_id  # in place
    assert "Chocolate, feeds 14." in item_after.raw_text
    assert item_after.meta["version"] == "2026-03-11T08:00:00.000Z"
    assert item_id in h.indexer.indexed  # re-indexed


def test_deleted_task_is_marked_and_hidden_never_hard_deleted(runner_db):
    h = GTasksHarness(runner_db)
    connector = _connector(_full_service())
    h.runner(connector).run()
    item_before = h.item("task-order-cake")
    assert item_before is not None
    item_id = item_before.id

    deleted = copy.deepcopy(_fixture("tasks_personal_page1.json"))
    cake = next(t for t in deleted["items"] if t["id"] == "task-order-cake")
    cake["deleted"] = True
    cake["hidden"] = True
    cake["updated"] = "2026-03-11T08:00:00.000Z"
    second_service = FakeTasksService(
        tasklists_pages=[_fixture("tasklists_page1.json")],
        tasks_pages={PERSONAL: [deleted]},
    )
    connector._service = lambda: second_service  # type: ignore[method-assign]
    summary = h.runner(connector).run()

    assert summary.deleted == 1
    row = h.records.get("gtasks", "task-order-cake")
    assert row is not None  # the ledger entry stays
    item = h.repo.get(item_id)
    assert item is not None  # NEVER hard-deleted
    assert item.meta["source_deleted"] is True
    assert item.superseded_by == item.id  # hidden via the PR B mechanism
    listed_ids = {
        str(i.id)
        for i in h.repo.list(item_type=None, project=None, status=None, limit=10, offset=0)
    }
    assert str(item_id) not in listed_ids  # the deleted one is hidden...
    assert len(listed_ids) == 6  # ...the rest are untouched
