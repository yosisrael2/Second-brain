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
same type ∧ same applies_to set ∧ similarity >= RECON_SIMILARITY_THRESHOLD.

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

import logging
import re
import uuid
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime, time, timedelta
from enum import StrEnum
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import or_, select

from brain import ops
from brain.services.relations import similarity_ratio

logger = logging.getLogger(__name__)

# Item 20a/30 signals, code-detected (the LLM only narrows, never creates).
# Substrings, casefolded; Hebrew morphological families share the root.
# E4 (review round 3): stems and patterns, not a word list — Hebrew
# inflections share a root (בוטל/בוטלה/בוטלו, מבוטלת, מתבטל...) and a
# WORD-PREFIX match catches the family; phrases are substrings.
_CANCEL_WORD_STEMS = ("בוטל", "מבוטל", "מתבטל")
_CANCEL_WORDS = ("ביטול",)
_CANCEL_PHRASES = ("לא יתקיים", "לא תתקיים", "called off", "cancel")
_CHANGE_WORD_STEMS = ("שינוי", "שונ", "עודכן", "עדכון", "תיקון", "הוקד", "נדחה")
_CHANGE_PHRASES = ("במקום", "ולא ב", "correction", "update", "change", "reschedul", "postpon")

# PR-b series scope: a stated scope in the candidate's own quotes. Matched
# as normalized substrings — the same conservative style as the change/
# cancel phrases (a hint can add, never a whole-source scan).
_SERIES_FROM_NOW_ON = (
    "לכל המפגשים", "לכל המועדים", "מעתה", "מעכשיו", "מהיום",
    "from now on", "going forward", "every week", "כל השבועות",
)
_SERIES_THIS_TIME = ("רק הפעם", "רק השבוע", "רק לפעם הזאת", "just this time", "this time only")


def stated_series_scope(quotes: Iterable[str]) -> str | None:
    """A stated series scope in the candidate's own quotes (PR-b).

    Returns "from_now_on", "this_time", or None. Scanned ONLY in the
    candidate's grounded quote(s) — the same discipline as signal_for: one
    email mentioning both a one-off change and a policy must not guess.
    """
    from_now = False
    this_time = False
    for text in quotes:
        if not text:
            continue
        lowered = _norm(text)
        if any(p in lowered for p in _SERIES_FROM_NOW_ON):
            from_now = True
        if any(p in lowered for p in _SERIES_THIS_TIME):
            this_time = True
    if from_now and not this_time:
        return "from_now_on"
    if this_time:
        return "this_time"  # an explicit this-time wins when both appear
    return None

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


# Round 5: matching runs on EMBEDDING cosine (the local embedder); the
# lexical ratio is only a fast accept for near-identical strings.
RECON_SIMILARITY_THRESHOLD = 0.80
RECON_LEXICAL_FAST_ACCEPT = 0.95


def _cosine(a: list[float], b: list[float]) -> float:
    """Cosine of two (already L2-normalized or not) vectors; 0 on zero vecs."""
    dot = sum(x * y for x, y in zip(a, b, strict=False))
    na = sum(x * x for x in a) ** 0.5
    nb = sum(x * x for x in b) ** 0.5
    if not na or not nb:
        return 0.0
    return dot / (na * nb)


def recon_similarity_from_settings(
    settings: Any, embedder: Any | None = None
) -> Callable[[str, str], float]:
    """THE one builder for the recon match callable — production and
    recon-eval both construct their matcher HERE and nowhere else
    (RECON_EMBED_MODEL embedder -> embedder_similarity with the 0.95
    lexical fast-accept and the 0.80 threshold). Tests inject `embedder`."""
    if embedder is None:
        from brain.ai.embedder import FastEmbedEmbedder  # noqa: PLC0415

        embedder = FastEmbedEmbedder(settings.recon_embed_model)
    return embedder_similarity(embedder)


def embedder_similarity(
    embedder: Any, *, cache: dict[str, list[float]] | None = None
) -> Callable[[str, str], float]:
    """E1 (round 5): THE match callable — embedding cosine against the local
    embedder (the same fastembed model search uses; offline, no API), with
    the lexical ratio as a fast accept when it already clears the threshold.
    Production (ReconciliationService) and recon-eval build their callable
    from THIS factory so the two can never drift. Vectors are cached per
    normalized title within the callable (one embed per distinct title).
    """
    store: dict[str, list[float]] = cache if cache is not None else {}

    def similarity(a: str, b: str) -> float:
        na, nb = _norm(a), _norm(b)
        if not na or not nb:
            return 0.0
        lexical = similarity_ratio(na, nb)
        if lexical >= RECON_LEXICAL_FAST_ACCEPT:
            return lexical  # near-identical strings: no embedding call
        for key, text in ((na, a), (nb, b)):
            if key not in store:
                store[key] = embedder.embed(text)
        va, vb = store[na], store[nb]
        return _cosine(va, vb)

    return similarity


