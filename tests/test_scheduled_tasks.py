"""Scheduled tasks: store CRUD, the atomic claim, catch-up/skip, run recording,
the /schedules API, and the migration round-trip (real *_test Postgres)."""

from __future__ import annotations

import threading
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient

from brain.ai.ask import LLMError
from brain.db.session import SessionLocal, engine
from brain.services.ask import AskResult
from brain.services.schedule_spec import ScheduleError
from brain.services.scheduled_tasks import (
    ScheduledTaskNotFound,
    ScheduledTaskStore,
    execute_claim,
)
from conftest import assert_test_database

IL = ZoneInfo("Asia/Jerusalem")
HEBREW_PROMPT = (
    "בדוק אם התקבלו מיילים חדשים 7 ימים אחרונים מהדומיין edu-haifa.org.il (בית הספר של רונה) ותסכם"
)


@pytest.fixture(name="sched_db")
def sched_db_fixture(database: None) -> Iterator[None]:
    assert_test_database(str(engine.url))
    with engine.begin() as conn:
        conn.execute(sa.text("DELETE FROM scheduled_tasks"))
    yield
    with engine.begin() as conn:
        conn.execute(sa.text("DELETE FROM scheduled_tasks"))


@contextmanager
def _store() -> Iterator[ScheduledTaskStore]:
    session = SessionLocal()
    try:
        yield ScheduledTaskStore(session)
    finally:
        session.close()


def _at(local: str) -> datetime:
    """'2026-11-13 07:00' in Israel time -> aware UTC."""
    return datetime.fromisoformat(local).replace(tzinfo=IL).astimezone(UTC)


def _make(now: datetime, schedule: str = "0 7 * * 5", **kw) -> uuid.UUID:
    with _store() as store:
        task = store.create(
            name=kw.pop("name", "Rona school mail"),
            prompt=kw.pop("prompt", HEBREW_PROMPT),
            schedule=schedule,
            now=now,
            **kw,
        )
        return task.id


def _runs(task_id: uuid.UUID) -> list:
    with _store() as store:
        return store.runs(task_id)


# --- store ------------------------------------------------------------------------


def test_create_computes_next_run_in_the_task_timezone(sched_db):
    task_id = _make(_at("2026-11-08 12:00"))
    with _store() as store:
        task = store.get(task_id)
    assert task.next_run_at == _at("2026-11-13 07:00")
    assert task.timezone == "Asia/Jerusalem" and task.enabled
    assert task.prompt == HEBREW_PROMPT


def test_create_rejects_bad_input(sched_db):
    with _store() as store:
        with pytest.raises(ScheduleError, match="5 fields"):
            store.create(name="x", prompt="y", schedule="0 7 *")
        with pytest.raises(ScheduleError, match="name is empty"):
            store.create(name="  ", prompt="y", schedule="0 7 * * *")
        with pytest.raises(ScheduleError, match="unknown timezone"):
            store.create(name="x", prompt="y", schedule="0 7 * * *", timezone="Nope/Nope")
        assert store.all() == []


def test_pause_clears_next_run_and_resume_does_not_catch_up(sched_db):
    task_id = _make(_at("2026-11-08 12:00"))
    with _store() as store:
        assert store.set_enabled(task_id, False, now=_at("2026-11-08 12:00")).next_run_at is None
        # resumed long after a missed Friday: next run is the NEXT Friday
        task = store.set_enabled(task_id, True, now=_at("2026-11-14 09:00"))
        assert task.next_run_at == _at("2026-11-20 07:00")
    with _store() as store:
        claims = store.claim_due(_at("2026-11-14 09:01"), max_late_s=12 * 3600)
    assert claims == []


def test_update_moves_next_run_only_when_timing_changes(sched_db):
    task_id = _make(_at("2026-11-08 12:00"))
    with _store() as store:
        task = store.update(
            task_id,
            name="Renamed",
            prompt="new prompt",
            schedule="0 7 * * 5",
            timezone="Asia/Jerusalem",
            now=_at("2026-11-10 12:00"),
        )
        assert task.next_run_at == _at("2026-11-13 07:00") and task.name == "Renamed"
        task = store.update(
            task_id,
            name="Renamed",
            prompt="new prompt",
            schedule="0 6 * * *",
            timezone="Asia/Jerusalem",
            now=_at("2026-11-10 12:00"),
        )
        assert task.next_run_at == _at("2026-11-11 06:00")


def test_delete_cascades_runs_and_unknown_id_raises(sched_db):
    task_id = _make(_at("2026-11-08 12:00"))
    with _store() as store:
        store.request_run(task_id)
        assert store.claim_due(max_late_s=60)
        store.delete(task_id)
        with pytest.raises(ScheduledTaskNotFound):
            store.get(task_id)
    with engine.connect() as conn:
        left = conn.execute(
            sa.text("SELECT count(*) FROM scheduled_task_runs WHERE task_id = :id"),
            {"id": task_id},
        ).scalar_one()
    assert left == 0


