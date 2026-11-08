"""Scheduled tasks: a name + a prompt run on a cron schedule (thin router).

The bot's poll loop executes due tasks through the /ask pipeline and sends
the answer to every allowed Telegram user (brain.telegram_bot); this router
only manages the tasks and reads their run history.

GET    /schedules                 every task (oldest first)
POST   /schedules                 create (201)
POST   /schedules/preview         validate a schedule: words + next 5 fires
GET    /schedules/{id}            one task
PUT    /schedules/{id}            replace name/prompt/schedule/timezone[/enabled]
DELETE /schedules/{id}            delete (its runs go with it) -> 204
POST   /schedules/{id}/pause      stop scheduling (next_run_at cleared)
POST   /schedules/{id}/resume     schedule again from now (no catch-up)
POST   /schedules/{id}/run        run now -> 202: QUEUED for the bot's next
                                  poll tick (<= ~1 minute), not run in the
                                  request: the answer goes to Telegram,
                                  which only the bot process talks to, and
                                  an LLM call can outlast an HTTP request.
                                  Works while paused.
GET    /schedules/{id}/runs       recent runs, newest first (?limit=, <= 100)

Schedule: a 5-field cron string evaluated in the task's IANA timezone
(brain.services.schedule_spec). An invalid schedule, unknown timezone, a
schedule that never fires or fires more often than every 15 minutes, or an
empty name/prompt -> 422 with a readable ``detail``; unknown id -> 404.
"""

import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from sqlalchemy.orm import Session

from brain import schemas
from brain.db.models import ScheduledTask
from brain.db.session import get_db
from brain.services.schedule_spec import ScheduleError, describe, next_fire, validate
from brain.services.scheduled_tasks import (
    ScheduledTaskNotFound,
    ScheduledTaskStore,
    task_payload,
)

router = APIRouter(prefix="/schedules", tags=["schedules"])


def _store(db: Session = Depends(get_db)) -> ScheduledTaskStore:
    return ScheduledTaskStore(db)


def _out(task: ScheduledTask) -> schemas.ScheduledTaskOut:
    return schemas.ScheduledTaskOut.model_validate(task_payload(task))


def _invalid(exc: ScheduleError) -> HTTPException:
    return HTTPException(status_code=422, detail=str(exc))


def _missing(exc: ScheduledTaskNotFound) -> HTTPException:
    return HTTPException(status_code=404, detail=str(exc))


@router.get("", response_model=schemas.ScheduledTaskList)
def list_tasks(store: ScheduledTaskStore = Depends(_store)) -> schemas.ScheduledTaskList:
    return schemas.ScheduledTaskList(tasks=[_out(task) for task in store.all()])


@router.post("", response_model=schemas.ScheduledTaskOut, status_code=201)
def create_task(
    data: schemas.ScheduledTaskIn, store: ScheduledTaskStore = Depends(_store)
) -> schemas.ScheduledTaskOut:
    try:
        task = store.create(
            name=data.name,
            prompt=data.prompt,
            schedule=data.schedule,
            timezone=data.timezone,
            enabled=True if data.enabled is None else data.enabled,
        )
    except ScheduleError as exc:
        raise _invalid(exc) from None
    return _out(task)


@router.post("/preview", response_model=schemas.SchedulePreviewOut)
def preview_schedule(data: schemas.SchedulePreviewIn) -> schemas.SchedulePreviewOut:
    """Validate without saving: the cockpit's live 'next runs' hint."""
    try:
        spec, tz = validate(data.schedule, data.timezone)
    except ScheduleError as exc:
        raise _invalid(exc) from None
    fires: list[datetime] = []
    moment = datetime.now(tz=UTC)
    for _ in range(5):
        moment = next_fire(spec, tz, moment)
        fires.append(moment)
    return schemas.SchedulePreviewOut(
        schedule=spec.expr, schedule_text=describe(spec.expr), next_runs=fires
    )


@router.get("/{task_id}", response_model=schemas.ScheduledTaskOut)
def get_task(
    task_id: uuid.UUID, store: ScheduledTaskStore = Depends(_store)
) -> schemas.ScheduledTaskOut:
    try:
        return _out(store.get(task_id))
    except ScheduledTaskNotFound as exc:
        raise _missing(exc) from None


@router.put("/{task_id}", response_model=schemas.ScheduledTaskOut)
def update_task(
    task_id: uuid.UUID,
    data: schemas.ScheduledTaskIn,
    store: ScheduledTaskStore = Depends(_store),
) -> schemas.ScheduledTaskOut:
    """Edit; the next run moves only when the schedule/timezone/state changed."""
    try:
        task = store.update(
            task_id,
            name=data.name,
            prompt=data.prompt,
            schedule=data.schedule,
            timezone=data.timezone,
            enabled=data.enabled,
        )
    except ScheduledTaskNotFound as exc:
        raise _missing(exc) from None
    except ScheduleError as exc:
        raise _invalid(exc) from None
    return _out(task)


@router.delete("/{task_id}", status_code=204)
def delete_task(task_id: uuid.UUID, store: ScheduledTaskStore = Depends(_store)) -> Response:
    try:
        store.delete(task_id)
    except ScheduledTaskNotFound as exc:
        raise _missing(exc) from None
    return Response(status_code=204)


def _set_enabled(
    store: ScheduledTaskStore, task_id: uuid.UUID, enabled: bool
) -> schemas.ScheduledTaskOut:
    try:
        return _out(store.set_enabled(task_id, enabled))
    except ScheduledTaskNotFound as exc:
        raise _missing(exc) from None
    except ScheduleError as exc:
        raise _invalid(exc) from None


@router.post("/{task_id}/pause", response_model=schemas.ScheduledTaskOut)
def pause_task(
    task_id: uuid.UUID, store: ScheduledTaskStore = Depends(_store)
) -> schemas.ScheduledTaskOut:
    return _set_enabled(store, task_id, False)


@router.post("/{task_id}/resume", response_model=schemas.ScheduledTaskOut)
def resume_task(
    task_id: uuid.UUID, store: ScheduledTaskStore = Depends(_store)
) -> schemas.ScheduledTaskOut:
    return _set_enabled(store, task_id, True)


@router.post("/{task_id}/run", response_model=schemas.ScheduledTaskOut, status_code=202)
def run_task_now(
    task_id: uuid.UUID, store: ScheduledTaskStore = Depends(_store)
) -> schemas.ScheduledTaskOut:
    """Queue a run for the bot's next tick (see the module docstring)."""
    try:
        return _out(store.request_run(task_id))
    except ScheduledTaskNotFound as exc:
        raise _missing(exc) from None


@router.get("/{task_id}/runs", response_model=schemas.ScheduledTaskRunList)
def list_runs(
    task_id: uuid.UUID,
    limit: int = Query(default=20, ge=1, le=100),
    store: ScheduledTaskStore = Depends(_store),
) -> schemas.ScheduledTaskRunList:
    try:
        runs = store.runs(task_id, limit=limit)
    except ScheduledTaskNotFound as exc:
        raise _missing(exc) from None
    return schemas.ScheduledTaskRunList(
        runs=[schemas.ScheduledTaskRunOut.model_validate(run) for run in runs]
    )
