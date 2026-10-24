"""Reconciliation (PR D-recon-1, spec item 37): classify an extracted child
against existing items BEFORE it becomes a proposal, so the user is never
shown duplicates or silent contradictions.

Exactly one verdict per candidate (delta item 37):

- NEW            -> proposed child (today's path, byte-identical).
- ALREADY KNOWN  -> no new item; the source is linked to the existing item
                    via item_sources (many-to-many, #39).
- UPDATE         -> a PROPOSED change to the existing item (old -> new per
                    field); auto-applies ONLY same-thread + same-sender
                    (item 20a) and always shown with Undo.
- MORE DETAIL    -> a proposed addition linked to the existing item.
- CONFLICT       -> both versions kept, linked for Ask to surface (item 38).
- CANCELLATION   -> proposal to cancel the existing item (D4: strike-through,
                    restorable, never deleted).

NOTHING is ever applied silently: the only auto-path is the item-20a
same-thread correction, which is always rendered with an undo.

The PURE core (Verdict, classify, detect_signal) runs against an in-memory
list of existing items — no DB — so ``brain recon-eval`` can score it offline
against a cases file. :class:`ReconciliationService` is the thin DB adapter
(fetch the universe, write relations/links/events).

Matching (thread recon/plan, manager veto 24 Oct 2026) — split the WINDOW,
not the verdict. Both windows always require, code-checked:
same type ∧ same applies_to set ∧ similarity >= relations.SIMILARITY_THRESHOLD.

- SAME LOCAL DAY window (no signal required): the existing item's LOCAL day
  (Settings.timezone, item 41) equals the candidate's local day. Covers the
  item-21 identity (±1h on aware instants; dateless matches dateless) AND
  same-day time changes — which is what makes CONFLICT reachable.
- EXTENDED window [today, today+30d] (change/cancel SIGNAL required): for
  dateless candidates or candidates stating a MOVED date — the existing
  item's local day falls inside. A signal-less candidate never gets it.

Verdict order (first match wins; a cancellation that also "changes" fields
cancels): 1 NEW (no match) / 2 CANCELLATION (cancel signal ∧ match) /
3 UPDATE (differing field ∧ change signal) / 4 CONFLICT (differing field ∧
no signal; same-day window only) / 5 MORE DETAIL (±1h, nothing new, adds
info) / 6 ALREADY KNOWN (±1h, nothing new).

Hints (extractor v7) only gate the extended window and feed the signal —
they can never create a match (type/applies_to/similarity stay code-checked).
Several matches: the best wins only with a >= 0.05 similarity margin over the
runner-up; otherwise NEW + an ops event (never pick silently).
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Any
from zoneinfo import ZoneInfo

from brain.services.relations import DATE_TOLERANCE, SIMILARITY_THRESHOLD, similarity_ratio

# Item 20a/30 signals, code-detected (the LLM only narrows, never creates).
# Substrings, casefolded; Hebrew morphological families share the root.
_CHANGE_PHRASES = ("תיקון", "עדכון", "שינוי", "correction", "update:", "change")
_CANCEL_PHRASES = ("בוטל", "מבוטל", "ביטול", "cancelled", "canceled")

# The several-matches guard: best must beat the runner-up by this much.
AMBIGUITY_MARGIN = 0.05

EXTENDED_WINDOW_DAYS = 30


class Verdict(StrEnum):
    NEW = "new"
    ALREADY_KNOWN = "already_known"
    UPDATE = "update"
    MORE_DETAIL = "more_detail"
    CONFLICT = "conflict"
    CANCELLATION = "cancellation"


@dataclass(frozen=True)
class ReconSignal:
    """A change/cancel signal: code-detected phrases OR extractor hints."""

    change: bool = False
    cancel: bool = False

    @property
    def any(self) -> bool:
        return self.change or self.cancel


@dataclass(frozen=True)
class ExistingItem:
    """The offline shape of one matchable item (mirrors memory_items)."""

    id: str
    type: str
    title: str
    start: datetime | None = None
    end: datetime | None = None
    applies_to: tuple[str, ...] = ()
    location: str | None = None


@dataclass(frozen=True)
class ReconCandidate:
    """The offline shape of the extracted candidate (mirrors ChildCandidate)."""

    kind: str
    title: str
    start: datetime | None = None
    end: datetime | None = None
    applies_to: tuple[str, ...] = ()
    location: str | None = None


@dataclass(frozen=True)
class ReconResult:
    """classify()'s verdict: which existing item (if any) and the field diffs."""

    verdict: Verdict
    matched_id: str | None = None
    # field -> (old, new) for time/end/location (UPDATE/CONFLICT evidence)
    fields: dict[str, tuple[str, str]] = field(default_factory=dict)
    ambiguous: bool = False
    runner_up_id: str | None = None