# --- claim --------------------------------------------------------------------------


def test_due_task_is_claimed_once_and_next_run_advances(sched_db):
    task_id = _make(_at("2026-11-08 12:00"))
    now = _at("2026-11-13 07:00") + timedelta(seconds=40)
    with _store() as store:
        claims = store.claim_due(now, max_late_s=12 * 3600)
        again = store.claim_due(now, max_late_s=12 * 3600)
        task = store.get(task_id)
    assert len(claims) == 1 and again == []
    assert claims[0].trigger == "schedule" and claims[0].scheduled_for == _at("2026-11-13 07:00")
    assert task.next_run_at == _at("2026-11-20 07:00")
    assert task.last_status == "running"
    (run,) = _runs(task_id)
    assert run.status == "running" and run.id == claims[0].run_id


def test_stale_snapshot_loses_the_compare_and_set(sched_db):
    _make(_at("2026-11-08 12:00"))
    now = _at("2026-11-13 07:01")
    with _store() as slow, _store() as fast:
        (snapshot,) = slow.due_tasks(now)
        assert len(fast.claim_due(now, max_late_s=3600)) == 1
        assert slow.claim(snapshot, now, max_late_s=3600) is None


def test_concurrent_claimers_run_a_slot_exactly_once(sched_db):
    task_id = _make(_at("2026-11-08 12:00"))
    now = _at("2026-11-13 07:01")
    barrier = threading.Barrier(8)
    results: list[int] = []
    lock = threading.Lock()

    def claimer() -> None:
        with _store() as store:
            barrier.wait()
            got = store.claim_due(now, max_late_s=3600)
        with lock:
            results.append(len(got))

    threads = [threading.Thread(target=claimer) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
    assert sum(results) == 1
    assert len(_runs(task_id)) == 1


def test_downtime_collapses_missed_slots_into_one_run(sched_db):
    # daily 06:00; the bot was down from the 9th: the 9th and 10th slots are
    # long gone, the 11th is 4h late -> ONE run, for the 11th
    task_id = _make(_at("2026-11-08 12:00"), schedule="0 6 * * *")
    with engine.begin() as conn:  # the last claim happened before the outage
        conn.execute(
            sa.text("UPDATE scheduled_tasks SET next_run_at = :t WHERE id = :id"),
            {"t": _at("2026-11-09 06:00"), "id": task_id},
        )
    now = _at("2026-11-11 10:00")
    with _store() as store:
        claims = store.claim_due(now, max_late_s=12 * 3600)
        assert store.claim_due(now, max_late_s=12 * 3600) == []
        task = store.get(task_id)
    assert len(claims) == 1 and claims[0].scheduled_for == _at("2026-11-11 06:00")
    assert task.next_run_at == _at("2026-11-12 06:00")
    (run,) = _runs(task_id)
    assert run.status == "running"


def test_slot_missed_by_more_than_the_window_is_recorded_as_skipped(sched_db):
    # weekly Friday 07:00; the bot came back Saturday 21:00 (38h late)
    task_id = _make(_at("2026-11-08 12:00"))
    now = _at("2026-11-14 21:00")
    with _store() as store:
        claims = store.claim_due(now, max_late_s=12 * 3600)
        task = store.get(task_id)
    assert claims == []
    assert task.next_run_at == _at("2026-11-20 07:00")
    assert task.last_status == "skipped"
    (run,) = _runs(task_id)
    assert run.status == "skipped" and "missed by 38.0h" in (run.error or "")
    assert run.scheduled_for == _at("2026-11-13 07:00") and run.finished_at is not None


def test_run_now_survives_a_skipped_slot(sched_db):
    """Review of #163: bot down 38h, the user pressed Run now meanwhile; the
    too-late slot is skipped but the manual request must still run."""
    task_id = _make(_at("2026-11-08 12:00"))
    with _store() as store:
        store.request_run(task_id, now=_at("2026-11-14 20:00"))
    now = _at("2026-11-14 21:00")
    with _store() as store:
        claims = store.claim_due(now, max_late_s=12 * 3600)
        task = store.get(task_id)
    assert [c.trigger for c in claims] == ["manual"]
    assert task.run_requested_at is None
    assert task.next_run_at == _at("2026-11-20 07:00")
    assert sorted(r.status for r in _runs(task_id)) == ["running", "skipped"]


def test_short_downtime_runs_the_missed_slot_once(sched_db):
    task_id = _make(_at("2026-11-08 12:00"), schedule="0 * * * *")
    with engine.begin() as conn:
        conn.execute(
            sa.text("UPDATE scheduled_tasks SET next_run_at = :t WHERE id = :id"),
            {"t": _at("2026-11-10 06:00"), "id": task_id},
        )
    now = _at("2026-11-10 09:20")  # 4 hourly slots missed, 3h20m late
    with _store() as store:
        claims = store.claim_due(now, max_late_s=12 * 3600)
        assert store.claim_due(now, max_late_s=12 * 3600) == []
        task = store.get(task_id)
    assert len(claims) == 1
    assert task.next_run_at == _at("2026-11-10 10:00")  # from NOW, not the old slot


def test_paused_task_is_not_claimed_but_run_now_is(sched_db):
    task_id = _make(_at("2026-11-08 12:00"))
    with _store() as store:
        store.set_enabled(task_id, False)
        assert store.claim_due(_at("2026-11-13 07:01"), max_late_s=3600) == []
        store.request_run(task_id)
        (claim,) = store.claim_due(max_late_s=3600)
        assert claim.trigger == "manual" and claim.scheduled_for is None
        assert store.claim_due(max_late_s=3600) == []
        task = store.get(task_id)
    assert task.run_requested_at is None and task.next_run_at is None and not task.enabled


def test_excluded_inflight_task_waits(sched_db):
    task_id = _make(_at("2026-11-08 12:00"))
    now = _at("2026-11-13 07:01")
    with _store() as store:
        assert store.claim_due(now, max_late_s=3600, exclude=[task_id]) == []
        assert len(store.claim_due(now, max_late_s=3600)) == 1


# --- run recording ---------------------------------------------------------------------


def test_execute_claim_records_ok_with_cited_ids(sched_db):
    task_id = _make(_at("2026-11-08 12:00"))
    with _store() as store:
        store.request_run(task_id)
        (claim,) = store.claim_due(max_late_s=60)
    cited = str(uuid.uuid4())
    asked: list[str] = []

    def ask(prompt: str) -> AskResult:
        asked.append(prompt)
        return AskResult(answer=f"3 new mails [{cited}]", cited_ids=[cited])

    outcome = execute_claim(_store, claim, ask)
    assert outcome.status == "ok" and outcome.recorded and asked == [HEBREW_PROMPT]
    (run,) = _runs(task_id)
    assert run.status == "ok" and run.output == f"3 new mails [{cited}]"
    assert run.cited_ids == [cited] and run.finished_at is not None
    with _store() as store:
        assert store.get(task_id).last_status == "ok"


def test_execute_claim_records_errors_without_raising(sched_db):
    task_id = _make(_at("2026-11-08 12:00"))
    with _store() as store:
        store.request_run(task_id)
        (claim,) = store.claim_due(max_late_s=60)

    def ask(prompt: str) -> AskResult:
        raise LLMError("llm down")

    outcome = execute_claim(_store, claim, ask)
    assert outcome.status == "error" and outcome.llm_unavailable
    (run,) = _runs(task_id)
    assert run.status == "error" and "llm down" in (run.error or "")


def test_finish_is_first_writer_wins(sched_db):
    task_id = _make(_at("2026-11-08 12:00"))
    with _store() as store:
        store.request_run(task_id)
        (claim,) = store.claim_due(max_late_s=60)
        assert store.finish_run(claim.run_id, status="error", error="timed out after 1s")
        assert not store.finish_run(claim.run_id, status="ok", output="late answer")
    (run,) = _runs(task_id)
    assert run.status == "error" and run.output is None


def test_stale_running_rows_are_reaped(sched_db):
    task_id = _make(_at("2026-11-08 12:00"))
    with _store() as store:
        store.request_run(task_id)
        store.claim_due(_at("2026-11-08 12:00"), max_late_s=60)
        store.claim_due(_at("2026-11-08 12:30"), max_late_s=60, stale_after_s=600)
        task = store.get(task_id)
    (run,) = _runs(task_id)
    assert run.status == "error" and "interrupted" in (run.error or "")
    assert task.last_status == "error"


# --- API ----------------------------------------------------------------------------------


def _body(**overrides) -> dict:
    body = {"name": "Rona school mail", "prompt": HEBREW_PROMPT, "schedule": "0 7 * * 5"}
    body.update(overrides)
    return body


def test_api_crud_round_trip(sched_db, client: TestClient):
    created = client.post("/schedules", json=_body())
    assert created.status_code == 201, created.text
    task = created.json()
    assert task["schedule_text"] == "Every Friday at 07:00"
    assert task["timezone"] == "Asia/Jerusalem" and task["enabled"] is True
    assert task["next_run_at"] is not None and task["run_requested"] is False
    task_id = task["id"]

    assert [t["id"] for t in client.get("/schedules").json()["tasks"]] == [task_id]
    assert client.get(f"/schedules/{task_id}").json()["prompt"] == HEBREW_PROMPT

    updated = client.put(f"/schedules/{task_id}", json=_body(name="Daily", schedule="0 6 * * *"))
    assert updated.status_code == 200
    assert updated.json()["name"] == "Daily"
    assert updated.json()["schedule_text"] == "Every day at 06:00"

    paused = client.post(f"/schedules/{task_id}/pause").json()
    assert paused["enabled"] is False and paused["next_run_at"] is None
    resumed = client.post(f"/schedules/{task_id}/resume").json()
    assert resumed["enabled"] is True and resumed["next_run_at"] is not None

    queued = client.post(f"/schedules/{task_id}/run")
    assert queued.status_code == 202 and queued.json()["run_requested"] is True

    with _store() as store:
        (claim,) = store.claim_due(max_late_s=60)
        store.finish_run(claim.run_id, status="ok", output="answer", cited_ids=["a"])
    runs = client.get(f"/schedules/{task_id}/runs").json()["runs"]
    assert len(runs) == 1 and runs[0]["status"] == "ok" and runs[0]["output"] == "answer"
    assert runs[0]["trigger"] == "manual" and runs[0]["cited_ids"] == ["a"]

    assert client.delete(f"/schedules/{task_id}").status_code == 204
    assert client.get(f"/schedules/{task_id}").status_code == 404
    assert client.get(f"/schedules/{task_id}/runs").status_code == 404


@pytest.mark.parametrize(
    ("overrides", "detail"),
    [
        ({"schedule": "every friday"}, "5 fields"),
        ({"schedule": "0 25 * * *"}, "hour value 25 is out of range"),
        ({"schedule": "* * * * *"}, "too often"),
        ({"schedule": "0 0 31 2 *"}, "never fires"),
        ({"timezone": "Asia/Nowhere"}, "unknown timezone"),
        ({"name": "   "}, "name is empty"),
        ({"prompt": ""}, "prompt is empty"),
    ],
)
def test_api_rejects_invalid_input_with_422(sched_db, client: TestClient, overrides, detail):
    response = client.post("/schedules", json=_body(**overrides))
    assert response.status_code == 422
    assert detail in response.json()["detail"]
    assert client.get("/schedules").json()["tasks"] == []


def test_api_update_with_invalid_schedule_keeps_the_task(sched_db, client: TestClient):
    task_id = client.post("/schedules", json=_body()).json()["id"]
    response = client.put(f"/schedules/{task_id}", json=_body(schedule="0 7 * *"))
    assert response.status_code == 422 and "5 fields" in response.json()["detail"]
    assert client.get(f"/schedules/{task_id}").json()["schedule"] == "0 7 * * 5"


def test_api_unknown_ids_are_404(sched_db, client: TestClient):
    missing = uuid.uuid4()
    assert client.put(f"/schedules/{missing}", json=_body()).status_code == 404
    assert client.delete(f"/schedules/{missing}").status_code == 404
    assert client.post(f"/schedules/{missing}/pause").status_code == 404
    assert client.post(f"/schedules/{missing}/run").status_code == 404


def test_api_preview(sched_db, client: TestClient):
    response = client.post("/schedules/preview", json={"schedule": "@daily", "timezone": "UTC"})
    assert response.status_code == 200
    payload = response.json()
    assert payload["schedule"] == "0 0 * * *"
    assert payload["schedule_text"] == "Every day at 00:00"
    assert len(payload["next_runs"]) == 5
    bad = client.post("/schedules/preview", json={"schedule": "0 7 * * 9"})
    assert bad.status_code == 422


# --- migration ----------------------------------------------------------------------------


def test_scheduled_tasks_migration_round_trips(database):
    from alembic import command
    from alembic.config import Config

    from conftest import _REPO_ROOT

    cfg = Config(str(_REPO_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(_REPO_ROOT / "src/brain/db/migrations"))
    assert_test_database(str(engine.url))
    command.downgrade(cfg, "f3a7c9e2b5d8")
    try:
        with engine.connect() as conn:
            assert conn.execute(sa.text("SELECT to_regclass('scheduled_tasks')")).scalar() is None
            assert (
                conn.execute(sa.text("SELECT to_regclass('scheduled_task_runs')")).scalar() is None
            )
    finally:
        command.upgrade(cfg, "head")
    with engine.connect() as conn:
        cols = {
            row[0]
            for row in conn.execute(
                sa.text(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_name = 'scheduled_tasks'"
                )
            )
        }
    assert {
        "id",
        "name",
        "prompt",
        "schedule",
        "timezone",
        "enabled",
        "next_run_at",
        "last_run_at",
        "last_status",
        "run_requested_at",
        "created_at",
        "updated_at",
    } <= cols