def _meta_instant(value: Any) -> datetime | None:
    """Parse an iso meta instant (paste 'end', extraction_meta 'end')."""
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value))
    except ValueError:
        return None


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
    # PR-b: True when the match needed the RELAXED type gate (the cancel
    # signal alone matched a different kind); write() stores it in the
    # relation's provenance.
    cross_kind: bool = False


def _sender_address(meta: dict[str, Any]) -> str:
    """The sender's bare email address from a source's meta (bug 4, review
    round 2): email sources write it under 'from' ("Rinat <rinat@x>" or the
    bare address), WhatsApp under 'sender'. Empty when neither holds one."""
    for key in ("from", "sender"):
        value = str(meta.get(key) or "")
        match = re.search(r"[\w.+-]+@[\w-]+\.[\w.+-]+", value)
        if match:
            return _norm(match.group(0))
    return ""


def _norm(value: str | None) -> str:
    return " ".join(str(value or "").split()).casefold()


def signal_for(
    quotes: Iterable[str], *, hint_change: bool = False, hint_cancel: bool = False
) -> ReconSignal:
    """THE signal helper — production and recon-eval both call this.

    The change/cancel phrases are scanned in the CANDIDATE'S OWN texts only
    (its grounded verbatim quote(s) in production; the eval case's
    new_text_quote in recon-eval), ORed with the extractor's hints. Never
    the whole source: one email that cancels activity X must not hand a
    cancel signal to the five other activities it mentions (review round 1,
    blocker 1). A hint can add a signal, never remove one; and a hint alone
    can never create a match — classify() still demands type + applies_to +
    similarity.
    """
    change = hint_change
    cancel = hint_cancel
    for text in quotes:
        if not text:
            continue
        lowered = _norm(text)
        words = re.findall(r"[\w]+", lowered)
        change = change or any(
            w.startswith(stem) for w in words for stem in _CHANGE_WORD_STEMS
        ) or any(p in lowered for p in _CHANGE_PHRASES)
        cancel = cancel or any(
            w.startswith(stem) for w in words for stem in _CANCEL_WORD_STEMS
        ) or any(w in _CANCEL_WORDS for w in words) or any(
            p in lowered for p in _CANCEL_PHRASES
        )
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

    # E2 (round 5): the +-1h DATE_TOLERANCE is the IDENTITY rule of the
    # windows only. Field diffs are EXACT: any stated clock that differs at
    # all (15 minutes included) is a differing value.
    diffs: dict[str, tuple[str, str]] = {}
    if (
        candidate.start is not None
        and existing.start is not None
        and _clock_stated(candidate.start, tz)
        and _clock_stated(existing.start, tz)
        and candidate.start != existing.start
    ):
        diffs["time"] = (fmt(existing.start), fmt(candidate.start))
    if (
        candidate.end is not None
        and existing.end is not None
        and candidate.end != existing.end
    ):
        diffs["end"] = (fmt(existing.end), fmt(candidate.end))
    if (
        candidate.location is not None
        and existing.location is not None
        and _norm(candidate.location) != _norm(existing.location)
    ):
        diffs["location"] = (existing.location, candidate.location)
    return diffs


def _adds_info(candidate: ReconCandidate, existing: ExistingItem, tz: ZoneInfo) -> bool:
    """E3 (round 3): MORE DETAIL = no differing value AND the candidate
    states a field the existing item LACKS — a location, an end time, or a
    clock time on a date-only item. Title length is NEVER evidence (a plain
    re-mention is longer and states nothing new)."""
    if candidate.location and not existing.location:
        return True
    if candidate.end is not None and existing.end is None:
        return True
    if candidate.start is not None and _clock_stated(candidate.start, tz):
        if existing.start is None or not _clock_stated(existing.start, tz):
            # a stated clock time on a date-only (or dateless) item
            return True
    return False