def _norm(value: str | None) -> str:
    return " ".join(str(value or "").split()).casefold()


def detect_signal(
    text: str, *, hint_change: bool = False, hint_cancel: bool = False
) -> ReconSignal:
    """Code-detected change/cancel phrases ORed with the extractor's hints.

    The phrases are SUBSTRINGS on the normalized text (the LLM's hint can
    add a signal, never remove one; and a hint alone can never create a
    match — classify() still demands type + applies_to + similarity).
    """
    lowered = _norm(text)
    change = hint_change or any(p in lowered for p in _CHANGE_PHRASES)
    cancel = hint_cancel or any(p in lowered for p in _CANCEL_PHRASES)
    return ReconSignal(change=change, cancel=cancel)


def _clock_stated(moment: datetime, tz: ZoneInfo) -> bool:
    """A date-only resolution lands at local midnight — no time was stated."""
    return moment.astimezone(tz).time() != datetime(2000, 1, 1).time()


def _same_local_day(candidate: ReconCandidate, existing: ExistingItem, tz: ZoneInfo) -> bool:
    """The SAME LOCAL DAY window: no signal required.

    Dateless matches dateless (item 21); a dated pair matches when both land
    on the same local day. A date-only candidate (midnight) matches the day.
    Mixed dateless/dated never matches here.
    """
    if candidate.start is None and existing.start is None:
        return True
    if candidate.start is None or existing.start is None:
        return False
    return candidate.start.astimezone(tz).date() == existing.start.astimezone(tz).date()


def _in_extended_window(
    candidate: ReconCandidate, existing: ExistingItem, now: datetime, tz: ZoneInfo
) -> bool:
    """The EXTENDED window [today, today+30d] (local days); signal required.

    For dateless candidates or candidates stating a MOVED date: the existing
    item must be upcoming within 30 local days.
    """
    if existing.start is None:
        return False
    today = now.astimezone(tz).date()
    existing_day = existing.start.astimezone(tz).date()
    return today <= existing_day <= today + timedelta(days=EXTENDED_WINDOW_DAYS)


def _field_diffs(
    candidate: ReconCandidate, existing: ExistingItem, tz: ZoneInfo
) -> dict[str, tuple[str, str]]:
    """Old -> new per field, ONLY where the candidate states a value that
    differs BEYOND the identity tolerance: the +/-1h window IS the item-21
    identity (rows 5/6), so only a bigger move is a change. A field the
    candidate does not state is never a change (a date-only mention is not a
    midnight appointment)."""

    def fmt(moment: datetime) -> str:
        local = moment.astimezone(tz)
        same_day = (
            candidate.start is not None
            and existing.start is not None
            and candidate.start.astimezone(tz).date() == existing.start.astimezone(tz).date()
        )
        pattern = "%H:%M" if same_day and _clock_stated(moment, tz) else "%d.%m %H:%M"
        return local.strftime(pattern)

    beyond = DATE_TOLERANCE.total_seconds()
    diffs: dict[str, tuple[str, str]] = {}
    if (
        candidate.start is not None
        and existing.start is not None
        and _clock_stated(candidate.start, tz)
        and _clock_stated(existing.start, tz)
        and abs((candidate.start - existing.start).total_seconds()) > beyond
    ):
        diffs["time"] = (fmt(existing.start), fmt(candidate.start))
    if (
        candidate.end is not None
        and existing.end is not None
        and abs((candidate.end - existing.end).total_seconds()) > beyond
    ):
        diffs["end"] = (fmt(existing.end), fmt(candidate.end))
    if (
        candidate.location is not None
        and existing.location is not None
        and _norm(candidate.location) != _norm(existing.location)
    ):
        diffs["location"] = (existing.location, candidate.location)
    return diffs


