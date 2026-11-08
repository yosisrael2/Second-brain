"""Scheduled tasks: CRUD, the atomic run claim, and run execution.

A task is a name + a free-text prompt + a 5-field cron schedule in an IANA
timezone (:mod:`brain.services.schedule_spec`). A run is the SAME ask
pipeline as /ask (AskService: search + memory-layer facts + the user memory
file) with the prompt as the question; the bot's poll loop drives it
(``brain.telegram_bot._schedules_tick``) — no separate service.

Claim protocol (never twice): ``next_run_at`` is moved forward with a
compare-and-set ``UPDATE ... WHERE id = :id AND next_run_at = :old
RETURNING id``; only the caller whose UPDATE matched inserts the run row and
executes it. Run-now is a queue column (``run_requested_at``) claimed the
same way. After downtime the new ``next_run_at`` is computed from NOW, so a
burst of missed slots collapses into ONE run, for the LATEST missed slot;
when even that slot is older than ``max_late_s`` it is recorded as a
``skipped`` run instead of running late.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Callable, Iterator, Sequence
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import and_, or_, select, update
from sqlalchemy.orm import Session

from brain.db.models import ScheduledTask, ScheduledTaskRun
from brain.services.schedule_spec import (
    DEFAULT_TIMEZONE,
    ScheduleError,
    describe,
    get_zone,
    next_fire,
    parse_cron,
    validate,
)

logger = logging.getLogger(__name__)

NAME_MAX_CHARS = 120
PROMPT_MAX_CHARS = 4000
RUNS_DEFAULT_LIMIT = 20
_ERROR_MAX_CHARS = 2000
# Missed slots scanned to find the latest one after downtime (15-minute
# minimum interval: ~52 days of slots); past the cap the slot counts as old.
_CATCH_UP_SCAN_MAX = 5000


class ScheduledTaskNotFound(LookupError):
    """No task with that id."""


def _now() -> datetime:
    return datetime.now(tz=UTC)


@dataclass(frozen=True)
class DueTask:
    """A snapshot of a due task row (the claim's compare-and-set inputs)."""

    id: uuid.UUID
    name: str
    prompt: str
    schedule: str
    timezone: str
    enabled: bool
    next_run_at: datetime | None
    run_requested_at: datetime | None


@dataclass(frozen=True)
class Claim:
    """A claimed run: the run row exists (status 'running'); execute it."""

    run_id: uuid.UUID
    task_id: uuid.UUID
    name: str
    prompt: str
    trigger: str  # 'schedule' | 'manual'
    scheduled_for: datetime | None


@dataclass(frozen=True)
class RunOutcome:
    """What one execution produced (``recorded``: the run row took it)."""

    claim: Claim
    status: str  # 'ok' | 'error'
    result: Any = None  # AskResult on success
    error: str | None = None
    llm_unavailable: bool = False
    recorded: bool = True
    cited_ids: list[str] = field(default_factory=list)


def _clean_text(value: str | None, label: str, max_chars: int) -> str:
    text = (value or "").strip()
    if not text:
        raise ScheduleError(f"the {label} is empty")
    if len(text) > max_chars:
        raise ScheduleError(f"the {label} is longer than {max_chars} characters")
    return text


class ScheduledTaskStore:
    """Task CRUD + claim/finish over one SQLAlchemy session (commits itself)."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def _commit(self) -> None:
        # Core UPDATEs bypass the identity map: expire so later ORM reads
        # on this session see the committed rows, not a stale cache.
        self._session.commit()
        self._session.expire_all()

    # -- read -------------------------------------------------------------------

    def all(self) -> list[ScheduledTask]:
        return list(
            self._session.execute(
                select(ScheduledTask).order_by(ScheduledTask.created_at, ScheduledTask.id)
            ).scalars()
        )

    def get(self, task_id: uuid.UUID) -> ScheduledTask:
        task = self._session.get(ScheduledTask, task_id)
        if task is None:
            raise ScheduledTaskNotFound(f"scheduled task '{task_id}' not found")
        return task

    def runs(self, task_id: uuid.UUID, limit: int = RUNS_DEFAULT_LIMIT) -> list[ScheduledTaskRun]:
        self.get(task_id)
        return list(
            self._session.execute(
                select(ScheduledTaskRun)
                .where(ScheduledTaskRun.task_id == task_id)
                .order_by(ScheduledTaskRun.started_at.desc(), ScheduledTaskRun.id)
                .limit(limit)
            ).scalars()
        )

    # -- write ------------------------------------------------------------------

    def create(
        self,
        *,
        name: str,
        prompt: str,
        schedule: str,
        timezone: str = DEFAULT_TIMEZONE,
        enabled: bool = True,
        now: datetime | None = None,
    ) -> ScheduledTask:
        moment = now or _now()
        spec, tz = validate(schedule, timezone)
        task = ScheduledTask(
            id=uuid.uuid4(),
            name=_clean_text(name, "name", NAME_MAX_CHARS),
            prompt=_clean_text(prompt, "prompt", PROMPT_MAX_CHARS),
            schedule=spec.expr,
            timezone=tz.key,
            enabled=enabled,
            next_run_at=next_fire(spec, tz, moment) if enabled else None,
            created_at=moment,
            updated_at=moment,
        )
        self._session.add(task)
        self._session.commit()
        return task

    def update(
        self,
        task_id: uuid.UUID,
        *,
        name: str,
        prompt: str,
        schedule: str,
        timezone: str,
        enabled: bool | None = None,
        now: datetime | None = None,
    ) -> ScheduledTask:
        """Replace the editable fields; next_run_at is recomputed only when the
        schedule, timezone or enabled state changed (an edit of the name or
        prompt never moves the next run)."""
        moment = now or _now()
        task = self.get(task_id)
        spec, tz = validate(schedule, timezone)
        clean_name = _clean_text(name, "name", NAME_MAX_CHARS)
        clean_prompt = _clean_text(prompt, "prompt", PROMPT_MAX_CHARS)
        want_enabled = task.enabled if enabled is None else enabled
        timing_changed = (
            spec.expr != task.schedule or tz.key != task.timezone or want_enabled != task.enabled
        )
        task.name = clean_name
        task.prompt = clean_prompt
        task.schedule = spec.expr
        task.timezone = tz.key
        task.enabled = want_enabled
        if timing_changed or (want_enabled and task.next_run_at is None):
            task.next_run_at = next_fire(spec, tz, moment) if want_enabled else None
        task.updated_at = moment
        self._session.commit()
        return task

    def delete(self, task_id: uuid.UUID) -> None:
        task = self.get(task_id)
        self._session.delete(task)
        self._session.commit()

    def set_enabled(
        self, task_id: uuid.UUID, enabled: bool, *, now: datetime | None = None
    ) -> ScheduledTask:
        """Pause (next_run_at cleared) / resume (next fire from now: a
        resume never catches up on slots missed while paused)."""
        moment = now or _now()
        task = self.get(task_id)
        if task.enabled == enabled and (not enabled or task.next_run_at is not None):
            return task
        task.enabled = enabled
        if enabled:
            task.next_run_at = next_fire(parse_cron(task.schedule), get_zone(task.timezone), moment)
        else:
            task.next_run_at = None
        task.updated_at = moment
        self._session.commit()
        return task

    def request_run(self, task_id: uuid.UUID, *, now: datetime | None = None) -> ScheduledTask:
        """Queue a run-now (works while paused); the bot's next tick runs it."""
        task = self.get(task_id)
        if task.run_requested_at is None:
            task.run_requested_at = now or _now()
            self._session.commit()
        return task

    # -- claim / finish ------------------------------------------------------------

    def due_tasks(self, now: datetime) -> list[DueTask]:
        """Snapshots of every task with a due slot or a queued run-now."""
        rows = self._session.execute(
            select(ScheduledTask)
            .where(
                or_(
                    and_(ScheduledTask.enabled.is_(True), ScheduledTask.next_run_at <= now),
                    ScheduledTask.run_requested_at.is_not(None),
                )
            )
            .order_by(ScheduledTask.next_run_at, ScheduledTask.id)
        ).scalars()
        due = [
            DueTask(
                id=row.id,
                name=row.name,
                prompt=row.prompt,
                schedule=row.schedule,
                timezone=row.timezone,
                enabled=row.enabled,
                next_run_at=row.next_run_at,
                run_requested_at=row.run_requested_at,
            )
            for row in rows
        ]
        self._session.rollback()  # end the read transaction (no stale snapshot)
        return due

    def claim(self, task: DueTask, now: datetime, *, max_late_s: float) -> Claim | None:
        """Compare-and-set one due task; None when someone else got it first,
        or when the slot was too late and is recorded as skipped."""
        scheduled_due = task.enabled and task.next_run_at is not None and task.next_run_at <= now
        if scheduled_due:
            assert task.next_run_at is not None
            slot = task.next_run_at  # the latest missed slot <= now (catch-up target)
            try:
                spec, tz = parse_cron(task.schedule), get_zone(task.timezone)
                new_next: datetime | None = next_fire(spec, tz, slot)
                for _ in range(_CATCH_UP_SCAN_MAX):
                    if new_next is None or new_next > now:
                        break
                    slot = new_next
                    new_next = next_fire(spec, tz, slot)
                if new_next is not None and new_next <= now:  # scan cap hit
                    new_next = next_fire(spec, tz, now)
            except ScheduleError:  # a stored spec that no longer parses: stop it
                logger.warning("scheduled task %s: invalid stored schedule", task.id)
                new_next = None
            late_s = (now - slot).total_seconds()
            skipped = late_s > max_late_s
            status = "skipped" if skipped else "running"
            matched = self._session.execute(
                update(ScheduledTask)
                .where(
                    ScheduledTask.id == task.id,
                    ScheduledTask.enabled.is_(True),
                    ScheduledTask.next_run_at == task.next_run_at,
                )
                .values(
                    next_run_at=new_next,
                    last_run_at=now,
                    last_status=status,
                    # a run-now request is consumed by a real run only: a
                    # skipped (too-late) slot leaves it for the manual path
                    **({} if skipped else {"run_requested_at": None}),
                )
                .returning(ScheduledTask.id)
                .execution_options(synchronize_session=False)
            ).first()
            if matched is None:
                self._session.rollback()
                return None
            run = ScheduledTaskRun(
                id=uuid.uuid4(),
                task_id=task.id,
                trigger="schedule",
                scheduled_for=slot,
                started_at=now,
                status=status,
                finished_at=now if skipped else None,
                error=(
                    f"missed by {late_s / 3600:.1f}h (the bot was down); not run late"
                    if skipped
                    else None
                ),
                cited_ids=[],
            )
            self._session.add(run)
            self._commit()
            if not skipped:
                return Claim(run.id, task.id, task.name, task.prompt, "schedule", slot)
            # skipped slot: fall through, a pending run-now still runs
        if task.run_requested_at is None:
            return None
        matched = self._session.execute(
            update(ScheduledTask)
            .where(
                ScheduledTask.id == task.id,
                ScheduledTask.run_requested_at == task.run_requested_at,
            )
            .values(run_requested_at=None, last_run_at=now, last_status="running")
            .returning(ScheduledTask.id)
            .execution_options(synchronize_session=False)
        ).first()
        if matched is None:
            self._session.rollback()
            return None
        run = ScheduledTaskRun(
            id=uuid.uuid4(),
            task_id=task.id,
            trigger="manual",
            scheduled_for=None,
            started_at=now,
            status="running",
            cited_ids=[],
        )
        self._session.add(run)
        self._commit()
        return Claim(run.id, task.id, task.name, task.prompt, "manual", None)

    def claim_due(
        self,
        now: datetime | None = None,
        *,
        max_late_s: float,
        exclude: Sequence[uuid.UUID] = (),
        stale_after_s: float | None = None,
    ) -> list[Claim]:
        """Claim every due task not in ``exclude`` (tasks whose previous run
        is still executing wait for it); optionally reap stale 'running' rows
        first (a bot that died mid-run)."""
        moment = now or _now()
        if stale_after_s is not None:
            self.reap_stale(moment, stale_after_s)
        skip = set(exclude)
        claims: list[Claim] = []
        for task in self.due_tasks(moment):
            if task.id in skip:
                continue
            claim = self.claim(task, moment, max_late_s=max_late_s)
            if claim is not None:
                claims.append(claim)
        return claims

    def finish_run(
        self,
        run_id: uuid.UUID,
        *,
        status: str,
        output: str | None = None,
        error: str | None = None,
        cited_ids: Sequence[str] = (),
        now: datetime | None = None,
    ) -> bool:
        """Close a 'running' run (first writer wins: a timeout that already
        closed it makes the late worker's write a no-op -> False)."""
        moment = now or _now()
        row = self._session.execute(
            update(ScheduledTaskRun)
            .where(ScheduledTaskRun.id == run_id, ScheduledTaskRun.status == "running")
            .values(
                status=status,
                output=output,
                error=error[:_ERROR_MAX_CHARS] if error else None,
                cited_ids=[str(item) for item in cited_ids],
                finished_at=moment,
            )
            .returning(ScheduledTaskRun.task_id)
            .execution_options(synchronize_session=False)
        ).first()
        if row is None:
            self._session.rollback()
            return False
        self._session.execute(
            update(ScheduledTask)
            .where(ScheduledTask.id == row[0])
            .values(last_status=status)
            .execution_options(synchronize_session=False)
        )
        self._commit()
        return True

    def reap_stale(self, now: datetime, older_than_s: float) -> int:
        """Close 'running' rows older than the deadline (bot died mid-run)."""
        cutoff = now - timedelta(seconds=older_than_s)
        rows = self._session.execute(
            update(ScheduledTaskRun)
            .where(ScheduledTaskRun.status == "running", ScheduledTaskRun.started_at < cutoff)
            .values(status="error", error="interrupted (the bot stopped mid-run)", finished_at=now)
            .returning(ScheduledTaskRun.task_id)
            .execution_options(synchronize_session=False)
        ).all()
        task_ids = {row[0] for row in rows}
        if task_ids:
            self._session.execute(
                update(ScheduledTask)
                .where(ScheduledTask.id.in_(task_ids), ScheduledTask.last_status == "running")
                .values(last_status="error")
                .execution_options(synchronize_session=False)
            )
        self._commit()
        return len(rows)


StoreFactory = Callable[[], AbstractContextManager[ScheduledTaskStore]]


@contextmanager
def default_store() -> Iterator[ScheduledTaskStore]:
    """A store on a fresh session (the bot's runner; one per call/thread)."""
    from brain.db.session import SessionLocal  # noqa: PLC0415

    session = SessionLocal()
    try:
        yield ScheduledTaskStore(session)
    finally:
        session.close()


def execute_claim(
    store_factory: StoreFactory,
    claim: Claim,
    ask: Callable[[str], Any],
) -> RunOutcome:
    """Run one claimed task through ``ask`` and record the result.

    BLOCKING (the LLM client may call asyncio.run): call it from a worker
    thread, never on the event loop. Never raises for an ask failure — the
    run row records it; a DB failure while recording propagates.
    """
    from brain.ai.ask import LLMError  # noqa: PLC0415

    try:
        result = ask(claim.prompt)
    except Exception as exc:  # noqa: BLE001 — one task's failure is its own
        logger.warning("scheduled task %s failed: %s", claim.task_id, type(exc).__name__)
        message = f"{type(exc).__name__}: {exc!s}"
        with store_factory() as store:
            recorded = store.finish_run(claim.run_id, status="error", error=message)
        return RunOutcome(
            claim,
            "error",
            error=message,
            llm_unavailable=isinstance(exc, LLMError),
            recorded=recorded,
        )
    cited = [str(item) for item in (getattr(result, "cited_ids", None) or [])]
    with store_factory() as store:
        recorded = store.finish_run(
            claim.run_id, status="ok", output=str(result.answer), cited_ids=cited
        )
    return RunOutcome(claim, "ok", result=result, recorded=recorded, cited_ids=cited)


def task_payload(task: ScheduledTask, lang: str = "en") -> dict[str, Any]:
    """The API shape of one task (schedule_text = the schedule in words)."""
    return {
        "id": task.id,
        "name": task.name,
        "prompt": task.prompt,
        "schedule": task.schedule,
        "schedule_text": describe(task.schedule, lang),
        "timezone": task.timezone,
        "enabled": task.enabled,
        "next_run_at": task.next_run_at,
        "last_run_at": task.last_run_at,
        "last_status": task.last_status,
        "run_requested": task.run_requested_at is not None,
        "created_at": task.created_at,
        "updated_at": task.updated_at,
    }
