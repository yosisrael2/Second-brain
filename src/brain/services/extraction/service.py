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
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from brain import ops
from brain.db.enums import Actionability, ItemStatus, ItemType
from brain.db.models import EnrichmentJob, ItemSource, MemoryItem
from brain.services.extraction import dates as xdates
from brain.services.extraction.base import Extractor, StubExtractor
from brain.services.extraction.candidates import ChildCandidate
from brain.services.items import ItemsRepositoryProto
from brain.services.memory import MemoryService
from brain.services.textnorm import quote_in_source, title_similarity

logger = logging.getLogger(__name__)

_JOB_STATES = ("queued", "running", "done", "failed")
_BUDGETS = ("daily", "backfill")
# Re-run identity (items 18+30): 'is this the same real-world thing' is a
# STRICTER question than the eval harness's gold-pairing (which already
# filters by type/date/applies_to and picks the best pair greedily), so the
# threshold shares the FUNCTION with the eval but NOT its number
# (DEFAULT_TITLE_THRESHOLD = 0.3). Measured failure mode at 0.3-0.6:
# 'חולצה לבנה' vs 'להביא סרגל' = 0.40, 'Bring a reading book' vs
# 'Bring a ruler' = 0.61 — two DIFFERENT same-day reminders that must stay
# two children. 0.7 keeps those apart while the reworded duplicate
# ('דיאלוג בנות 13:35' vs 'דיאלוג בנות, איסוף 13:35' = 0.83) still matches.
_CHILD_IDENTITY_THRESHOLD = 0.7
# timed children match within one hour of resolved start (delta item 18)
_TIMED_MATCH_WINDOW = timedelta(hours=1)

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


