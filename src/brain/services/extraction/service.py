"""Enrichment queue + runner: capture -> child items, never blocking a reply.

PR A1 (brief): the runner drains a durable queue oldest-first under a daily
LLM cap (D5: over cap -> job deferred to tomorrow; backfill on its own
budget, D6), re-runs DIFF instead of overwrite (item 18: user-acted children
are immutable, dropped ones never reappear — item 30), and every child
passes the validation pipeline before storage:

- grounding: the candidate's quote must appear verbatim in the source
  (textnorm.quote_in_source, item 8); ungrounded candidates are dropped and
  logged, never stored (anti-hallucination);
- dates: Hebrew calendar via pyluach, numeric/relative resolved against the
  SOURCE's sent time in Settings.timezone (item 41); weekday-vs-date
  disagreement flags date_conflict and the child lands in needs_review
  (item 2b) with both readings in extraction_meta;
- applies_to: MemoryService.resolve_applies_to (R1-R5, item 42); unknown
  stays unknown, never 'nobody'.

The extractor itself is the pluggable :class:`~brain.services.extraction.base.Extractor`
(A1: deterministic stub; A2: LLM). The capture reply is NEVER touched:
ingest only enqueues, all real work happens later in ``run_once``.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from brain import ops
from brain.db.enums import ItemStatus
from brain.db.models import EnrichmentJob, ItemSource, MemoryItem
from brain.services.extraction import dates as xdates
from brain.services.extraction.base import Extractor, StubExtractor
from brain.services.extraction.candidates import ChildCandidate
from brain.services.items import ItemsRepositoryProto
from brain.services.memory import MemoryService
from brain.services.textnorm import quote_in_source

logger = logging.getLogger(__name__)

_JOB_STATES = ("queued", "running", "done", "failed")
_BUDGETS = ("daily", "backfill")

# D1(b): emails and long/multi-date text get the extraction pass; short
# plain notes keep Jev-only labels (nothing to extract worth a model call).
_EMAIL_SOURCES = frozenset({"email", "outlook"})
_LONG_TEXT_CHARS = 300
_MIN_DATE_EXPRESSIONS = 2


def _dateish_expressions(text: str) -> int:
    """Count date-looking expressions (numeric dates + Hebrew month names)."""
    return len(xdates.DATEISH_RE.findall(text))


def qualifies(item: MemoryItem) -> bool:
    """D1(b): which captures get the extraction pass."""
    if item.source in _EMAIL_SOURCES:
        return True
    if len(item.raw_text) > _LONG_TEXT_CHARS:
        return True
    return _dateish_expressions(item.raw_text) >= _MIN_DATE_EXPRESSIONS


@dataclass
class RunReport:
    """What one run_once pass did (also the cockpit/ops payload's core)."""

    processed: int = 0
    children_created: int = 0
    children_updated: int = 0
    dropped_ungrounded: int = 0
    deferred_over_cap: int = 0
    failed: int = 0

    def add(self, other: RunReport) -> RunReport:
        for name in (
            "processed",
            "children_created",
            "children_updated",
            "dropped_ungrounded",
            "deferred_over_cap",
            "failed",
        ):
            setattr(self, name, getattr(self, name) + getattr(other, name))
        return self


class EnrichmentService:
    """Owns the queue (enqueue) and the extraction pass (run_once)."""

    def __init__(
        self,
        repo: ItemsRepositoryProto,
        session: Session,
        extractor: Extractor | None = None,
        memory: MemoryService | None = None,
        *,
        indexer: Any | None = None,
        daily_cap: int = 100,
        timezone: str | None = None,
    ) -> None:
        """``session`` is the runner's DB session (same one behind ``repo``).

        ``indexer`` is optional (Strategy, like ItemsService): new children
        are indexed best-effort; failures are logged and picked up by the
        next ``brain reindex``.
        """
        self._repo = repo
        self._session = session
        self._extractor: Extractor = extractor if extractor is not None else StubExtractor()
        self._memory = memory
        self._indexer = indexer
        self._daily_cap = daily_cap
        if timezone is None:
            from brain.config import get_settings  # noqa: PLC0415 (cheap, local)

            timezone = get_settings().timezone
        self._tz_name = timezone

    # --- queue ---------------------------------------------------------

    def enqueue(self, item: MemoryItem, *, budget: str = "daily") -> EnrichmentJob | None:
        """Queue one parent for extraction; idempotent per item (unique id).

        Returns the job (existing or new) or None when the capture does not
        qualify (D1(b)) — a no-op that never blocks the capture reply.
        """
        if not qualifies(item):
            return None
        existing = (
            self._session.query(EnrichmentJob)
            .filter(EnrichmentJob.item_id == item.id)
            .one_or_none()
        )
        if existing is not None:
            if existing.state == "failed":  # a fresh capture re-arms failures
                existing.state = "queued"
                existing.last_error = None
                self._session.commit()
            return existing
        job = EnrichmentJob(item_id=item.id, budget=budget if budget in _BUDGETS else "daily")
        self._session.add(job)
        self._session.commit()
        ops.record_event(
            "enrichment",
            "queued",
            ok=True,
            detail={"item_id": str(item.id), "budget": job.budget},
        )
        return job

    # --- accounting ------------------------------------------------------

    def queue_depth(self) -> int:
        """Jobs waiting to run right now (ops: queue depth)."""
        return (
            self._session.query(EnrichmentJob)
            .filter(EnrichmentJob.state == "queued")
            .count()
        )

    def cap_used_today(self, now: datetime) -> int:
        """LLM extractions charged to TODAY's daily budget (D5 accounting)."""
        day_start = now.astimezone(self._tz).replace(hour=0, minute=0, second=0, microsecond=0)
        return (
            self._session.query(EnrichmentJob)
            .filter(
                EnrichmentJob.budget == "daily",
                EnrichmentJob.llm_used.is_(True),
                EnrichmentJob.finished_at.is_not(None),
                EnrichmentJob.finished_at >= day_start,
            )
            .count()
        )

    # --- the runner ------------------------------------------------------

    def run_once(self, now: datetime, *, limit: int = 20, backfill: bool = False) -> RunReport:
        """Drain due jobs oldest-first, respecting the daily cap (D5).

        ``backfill=True`` processes backfill-budget jobs (D6: their own
        budget, so a one-off backfill never eats the live daily cap).
        """
        report = RunReport()
        budget = "backfill" if backfill else "daily"
        while report.processed < limit:
            job = self._next_due_job(now, budget)
            if job is None:
                break
            if not backfill and self.cap_used_today(now) >= self._daily_cap:
                # D5: over the cap -> defer to tomorrow, keep queue order
                job.not_before = self._next_day_start(now)
                self._session.commit()
                report.deferred_over_cap += 1
                break
            report.add(self._process_job(job, now))
        return report

    # --- internals ---------------------------------------------------------

    @property
    def _tz(self):  # type: ignore[no-untyped-def]
        from zoneinfo import ZoneInfo  # noqa: PLC0415

        return ZoneInfo(self._tz_name)

    def _next_due_job(self, now: datetime, budget: str) -> EnrichmentJob | None:
        stmt = (
            select(EnrichmentJob)
            .where(EnrichmentJob.state == "queued")
            .where(EnrichmentJob.budget == budget)
            .order_by(EnrichmentJob.queued_at.asc(), EnrichmentJob.id.asc())
            .limit(25)
        )
        for job in self._session.scalars(stmt):
            if job.not_before is None or job.not_before <= now:
                return job
        return None

    def _next_day_start(self, now: datetime) -> datetime:
        local = now.astimezone(self._tz)
        tomorrow = (local + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
        return tomorrow

    def _process_job(self, job: EnrichmentJob, now: datetime) -> RunReport:
        report = RunReport(processed=1)
        source = self._repo.get(job.item_id)
        if source is None:
            job.state = "failed"
            job.last_error = "source item vanished"
            job.finished_at = now
            self._session.commit()
            report.failed += 1
            return report
        job.state = "running"
        job.started_at = now
        self._session.commit()
        try:
            memory_context = self._memory.compact_block() if self._memory is not None else ""
            candidates = self._extractor.extract(source, memory_context)
        except Exception as exc:  # noqa: BLE001 — extraction must never crash the caller
            logger.warning("enrichment: extractor failed for %s (%s)", source.id, exc)
            job.state = "failed"
            job.attempts += 1
            job.last_error = f"{type(exc).__name__}: {exc}"
            job.finished_at = now
            self._session.commit()
            ops.record_event(
                "enrichment", "run_failed", ok=False, detail={"item_id": str(source.id)}
            )
            report.failed += 1
            return report
        run_id = uuid.uuid4()
        existing_children = self._children_of(source.id)
        for candidate in candidates:
            resolved = self._validate(candidate, source, now)
            if resolved is None:
                report.dropped_ungrounded += 1
                continue
            outcome = self._store_child(resolved, source, existing_children, run_id, now)
            if outcome == "created":
                report.children_created += 1
            elif outcome == "updated":
                report.children_updated += 1
        job.state = "done"
        job.llm_used = True  # the pass consumed an extraction (A2: a real LLM call)
        job.finished_at = now
        self._session.commit()
        ops.record_event(
            "enrichment",
            "ran",
            ok=True,
            detail={
                "item_id": str(source.id),
                "created": report.children_created,
                "updated": report.children_updated,
                "dropped": report.dropped_ungrounded,
            },
        )
        return report

    # --- validation pipeline ---------------------------------------------

    def _validate(
        self, candidate: ChildCandidate, source: MemoryItem, now: datetime
    ) -> _ResolvedChild | None:
        """Grounding guard first (item 8); then dates (2b/41); then applies_to (42)."""
        if not quote_in_source(candidate.quote, source.raw_text):
            logger.info(
                "enrichment: dropped ungrounded candidate %r from %s",
                candidate.title[:60],
                source.id,
            )
            ops.record_event(
                "enrichment",
                "ungrounded_dropped",
                ok=True,
                detail={"item_id": str(source.id)},
            )
            return None
        start: datetime | None = None
        due: datetime | None = None
        date_conflict = False
        conflict_note = None
        if candidate.when_raw:
            sent_local = self._source_sent_local(source)
            resolved = xdates.resolve_when(candidate.when_raw, sent_local, self._tz)
            if resolved is not None:
                clock = xdates.extract_clock(candidate.when_raw)
                moment = xdates.combine_local(resolved.day, clock, self._tz)
                date_conflict = resolved.date_conflict
                conflict_note = resolved.conflict_note
                if candidate.due:
                    due = moment
                else:
                    start = moment
        applies_to = (
            self._memory.resolve_applies_to(dict(candidate.audience))
            if self._memory is not None
            else ["unknown"]
        )
        meta: dict[str, Any] = {
            "when_raw": candidate.when_raw,
            "model": getattr(self._extractor, "name", "stub"),
            "run_id": None,  # set at storage time
        }
        if conflict_note:
            meta["date_conflict_note"] = conflict_note  # both readings survive (item 2b)
        return _ResolvedChild(
            candidate=candidate,
            start=start,
            due=due,
            applies_to=applies_to,
            date_conflict=date_conflict,
            meta=meta,
        )

    def _source_sent_local(self, source: MemoryItem) -> datetime:
        """The source's sent time in the user's timezone (item 41).

        occurred_at carries the email's sent time when known (UTC in the DB);
        fallback: the capture time. Converting an aware datetime with
        astimezone(tz) applies the CORRECT offset for that date — DST-safe.
        """
        moment = source.occurred_at or source.created_at
        if moment.tzinfo is None:  # legacy zone-less rows: already local wall clock
            moment = moment.replace(tzinfo=self._tz)
        return moment.astimezone(self._tz)

    # --- storage + diff (item 18, item 30) ----------------------------------

    def _children_of(self, source_id: uuid.UUID) -> dict[uuid.UUID, MemoryItem]:
        rows = self._session.execute(
            select(MemoryItem)
            .join(ItemSource, ItemSource.child_id == MemoryItem.id)
            .where(ItemSource.source_id == source_id)
        ).scalars().all()
        return {row.id: row for row in rows}

    @staticmethod
    def _identity_key(candidate: ChildCandidate) -> tuple[str, str]:
        """(kind, normalized title) — the diff key within one source (item 18)."""
        from brain.services.textnorm import normalize  # noqa: PLC0415

        return (candidate.kind, normalize(candidate.title))

    def _store_child(
        self,
        resolved: _ResolvedChild,
        source: MemoryItem,
        existing_children: dict[uuid.UUID, MemoryItem],
        run_id: uuid.UUID,
        now: datetime,
    ) -> str:
        """Diff-and-store: create new, update untouched proposed, respect user actions.

        - a kept child is IMMUTABLE (item 18): the user approved it as-is;
        - a dropped child SUPPRESSES the matching candidate (item 30);
        - an untouched proposed child may be updated in place.
        Returns 'created' | 'updated' | 'skipped'.
        """
        candidate = resolved.candidate
        key = self._identity_key(candidate)
        for child in existing_children.values():
            child_key = (
                (child.type.value if child.type else "event"),
                _normalized(child.raw_text),
            )
            if child_key != key:
                continue
            if child.child_state == "dropped":
                return "skipped"  # remembered rejection: never reappears (item 30)
            if child.child_state == "kept":
                return "skipped"  # user acted: immutable (item 18)
            # untouched proposed child: update in place
            self._apply_candidate_to_item(child, resolved, source, run_id)
            self._repo.commit()
            return "updated"
        child = MemoryItem(
            raw_text=candidate.title,
            source=source.source,
            type="event",  # refined by A2's typed candidates; search-visible now
            occurred_at=resolved.start or resolved.due,
            status=ItemStatus.NEEDS_REVIEW if resolved.date_conflict else ItemStatus.INBOX,
            child_state="proposed",
            applies_to=resolved.applies_to,
            date_conflict=resolved.date_conflict,
            extraction_meta={**resolved.meta, "run_id": str(run_id)},
        )
        self._repo.add(child)
        self._session.flush()  # assign child.id before linking
        self._session.add(
            ItemSource(child_id=child.id, source_id=source.id, quote=candidate.quote)
        )
        self._repo.commit()
        self._index_child(child)
        return "created"

    def _index_child(self, child: MemoryItem) -> None:
        """Best-effort vector indexing for a new child (never fails the run)."""
        if self._indexer is None:
            return
        try:
            self._indexer.index(child)
        except Exception as exc:  # noqa: BLE001 — same fallback policy as ingest
            logger.warning(
                "enrichment: indexing failed for child %s (%s); deferred to 'brain reindex'",
                child.id,
                exc,
            )

    def _apply_candidate_to_item(
        self, child: MemoryItem, resolved: _ResolvedChild, source: MemoryItem, run_id: uuid.UUID
    ) -> None:
        candidate = resolved.candidate
        child.raw_text = candidate.title
        child.occurred_at = resolved.start or resolved.due
        child.applies_to = resolved.applies_to
        child.date_conflict = resolved.date_conflict
        child.status = ItemStatus.NEEDS_REVIEW if resolved.date_conflict else child.status
        child.extraction_meta = {**resolved.meta, "run_id": str(run_id)}


@dataclass(frozen=True)
class _ResolvedChild:
    """A candidate that passed grounding, with resolved dates and audience."""

    candidate: ChildCandidate
    start: datetime | None
    due: datetime | None
    applies_to: list[str]
    date_conflict: bool
    meta: dict[str, Any]


def _normalized(text: str) -> str:
    from brain.services.textnorm import normalize  # noqa: PLC0415

    return normalize(text)