def _compatible(
    candidate: ReconCandidate,
    existing: ExistingItem,
    similarity: Callable[[str, str], float],
    *,
    relax_type: bool = False,
) -> bool:
    """The always-on guards: same type ∧ same applies_to ∧ similar enough.

    R5 (items 21/42): two different KNOWN audiences never match; unknown ==
    unknown is allowed (memory may have improved since). Whether a dated and
    a dateless pair can meet is the WINDOWS' business, not a guard here.

    ``relax_type`` drops ONLY the type gate (PR-b cross-kind cancellation);
    the applies_to gate and the similarity threshold never relax.
    """
    if existing.type != candidate.kind and not relax_type:
        return False
    candidate_applies = tuple(sorted(candidate.applies_to))
    existing_applies = tuple(sorted(existing.applies_to))
    if candidate_applies != existing_applies:
        return False
    return similarity(candidate.title, existing.title) >= RECON_SIMILARITY_THRESHOLD


def classify(
    candidate: ReconCandidate,
    existing_items: list[ExistingItem],
    *,
    similarity: Callable[[str, str], float] = similarity_ratio,
    now: datetime,
    tz_name: str,
    signal: ReconSignal | None = None,
    relax_type: bool = False,
) -> ReconResult:
    """One verdict for the candidate against the existing items (pure).

    Windows: same local day always; extended [today, today+30d] only with a
    signal. Verdict order per the module docstring. Several matches need a
    similarity margin >= AMBIGUITY_MARGIN or the result is NEW + ambiguous
    (the caller records the ops event; the ids travel with it).

    ``relax_type`` (PR-b): cross-kind rows join the pool when the signal
    cancels — ONLY a CANCELLATION can come out of a cross-kind match; a
    cross-kind match with any other verdict is dropped (NEW). Callers pass
    relax_type only for the cancel pre-pass.
    """
    tz = ZoneInfo(tz_name)
    signal = signal or ReconSignal()
    relax = relax_type and signal.cancel
    compatible = [
        e for e in existing_items if _compatible(candidate, e, similarity, relax_type=relax)
    ]
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
    if best.type != candidate.kind:
        # a cross-kind match (only reachable with relax_type + cancel): a
        # reminder saying an event is cancelled. Only CANCELLATION is a
        # verdict here — a change phrased cross-kind is ambiguous speech,
        # not evidence (PR-b decision).
        if not (relax and signal.cancel):
            return ReconResult(verdict=Verdict.NEW)
        return ReconResult(
            verdict=Verdict.CANCELLATION, matched_id=best.id, cross_kind=True
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
    if best in same_day and _adds_info(candidate, best, tz):
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

    def __init__(
        self, session: Any, repo: Any, *, tz_name: str,
        similarity: Callable[[str, str], float] | None = None,
        similarity_factory: Callable[[], Any] | None = None,
    ) -> None:
        # B1 (final check): None means "not built yet" — the factory (the
        # ONE shared builder, via EnrichmentService) runs lazily on the
        # first reconciled candidate. A bare service with neither similarity
        # nor factory falls back to lexical (unit tests only).
        self._similarity = similarity
        self._similarity_factory = similarity_factory
        self._embedder_failed = False
        self._session = session
        self._repo = repo
        self._tz_name = tz_name

    # -- universe ---------------------------------------------------------

    def _window_statement(self, kind: str, exclude_ids: set[uuid.UUID],
                          candidate: ReconCandidate, signal: ReconSignal,
                          now: datetime, *, relax_type: bool = False) -> Any:
        """PERF 5 (round 2): the matchable universe is fetched with the
        windows IN SQL — the candidate's local day (no signal needed) OR,
        only when the candidate carries a signal, the extended
        [today, today+30d] span — plus the same type/exclusion/hidden/
        non-dropped filters. Occurred_at IS NULL rows (dateless items) join
        the same-day leg only for a dateless candidate (dateless matches
        dateless, item 21). Bounded query, never all history.

        ``relax_type`` (PR-b): drop ONLY the SQL type filter — the cancel
        pre-pass fetches other kinds too; every other guard stays.
        """
        from brain.db.models import MemoryItem  # noqa: PLC0415
        from brain.repositories.items import default_hidden_conditions  # noqa: PLC0415

        tz = ZoneInfo(self._tz_name)
        today = now.astimezone(tz).date()
        stmt = select(MemoryItem).filter(
            MemoryItem.id.not_in(exclude_ids or {uuid.uuid4()})
        )
        if not relax_type:
            stmt = stmt.filter(MemoryItem.type == kind)
        for cond in default_hidden_conditions():
            stmt = stmt.where(cond)  # superseded + confirmed-cancelled
        stmt = stmt.where(
            MemoryItem.child_state.is_(None) | (MemoryItem.child_state != "dropped")
        )
        today_start = datetime.combine(today, time(0, 0), tzinfo=tz).astimezone(UTC)
        today_end = datetime.combine(
            today + timedelta(days=EXTENDED_WINDOW_DAYS + 1), time(0, 0), tzinfo=tz
        ).astimezone(UTC)
        extended = MemoryItem.occurred_at >= today_start
        if signal.any:
            extended = extended & (MemoryItem.occurred_at < today_end)
        if candidate.start is None:
            day_leg = MemoryItem.occurred_at.is_(None)  # dateless matches dateless
        else:
            day = candidate.start.astimezone(tz).date()
            day_start = datetime.combine(day, time(0, 0), tzinfo=tz).astimezone(UTC)
            day_end = datetime.combine(
                day + timedelta(days=1), time(0, 0), tzinfo=tz
            ).astimezone(UTC)
            day_leg = (MemoryItem.occurred_at >= day_start) & (
                MemoryItem.occurred_at < day_end
            )
        if signal.any:
            stmt = stmt.where(or_(day_leg, extended))
        else:
            stmt = stmt.where(day_leg)
        return stmt

    def matchable_items(
        self,
        kind: str,
        exclude_ids: set[uuid.UUID],
        *,
        candidate: ReconCandidate,
        now: datetime,
        signal: ReconSignal | None = None,
        relax_type: bool = False,
    ) -> list[tuple[ExistingItem, Any]]:
        """Existing items a candidate may reconcile against.

        Same type (unless ``relax_type`` — the PR-b cancel pre-pass); NOT
        dropped (item 30: only the same-source diff suppresses); NOT
        superseded (the current version matches); NOT cancelled; excludes
        the source itself and its own children (the A1 diff owns those); the
        two windows filter IN SQL (PERF 5, round 2).
        """

        rows = (
            self._session.execute(
                self._window_statement(
                    kind, exclude_ids, candidate, signal or ReconSignal(), now,
                    relax_type=relax_type,
                )
            )
            .scalars()
            .all()
        )
        out: list[tuple[ExistingItem, Any]] = []
        for row in rows:
            if row.child_state == "dropped":
                continue
            meta = row.extraction_meta if isinstance(row.extraction_meta, dict) else {}
            if meta.get("cancelled"):
                continue
            row_meta = row.meta if isinstance(row.meta, dict) else {}
            out.append(
                (
                    ExistingItem(
                        id=str(row.id),
                        type=getattr(row.type, "value", row.type) if row.type else kind,
                        title=row.raw_text,
                        start=row.occurred_at,
                        # NULL applies_to = the audience was never resolved
                        # (real pasted/user items) — equivalent to "unknown";
                        # R5 only forbids two DIFFERENT KNOWN audiences.
                        applies_to=tuple(row.applies_to) if row.applies_to else ("unknown",),
                        # F2 (round 2): end/location from where real items
                        # store them — children in extraction_meta, pasted
                        # calendar items in meta (verified paste keys).
                        end=_meta_instant(meta.get("end") or row_meta.get("end")),
                        location=meta.get("location") or row_meta.get("location"),
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
        """classify() against the DB universe (the pure function, unchanged)
        plus the F3 gate (round 4): a still-PROPOSED child of ANOTHER source
        is matchable only for ALREADY KNOWN / MORE DETAIL — for UPDATE /
        CONFLICT / CANCELLATION the verdict is NEW + an ops
        recon_target_unapproved event (never update what the user has not
        approved yet; decision 6)."""
        # _ensure_embedder() guarantees a callable (factory or lexical
        # fallback); the `or` only settles the Optional for the type checker.
        similarity = self._similarity or similarity_ratio
        matches = self.matchable_items(
            candidate.kind, exclude_ids, candidate=candidate, now=now, signal=signal
        )
        result = classify(
            candidate,
            [pair[0] for pair in matches],
            similarity=similarity,
            now=now,
            tz_name=self._tz_name,
            signal=signal or ReconSignal(),
        )
        result = self._gate_unapproved(result, matches, candidate)
        if (
            result.verdict is Verdict.NEW
            and not result.ambiguous
            and signal is not None
            and signal.cancel
        ):
            # PR-b cross-kind pre-pass: ONLY the cancel signal may relax the
            # type gate, and only when the typed pass found nothing. The
            # relaxed verdict can only be a CANCELLATION (classify enforces);
            # the unapproved-target gate applies to it just the same.
            relaxed_matches = self.matchable_items(
                candidate.kind,
                exclude_ids,
                candidate=candidate,
                now=now,
                signal=signal,
                relax_type=True,
            )
            relaxed = classify(
                candidate,
                [pair[0] for pair in relaxed_matches],
                similarity=similarity,
                now=now,
                tz_name=self._tz_name,
                signal=signal,
                relax_type=True,
            )
            relaxed = self._gate_unapproved(relaxed, relaxed_matches, candidate)
            if relaxed.verdict is Verdict.CANCELLATION:
                result = relaxed
        return result

    def _gate_unapproved(
        self,
        result: ReconResult,
        matches: list[tuple[ExistingItem, Any]],
        candidate: ReconCandidate,
    ) -> ReconResult:
        """The F3 gate (round 4): a still-PROPOSED child of ANOTHER source is
        matchable only for ALREADY KNOWN / MORE DETAIL — for UPDATE /
        CONFLICT / CANCELLATION the verdict is NEW + an ops
        recon_target_unapproved event (never update what the user has not
        approved yet; decision 6)."""
        if result.matched_id is not None and result.verdict in (
            Verdict.UPDATE, Verdict.CONFLICT, Verdict.CANCELLATION,
        ):
            matched_row = next(
                (row for pair_id, row in
                 ((uuid.UUID(pair[0].id), pair[1]) for pair in matches)
                 if pair_id == uuid.UUID(str(result.matched_id))),
                None,
            )
            if matched_row is not None and matched_row.child_state == "proposed":
                self._record_target_unapproved(candidate, result)
                return ReconResult(verdict=Verdict.NEW)
        return result

    def attach_embedder(self, embedder: Any) -> None:
        """E1: switch matching to embedding cosine (production + eval use the
        same callable built by embedder_similarity)."""
        self._similarity = embedder_similarity(embedder)

    def _ensure_embedder(self) -> None:
        """B1: first reconciled candidate builds the match callable through
        the shared factory (health/ops constructions never pay). B2: a load
        failure degrades ONCE to the lexical matcher for the rest of the
        process — ops event + one log line, no exception escapes, no retry
        until the next process start."""
        if self._similarity is None and self._similarity_factory is not None:
            try:
                self._similarity = self._similarity_factory()
            except Exception as exc:  # noqa: BLE001 — degrade, never kill the pass
                self._similarity = similarity_ratio
                self._embedder_failed = True
                ops.record_event(
                    "enrichment", "recon_embedder_unavailable", ok=False,
                    detail={"error": type(exc).__name__, "reason": str(exc)[:120]},
                )
                logger.warning(
                    "recon embedder unavailable; matching falls back to "
                    "lexical for this process (%s)", exc,
                )

    def _record_target_unapproved(self, candidate: ReconCandidate,
                                  result: ReconResult) -> None:
        """Ops evidence for the F3 gate (auditable, never silent)."""
        self._record(Verdict.NEW, {
            "candidate": candidate.title[:60],
            "matched_id": str(result.matched_id),
            "would_have_been": result.verdict.value,
        })
        ops.record_event(
            "enrichment",
            "recon_target_unapproved",
            ok=True,
            detail={
                "candidate": candidate.title[:60],
                "matched_id": str(result.matched_id),
                "would_have_been": result.verdict.value,
            },
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
        """20a + decision 6: same thread AND same sender AND the existing item
        is not user-acted. A kept/dropped item is immutable — proposals may
        TARGET it, but nothing on it changes until the user confirms."""
        from brain.db.models import MemoryItem  # noqa: PLC0415

        matched_row = self._session.get(MemoryItem, uuid.UUID(existing_item_id))
        if matched_row is not None and matched_row.child_state in ("kept", "dropped"):
            return False
        current_thread = self._thread_key(source)
        current_meta = source.meta if isinstance(source.meta, dict) else {}
        current_sender = _sender_address(current_meta)
        for other in self._sources_of(uuid.UUID(existing_item_id)):
            if other.id == source.id:
                return True  # the very same source item
            if current_thread is not None and self._thread_key(other) == current_thread:
                other_meta = other.meta if isinstance(other.meta, dict) else {}
                if current_sender and _sender_address(other_meta) == current_sender:
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
        quotes: Iterable[str] | None = None,
    ) -> None:
        """Persist a NON-NEW verdict: the relation + (rare) auto-apply.

        The caller created the verdict's proposed child already (so the child
        id exists for the link). ALREADY KNOWN never reaches here (it links
        the quote and creates NO child).

        ``quotes`` (PR-b): the candidate's grounded quote(s) — the series
        scope scans them for a stated "from now on" / "this time only".
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
            detail: dict[str, Any] = {
                "child_id": str(child.id), "matched_id": result.matched_id,
                "fields": fields_json,
            }
            if auto:
                detail["auto_applied"] = True
            # PR-b series scope: question / stated answer / pre-filled future
            # member. The spread relations (stated or pre-confirmed 'from now
            # on') are PROPOSED — one tap confirms them, never silent.
            series_rels, series_meta = self._note_series_scope(
                child, source, matched_id, quotes, fields_json
            )
            if series_meta is not None:
                meta = dict(child.extraction_meta or {})
                meta["recon_series_scope"] = series_meta
                child.extraction_meta = meta
            self._session.add_all(series_rels)
            self._session.commit()
            self._record(result.verdict, detail)
            return
        elif result.verdict is Verdict.CANCELLATION:
            provenance: dict[str, Any] = {"how": "recon", "state": "proposed"}
            if result.cross_kind:
                provenance["cross_kind"] = True
            rel = ItemRelation(
                from_id=child.id, to_id=matched_id, kind="cancels",
                provenance=provenance,
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

    # -- series scope (PR-b) -------------------------------------------------

    def answer_series_scope(
        self, relation_id: uuid.UUID, scope: str
    ) -> list[Any]:
        """The card's answer to the series scope question (PR-b).

        ``scope`` is what the user tapped: ``"this_time"`` or
        ``"from_now_on"``. 'from now on' stores the decision on the child
        and PROPOSES the same change for every already-captured LATER
        member of the series (the caller's 'apply to all N' tap confirms
        them; nothing here is ever applied silently). 'this time only'
        stores the decision and spreads nothing.

        Returns the newly created relations (empty for 'this time').
        """
        from brain.db.models import ItemRelation, MemoryItem  # noqa: PLC0415

        if scope not in ("this_time", "from_now_on"):
            raise ValueError(f"unknown series scope answer: {scope}")
        rel = self._session.get(ItemRelation, relation_id)
        if rel is None or rel.kind != "supersedes":
            raise ValueError(f"not an update relation: {relation_id}")
        child = self._session.get(MemoryItem, rel.from_id)
        meta = dict(child.extraction_meta or {})
        smeta = dict(meta.get("recon_series_scope") or {})
        series_id = smeta.get("series_id")
        if not series_id:
            raise ValueError("the update has no series scope to answer")
        smeta["decided_at"] = datetime.now(tz=UTC).isoformat()
        created: list[Any] = []
        if scope == "from_now_on":
            smeta["scope"] = "series"
            fields = smeta.get("fields") or (rel.provenance or {}).get("fields") or {}
            created = self._series_spread(
                child, series_id, uuid.UUID(str(rel.to_id)), fields
            )
            self._session.add_all(created)
        else:
            smeta["scope"] = "this_time"
        meta["recon_series_scope"] = smeta
        child.extraction_meta = meta
        self._session.commit()
        return created

    def _note_series_scope(
        self,
        child: Any,
        source: Any,
        matched_id: uuid.UUID,
        quotes: Iterable[str] | None,
        fields_json: dict[str, list[str]],
    ) -> tuple[list[Any], dict[str, Any] | None]:
        """Series scope bookkeeping for one UPDATE (PR-b, decision 8).

        Only an update on a SERIES member (the source's meta series_uid
        equals the matched item's) stores anything:

        - a stated 'from now on' in the candidate's own quotes -> the
          decision is stored AND the later members get their PROPOSED
          relations right away;
        - a stated 'this time only' -> the decision is stored, no spread;
        - no stated scope -> the QUESTION is stored (the card asks; a prior
          series decision on the same series PRE-FILLS this member: one
          tap, never silent).

        Returns (spread relations, meta to store on the child) — ([], None)
        when this is not a series update.
        """
        from brain.db.models import MemoryItem  # noqa: PLC0415

        source_meta = source.meta if isinstance(source.meta, dict) else {}
        series_id = source_meta.get("series_uid")
        if not series_id:
            return [], None
        matched_row = self._session.get(MemoryItem, matched_id)
        if matched_row is None:
            return [], None
        matched_meta = matched_row.meta if isinstance(matched_row.meta, dict) else {}
        if matched_meta.get("series_uid") != series_id:
            return [], None
        decided_at = datetime.now(tz=UTC).isoformat()
        stated = stated_series_scope(quotes or [])
        if stated == "from_now_on":
            spread = self._series_spread(child, series_id, matched_id, fields_json)
            meta = {
                "series_id": series_id, "scope": "series",
                "fields": fields_json, "decided_at": decided_at,
            }
            return spread, meta
        if stated == "this_time":
            return [], {
                "series_id": series_id, "scope": "this_time",
                "fields": fields_json, "decided_at": decided_at,
            }
        prior = self._prior_series_decision(series_id, exclude_child_id=child.id)
        if prior is not None and prior.get("scope") == "series":
            # a stored 'from now on' decision PRE-FILLS this future member:
            # its card carries the decision (one tap confirms), never silent.
            spread = self._series_spread(child, series_id, matched_id, fields_json)
            meta = {
                "series_id": series_id, "scope": "series",
                "fields": fields_json, "decided_at": prior.get("decided_at"),
                "pre_confirmed": True,
            }
            return spread, meta
        return [], {
            "series_id": series_id, "scope": "question",
            "fields": fields_json, "decided_at": None,
        }

    def _prior_series_decision(
        self, series_id: str, *, exclude_child_id: Any
    ) -> dict[str, Any] | None:
        """The most recent stored series decision for this series (PR-b).

        Only PROPOSED/AUTO children carry scope decisions; a dropped child's
        decision is not evidence (the user rejected that proposal).
        """
        from brain.db.models import MemoryItem  # noqa: PLC0415

        rows = (
            self._session.query(MemoryItem)
            .filter(
                MemoryItem.extraction_meta["recon_series_scope"]["series_id"]
                .as_string() == series_id
            )
            .filter(MemoryItem.id != exclude_child_id)
            .filter(MemoryItem.child_state != "dropped")
            .order_by(MemoryItem.created_at.desc())
            .limit(5)
            .all()
        )
        for row in rows:
            scope_meta = (row.extraction_meta or {}).get("recon_series_scope")
            if isinstance(scope_meta, dict) and scope_meta.get("decided_at"):
                return scope_meta
        return None

    def _series_spread(
        self,
        child: Any,
        series_id: str,
        matched_id: uuid.UUID,
        fields_json: dict[str, list[str]],
    ) -> list[Any]:
        """PROPOSED supersedes relations from ``child`` to every
        already-captured LATER member of the series (PR-b).

        Later = occurred_at at/after the matched instance's. Members already
        superseded, cancelled, dropped, or already proposed BY THIS CHILD
        are skipped; the query is bounded. Idempotent per child.
        """
        from brain.db.models import ItemRelation, MemoryItem  # noqa: PLC0415
        from brain.repositories.items import default_hidden_conditions  # noqa: PLC0415

        matched_row = self._session.get(MemoryItem, matched_id)
        if matched_row is None or matched_row.occurred_at is None:
            return []
        existing_targets = {
            str(rel.to_id)
            for rel in self._session.query(ItemRelation)
            .filter(ItemRelation.from_id == child.id)
            .filter(ItemRelation.kind == "supersedes")
            .all()
        }
        stmt = self._session.query(MemoryItem).filter(
            MemoryItem.meta["series_uid"].as_string() == series_id
        )
        for cond in default_hidden_conditions():
            stmt = stmt.filter(cond)
        rows = (
            stmt.filter(MemoryItem.id != matched_id)
            .filter(MemoryItem.id != child.id)
            .filter(MemoryItem.occurred_at >= matched_row.occurred_at)
            .order_by(MemoryItem.occurred_at)
            .limit(50)
            .all()
        )
        out: list[Any] = []
        for row in rows:
            if str(row.id) in existing_targets:
                continue
            out.append(
                ItemRelation(
                    from_id=child.id,
                    to_id=row.id,
                    kind="supersedes",
                    provenance={
                        "how": "recon", "state": "proposed",
                        "verdict": "update", "fields": fields_json,
                        "series_id": series_id,
                    },
                )
            )
        return out

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