def _adds_info(candidate: ReconCandidate, existing: ExistingItem) -> bool:
    """Nothing differs, but the candidate carries extra text (a dress code,
    what to bring). Deterministic: a longer title than the stored one."""
    return len(_norm(candidate.title)) > len(_norm(existing.title))


def _compatible(
    candidate: ReconCandidate, existing: ExistingItem, similarity: Callable[[str, str], float]
) -> bool:
    """The always-on guards: same type ∧ same applies_to ∧ similar enough.

    R5 (items 21/42): two different KNOWN audiences never match; unknown ==
    unknown is allowed (memory may have improved since). Whether a dated and
    a dateless pair can meet is the WINDOWS' business, not a guard here.
    """
    if existing.type != candidate.kind:
        return False
    candidate_applies = tuple(sorted(candidate.applies_to))
    existing_applies = tuple(sorted(existing.applies_to))
    if candidate_applies != existing_applies:
        return False
    return similarity(candidate.title, existing.title) >= SIMILARITY_THRESHOLD


def classify(
    candidate: ReconCandidate,
    existing_items: list[ExistingItem],
    *,
    similarity: Callable[[str, str], float] = similarity_ratio,
    now: datetime,
    tz_name: str,
    signal: ReconSignal | None = None,
) -> ReconResult:
    """One verdict for the candidate against the existing items (pure).

    Windows: same local day always; extended [today, today+30d] only with a
    signal. Verdict order per the module docstring. Several matches need a
    similarity margin >= AMBIGUITY_MARGIN or the result is NEW + ambiguous
    (the caller records the ops event; the ids travel with it).
    """
    tz = ZoneInfo(tz_name)
    signal = signal or ReconSignal()
    compatible = [e for e in existing_items if _compatible(candidate, e, similarity)]
    same_day = [e for e in compatible if _same_local_day(candidate, e, tz)]
    extended = (
        [e for e in compatible if e not in same_day and _in_extended_window(candidate, e, now, tz)]
        if signal.any
        else []
    )
    pool = same_day + extended
    if not pool:
        return ReconResult(verdict=Verdict.NEW)
    scored = sorted(
        ((similarity(candidate.title, e.title), e) for e in pool), key=lambda p: -p[0]
    )
    best_score, best = scored[0]
    if len(scored) > 1 and best_score - scored[1][0] < AMBIGUITY_MARGIN:
        return ReconResult(
            verdict=Verdict.NEW, ambiguous=True, runner_up_id=scored[1][1].id
        )
    if signal.cancel:
        return ReconResult(verdict=Verdict.CANCELLATION, matched_id=best.id)
    diffs = _field_diffs(candidate, best, tz)
    if diffs and signal.change:
        return ReconResult(verdict=Verdict.UPDATE, matched_id=best.id, fields=diffs)
    if diffs:
        # the extended window REQUIRES a signal, so a no-signal diff can only
        # come from a same-day match (the VETO's CONFLICT row)
        return ReconResult(verdict=Verdict.CONFLICT, matched_id=best.id, fields=diffs)
    if best in same_day and _adds_info(candidate, best):
        return ReconResult(verdict=Verdict.MORE_DETAIL, matched_id=best.id)
    if best in same_day:
        return ReconResult(verdict=Verdict.ALREADY_KNOWN, matched_id=best.id)
    # an extended-window match carries the signal but no field change we can
    # propose — that is not a verdict the spec defines; stay NEW (never invent).
    return ReconResult(verdict=Verdict.NEW)


# --- the DB adapter -----------------------------------------------------------


@dataclass(frozen=True)
class ReconOutcome:
    """What one child's reconciliation did (verdict + what it matched).

    The run report consumes `verdict`; `matched_id` names the existing item
    for tests and ops. Fields/child ids live on the child's extraction_meta
    and the relation row — re-derived from the DB, never duplicated here.
    """

    verdict: Verdict
    matched_id: uuid.UUID | None = None