@dataclass(frozen=True)
class SourceOutcome:
    """One source's enrichment result in a run (the completion hook's payload).

    The proposal-card layer (PR C) turns these into one bot card per source.
    """

    source: MemoryItem
    created: int
    updated: int


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
        on_children_changed: Callable[[list[SourceOutcome]], None] | None = None,
    ) -> None:
        """``session`` is the runner's DB session (same one behind ``repo``).

        ``indexer`` is optional (Strategy, like ItemsService): new children
        are indexed best-effort; failures are logged and picked up by the
        next ``brain reindex``.

        ``on_children_changed`` is the PR C completion hook: after a
        ``run_once`` pass it receives one :class:`SourceOutcome` per source
        whose children were created/updated (never per quiet source). A
        raising hook is logged and swallowed — a notification must never
        break the run.
        """
        self._repo = repo
        self._session = session
        self._extractor: Extractor = extractor if extractor is not None else StubExtractor()
        self._memory = memory
        self._indexer = indexer
        self._daily_cap = daily_cap
        self.on_children_changed = on_children_changed
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
        job_outcomes: list[SourceOutcome] = []
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
            report.add(self._process_job(job, now, job_outcomes))
        self._fire_children_changed(job_outcomes)
        return report

    # --- internals ---------------------------------------------------------

    def _fire_children_changed(self, outcomes: list[SourceOutcome]) -> None:
        """The PR C completion hook (never raises into the runner)."""
        if not outcomes or self.on_children_changed is None:
            return
        try:
            self.on_children_changed(outcomes)
        except Exception:  # noqa: BLE001 — a notification must never break a run
            logger.exception("enrichment: on_children_changed hook failed")

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

    def _process_job(
        self, job: EnrichmentJob, now: datetime, outcomes: list[SourceOutcome]
    ) -> RunReport:
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
        if report.children_created or report.children_updated:
            # the completion hook only hears about sources whose children CHANGED
            outcomes.append(
                SourceOutcome(
                    source=source,
                    created=report.children_created,
                    updated=report.children_updated,
                )
            )
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
        """Grounding guard first (item 8); then dates (2b/41); then applies_to (42).

        Derived children (item 22) cite the fact they derive from, not the
        source: an EMPTY quote is allowed only for them and skips the
        grounding guard; a NON-derived empty quote is still dropped, and a
        derived child WITH a quote is verified normally (eval doc).
        """
        if candidate.quote:
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
        elif candidate.derived_from is None:
            logger.info(
                "enrichment: dropped empty-quote non-derived candidate %r from %s",
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
            "prompt_version": getattr(
                self._extractor, "prompt_version", None
            ),
            "run_id": None,  # set at storage time
        }
        if candidate.derived_from is not None:
            # item 22: interim link until PR B's item_relations table.
            # EXACT SHAPE: meta["derived_from"] is the 0-based INDEX of the
            # source candidate WITHIN THIS EXTRACTION PASS's candidate list
            # (the LLM's own numbering — stable only within the pass, not a
            # child id). PR B (worker_3) will convert this write path to a
            # real relation row keyed by child id.
            meta["derived_from"] = candidate.derived_from
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
    def _same_child(
        candidate: ChildCandidate,
        moment: datetime | None,
        applies_to: list[str],
        child: MemoryItem,
    ) -> bool:
        """The spec's re-run identity (items 18 + 30), not an exact title.

        A candidate matches an existing child of the same source when:
        - the KIND is the same (child.type stores candidate.kind);
        - the resolved date is the same — timed children within ±1h;
          dateless children only match dateless ones;
        - the resolved applies_to SET is the same;
        - the title similarity >= _CHILD_IDENTITY_THRESHOLD (stricter than
          the eval pairing threshold — see the constant's comment; the
          FUNCTION is shared with the eval, only the number differs).
        """
        if child.type is None or child.type.value != candidate.kind:
            return False
        if (child.occurred_at is None) != (moment is None):
            return False
        if moment is not None and child.occurred_at is not None:
            delta = abs((child.occurred_at - moment).total_seconds())
            if delta > _TIMED_MATCH_WINDOW.total_seconds():
                return False
        stored = set(child.applies_to or [])
        candidate_set = set(applies_to)
        if stored != candidate_set:
            # the ONE allowed mismatch: the stored child was unresolved
            # (['unknown']) and memory has since improved — the candidate is
            # still the same child and updates it in place (item 18). Two
            # different KNOWN audiences (Rona vs Noa) never match.
            if stored != {"unknown"}:
                return False
        return title_similarity(child.raw_text, candidate.title) >= _CHILD_IDENTITY_THRESHOLD

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
        moment = resolved.start or resolved.due
        kind = ItemType(candidate.kind)  # validated child kind (spec section 2)
        matches = [
            child
            for child in existing_children.values()
            if self._same_child(candidate, moment, resolved.applies_to, child)
        ]
        if matches:
            # several children may qualify; the MOST similar one is the same
            # real-world thing (review round 2) — the others stay untouched
            best = max(
                matches,
                key=lambda child: title_similarity(child.raw_text, candidate.title),
            )
            if best.child_state == "dropped":
                return "skipped"  # remembered rejection: never reappears (item 30)
            if best.child_state == "kept":
                return "skipped"  # user acted: immutable (item 18)
            # untouched proposed child: update in place
            self._apply_candidate_to_item(best, resolved, source, run_id)
            self._repo.commit()
            return "updated"
        child = MemoryItem(
            raw_text=candidate.title,
            source=source.source,
            type=kind,  # the candidate's kind survives (review round 1, defect 1)
            occurred_at=moment,
            importance=candidate.importance,
            actionability=(
                Actionability(candidate.actionability)
                if candidate.actionability is not None
                else None
            ),
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
        existing_children[child.id] = child  # visible to later candidates this run
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
        child.importance = resolved.candidate.importance
        child.actionability = (
            Actionability(resolved.candidate.actionability)
            if resolved.candidate.actionability is not None
            else None
        )
        child.applies_to = resolved.applies_to
        child.date_conflict = resolved.date_conflict
        child.status = ItemStatus.NEEDS_REVIEW if resolved.date_conflict else child.status
        child.extraction_meta = {**resolved.meta, "run_id": str(run_id)}
        # one link per (child, source): a DIFFERENT quote of the same pair is
        # an extra quote, not a second link row (UNIQUE(child_id, source_id))
        link = (
            self._session.query(ItemSource)
            .filter(
                ItemSource.child_id == child.id, ItemSource.source_id == source.id
            )
            .one_or_none()
        )
        if link is not None and link.quote != resolved.candidate.quote:
            extras = list(child.extraction_meta.get("extra_quotes", []))
            if resolved.candidate.quote not in extras:
                extras.append(resolved.candidate.quote)
            child.extraction_meta["extra_quotes"] = extras


@dataclass(frozen=True)
class _ResolvedChild:
    """A candidate that passed grounding, with resolved dates and audience."""

    candidate: ChildCandidate
    start: datetime | None
    due: datetime | None
    applies_to: list[str]
    date_conflict: bool
    meta: dict[str, Any]