class ReconciliationService:
    """Classifies against the DB universe and writes the verdicts: relations,
    quote links, cancellations, ops events.

    Thin on purpose: matching itself is the pure :func:`classify` (the same
    function recon-eval scores). All writes keep the standing rules — nothing
    applied silently, user-acted items immutable, rows never deleted.
    """

    def __init__(self, session: Any, repo: Any, *, tz_name: str) -> None:
        self._session = session
        self._repo = repo
        self._tz_name = tz_name

    # -- universe ---------------------------------------------------------

    def matchable_items(
        self, kind: str, exclude_ids: set[uuid.UUID]
    ) -> list[tuple[ExistingItem, Any]]:
        """Existing items a candidate may reconcile against.

        Same type; NOT dropped (item 30: only the same-source diff suppresses);
        NOT superseded (the current version matches); NOT cancelled; excludes
        the source itself and its own children (the A1 diff owns those).
        """
        from brain.db.models import MemoryItem  # noqa: PLC0415

        rows = (
            self._session.query(MemoryItem)
            .filter(MemoryItem.type == kind)
            .filter(MemoryItem.id.not_in(exclude_ids or {uuid.uuid4()}))
            .filter(MemoryItem.superseded_by.is_(None))
            .all()
        )
        out: list[tuple[ExistingItem, Any]] = []
        for row in rows:
            if row.child_state == "dropped":
                continue
            meta = row.extraction_meta if isinstance(row.extraction_meta, dict) else {}
            if meta.get("cancelled"):
                continue
            out.append(
                (
                    ExistingItem(
                        id=str(row.id),
                        type=getattr(row.type, "value", row.type) if row.type else kind,
                        title=row.raw_text,
                        start=row.occurred_at,
                        applies_to=tuple(row.applies_to or ()),
                        location=meta.get("location"),
                    ),
                    row,
                )
            )
        return out

    def classify_universe(
        self,
        candidate: ReconCandidate,
        *,
        exclude_ids: set[uuid.UUID],
        now: datetime,
        signal: ReconSignal | None = None,
    ) -> ReconResult:
        """classify() against the DB universe (the pure function, unchanged)."""
        matches = self.matchable_items(candidate.kind, exclude_ids)
        return classify(
            candidate,
            [pair[0] for pair in matches],
            similarity=similarity_ratio,
            now=now,
            tz_name=self._tz_name,
            signal=signal or ReconSignal(),
        )

    # -- same-thread / same-sender (item 20a + decision 3) ------------------

    def _sources_of(self, item_id: uuid.UUID) -> list[Any]:
        from brain.db.models import ItemSource, MemoryItem  # noqa: PLC0415

        return (
            self._session.query(MemoryItem)
            .join(ItemSource, ItemSource.source_id == MemoryItem.id)
            .filter(ItemSource.child_id == item_id)
            .all()
        )

    @staticmethod
    def _thread_key(source: Any) -> str | None:
        """The normalized subject minus reply/forward/correction prefixes."""
        meta = source.meta if isinstance(source.meta, dict) else {}
        subject = meta.get("subject")
        if not isinstance(subject, str) or not subject.strip():
            return None
        text = subject.strip()
        while True:
            lowered = text.casefold()
            for prefix in ("re:", "fw:", "fwd:", "תיקון:", "עדכון:", "שינוי:"):
                if lowered.startswith(prefix):
                    text = text[len(prefix):].strip()
                    break
            else:
                return _norm(text)

    def _auto_apply_allowed(self, source: Any, existing_item_id: str) -> bool:
        """Same thread AND same sender as the existing item's source (20a)."""
        current_thread = self._thread_key(source)
        current_meta = source.meta if isinstance(source.meta, dict) else {}
        current_sender = _norm(current_meta.get("sender") or "")
        for other in self._sources_of(uuid.UUID(existing_item_id)):
            if other.id == source.id:
                return True  # the very same source item
            if current_thread is not None and self._thread_key(other) == current_thread:
                other_meta = other.meta if isinstance(other.meta, dict) else {}
                if current_sender and _norm(other_meta.get("sender") or "") == current_sender:
                    return True
        return False

    # -- writes -------------------------------------------------------------

    def record_already_known(self, matched_id: uuid.UUID, source_id: uuid.UUID) -> None:
        """Ops event for an ALREADY KNOWN verdict (the quote link is done by
        the caller — it owns the resolved quote list)."""
        self._record(
            Verdict.ALREADY_KNOWN,
            {"matched_id": str(matched_id), "item_id": str(source_id)},
        )

    def write(
        self,
        result: ReconResult,
        *,
        source: Any,
        child: Any,
    ) -> None:
        """Persist a NON-NEW verdict: the relation + (rare) auto-apply.

        The caller created the verdict's proposed child already (so the child
        id exists for the link). ALREADY KNOWN never reaches here (it links
        the quote and creates NO child).
        """
        from brain.db.models import ItemRelation  # noqa: PLC0415

        matched_id = uuid.UUID(str(result.matched_id))
        fields_json = {k: list(v) for k, v in result.fields.items()}
        if result.verdict is Verdict.MORE_DETAIL:
            rel = ItemRelation(
                from_id=child.id, to_id=matched_id, kind="details",
                provenance={"how": "recon"},
            )
        elif result.verdict is Verdict.CONFLICT:
            rel = ItemRelation(
                from_id=child.id, to_id=matched_id, kind="conflicts",
                provenance={"how": "recon", "fields": fields_json},
            )
        elif result.verdict is Verdict.UPDATE:
            auto = self._auto_apply_allowed(source, result.matched_id)
            rel = ItemRelation(
                from_id=child.id, to_id=matched_id, kind="supersedes",
                provenance={
                    "how": "recon", "state": "applied" if auto else "proposed",
                    "verdict": "update", "fields": fields_json,
                },
            )
            self._session.add(rel)
            if auto:  # item 20a: same-thread + same-sender, always shown + undoable
                from brain.db.models import MemoryItem  # noqa: PLC0415

                existing_row = self._session.get(MemoryItem, matched_id)
                existing_row.superseded_by = child.id
                self._record(result.verdict, {
                    "child_id": str(child.id), "matched_id": result.matched_id,
                    "auto_applied": True, "fields": fields_json,
                })
                self._session.commit()
                return
        elif result.verdict is Verdict.CANCELLATION:
            rel = ItemRelation(
                from_id=child.id, to_id=matched_id, kind="cancels",
                provenance={"how": "recon", "state": "proposed"},
            )
        else:  # pragma: no cover — NEW/ALREADY_KNOWN never reach write()
            raise ValueError(f"write() does not handle verdict {result.verdict}")
        self._session.add(rel)
        self._session.commit()
        self._record(
            result.verdict,
            {"child_id": str(child.id), "matched_id": result.matched_id,
             "fields": fields_json},
        )

    def apply_cancellation(self, relation_id: uuid.UUID) -> None:
        """✅ on the cancellation card: strike the target (D4), never delete."""
        from datetime import UTC  # noqa: PLC0415

        from brain.db.models import ItemRelation, MemoryItem  # noqa: PLC0415

        rel = self._session.get(ItemRelation, relation_id)
        if rel is None or rel.kind != "cancels":
            raise ValueError(f"not a cancellation relation: {relation_id}")
        state = (rel.provenance or {}).get("state")
        if state == "applied":
            return  # double confirm: no-op
        if state != "proposed":
            raise ValueError("cancellation was undone — propose it again")
        target = self._session.get(MemoryItem, rel.to_id)
        meta = dict(target.extraction_meta or {})
        meta["cancelled"] = {
            "by": str(rel.from_id), "at": datetime.now(tz=UTC).isoformat(),
            "relation_id": str(rel.id),
        }
        target.extraction_meta = meta
        rel.provenance = {**rel.provenance, "state": "applied"}
        self._session.commit()

    def restore_cancellation(self, relation_id: uuid.UUID) -> None:
        """↩︎ restore: the item is current again (D4)."""
        from brain.db.models import ItemRelation, MemoryItem  # noqa: PLC0415

        rel = self._session.get(ItemRelation, relation_id)
        if rel is None or rel.kind != "cancels":
            raise ValueError(f"not a cancellation relation: {relation_id}")
        state = (rel.provenance or {}).get("state")
        if state == "undone":
            return
        if state != "applied":
            raise ValueError("not applied yet — nothing to restore")
        target = self._session.get(MemoryItem, rel.to_id)
        meta = dict(target.extraction_meta or {})
        cancelled = meta.get("cancelled")
        if isinstance(cancelled, dict) and cancelled.get("relation_id") == str(rel.id):
            meta.pop("cancelled")
            target.extraction_meta = meta
        rel.provenance = {**rel.provenance, "state": "undone"}
        self._session.commit()

    def _record(self, verdict: Verdict, detail: dict[str, Any]) -> None:
        from brain import ops  # noqa: PLC0415

        ops.record_event("enrichment", f"recon_{verdict.value}", ok=True, detail=detail)
