"""Reconciliation (PR D-recon-1, spec item 37): classify an extracted child
against existing items before proposals — NEW / ALREADY KNOWN / UPDATE /
MORE DETAIL / CONFLICT / CANCELLATION; nothing applied silently.

Rule source: thread recon/plan (manager decisions + veto, 24 Oct 2026):
- Two WINDOWS: SAME LOCAL DAY (no signal required; covers the item-21 ±1h
  identity AND same-day time changes) and EXTENDED [today, today+30d] for
  dateless or moved-date candidates (SIGNAL required).
- Verdict order (first wins): NEW; cancellation-signal match -> CANCELLATION;
  differing field value + change signal -> UPDATE; differing value, no signal
  -> CONFLICT (same-day window only); +/-1h, nothing new, adds info ->
  MORE DETAIL; +/-1h, nothing new -> ALREADY KNOWN.
- Hints never create a match; multi-match needs a 0.05 similarity margin;
  dropped children are outside the universe; cross-source look-alike of a
  dropped child is NEW.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from brain.db.models import ItemRelation, ItemSource, MemoryItem, OpsEvent
from brain.db.session import SessionLocal, engine
from brain.repositories.items import ItemsRepository
from brain.services.extraction.candidates import ChildCandidate, ReconHint
from brain.services.extraction.reconciliation import (
    ExistingItem,
    ReconCandidate,
    ReconciliationService,
    ReconSignal,
    Verdict,
    classify,
    signal_for,
)
from brain.services.extraction.service import EnrichmentService
from brain.services.relations import RelationsService, similarity_ratio
from conftest import assert_test_database

JLM = ZoneInfo("Asia/Jerusalem")
NOW = datetime(2026, 11, 2, 12, 0, tzinfo=UTC)  # Mon 2.11.2026, after DST end

# Long base title: difflib punishes suffixes on short Hebrew strings; a long
# title keeps appended detail within the 0.85 threshold (like real summaries).
BASE = "טיול שנתי של כיתה ה לפארק השרון ביום שלישי 10.11"

# --- helpers ------------------------------------------------------------------


def _jlm(day: int, hour: int, minute: int = 0) -> datetime:
    """A wall-clock instant on 2026-11 (post-DST, +02:00) as aware UTC."""
    return datetime(2026, 11, day, hour, minute, tzinfo=JLM)


def _existing(
    id: str = "e1",
    type: str = "event",
    title: str = BASE,
    start: datetime | None = None,
    applies_to: tuple[str, ...] = ("Rona",),
    location: str | None = None,
    end: datetime | None = None,
) -> ExistingItem:
    return ExistingItem(
        id=id, type=type, title=title, start=start, applies_to=applies_to,
        location=location, end=end,
    )


def _candidate(
    kind: str = "event",
    title: str = BASE,
    start: datetime | None = None,
    applies_to: tuple[str, ...] = ("Rona",),
    location: str | None = None,
    end: datetime | None = None,
) -> ReconCandidate:
    return ReconCandidate(
        kind=kind, title=title, start=start, end=end,
        applies_to=applies_to, location=location,
    )


def _similarity(a: str, b: str) -> float:
    return similarity_ratio(a, b)


# --- the veto's reachability spine (BUG B) -------------------------------------


def test_no_match_is_new():
    result = classify(
        _candidate(start=_jlm(10, 9)),
        [_existing(start=_jlm(20, 16))],
        similarity=_similarity, now=NOW, tz_name="Asia/Jerusalem",
    )
    assert result.verdict is Verdict.NEW
    assert result.matched_id is None


def test_same_day_time_change_no_signal_is_conflict():
    """The VETO case: 16:00 vs 18:00 the same day, NO signal -> CONFLICT."""
    result = classify(
        _candidate(title=BASE, start=_jlm(10, 18, 0)),
        [_existing(start=_jlm(10, 16, 0))],
        similarity=_similarity, now=NOW, tz_name="Asia/Jerusalem",
    )
    assert result.verdict is Verdict.CONFLICT
    assert result.matched_id == "e1"
    assert "time" in result.fields


def test_same_day_time_change_with_signal_is_update():
    """The VETO case: same move WITH a change signal -> UPDATE."""
    result = classify(
        _candidate(title=BASE, start=_jlm(10, 18, 0)),
        [_existing(start=_jlm(10, 16, 0))],
        similarity=_similarity, now=NOW, tz_name="Asia/Jerusalem",
        signal=ReconSignal(change=True),
    )
    assert result.verdict is Verdict.UPDATE
    assert result.fields["time"] == ("16:00", "18:00")


def test_same_time_location_differs_no_signal_is_conflict():
    """The VETO case: time equal, location differs, no signal -> CONFLICT."""
    result = classify(
        _candidate(title=BASE, start=_jlm(10, 16, 0), location="אשקלות"),
        [_existing(title=BASE, start=_jlm(10, 16, 0), location="בית הספר")],
        similarity=_similarity, now=NOW, tz_name="Asia/Jerusalem",
    )
    assert result.verdict is Verdict.CONFLICT
    assert result.fields["location"][0] != result.fields["location"][1]


def test_plus_minus_1h_nothing_new_is_already_known():
    # E2 (round 5): identity tolerance lives in the windows only; an exactly
    # stated different clock is a diff (the E2 cases above). "Nothing new"
    # now means the SAME stated instant:
    result = classify(
        _candidate(title=BASE, start=_jlm(10, 16, 0)),
        [_existing(start=_jlm(10, 16, 0))],
        similarity=_similarity, now=NOW, tz_name="Asia/Jerusalem",
    )
    assert result.verdict is Verdict.ALREADY_KNOWN
    assert result.matched_id == "e1"


def test_plus_minus_1h_adds_info_is_more_detail():
    """E3 (round 3): MORE DETAIL = the candidate states a field the existing
    item LACKS (here: a location). Title length alone is never evidence —
    a plain longer re-mention is ALREADY KNOWN."""
    assert _similarity(BASE + " להביא כובע", BASE) >= 0.85  # the key still matches
    result = classify(
        _candidate(start=_jlm(10, 16, 0), location="פארק השרון"),
        [_existing(start=_jlm(10, 16, 0))],
        similarity=_similarity, now=NOW, tz_name="Asia/Jerusalem",
    )
    assert result.verdict is Verdict.MORE_DETAIL
    longer_only = classify(
        _candidate(title=BASE + " להביא כובע", start=_jlm(10, 16, 0)),
        [_existing(start=_jlm(10, 16, 0))],
        similarity=_similarity, now=NOW, tz_name="Asia/Jerusalem",
    )
    assert longer_only.verdict is Verdict.ALREADY_KNOWN  # length is not info


def test_cancellation_signal_match_is_cancellation_even_dateless():
    """A dateless 'הטיול בוטל' matches the dated trip in the extended window."""
    result = classify(
        _candidate(title="בוטל: " + BASE, start=None),
        [_existing(title=BASE, start=_jlm(10, 16, 0))],
        similarity=_similarity, now=NOW, tz_name="Asia/Jerusalem",
        signal=ReconSignal(cancel=True),
    )
    assert result.verdict is Verdict.CANCELLATION
    assert result.matched_id == "e1"


def test_each_of_the_six_verdicts_is_reachable():
    """One case per verdict, incl. the manager's three named ones."""
    day = _existing(start=_jlm(10, 16, 0))
    cases = [
        # NEW: nothing similar
        (_candidate(title="אספת הורים חדשה", start=_jlm(20, 9, 0)), ReconSignal(), Verdict.NEW),
        # CANCELLATION: cancellation signal + same-day match
        (
            _candidate(title="בוטל: " + BASE, start=_jlm(10, 16, 0)),
            ReconSignal(cancel=True), Verdict.CANCELLATION,
        ),
        # UPDATE: same day 16:00 vs 18:00 WITH שינוי
        (
            _candidate(title=BASE, start=_jlm(10, 18, 0)),
            ReconSignal(change=True), Verdict.UPDATE,
        ),
        # CONFLICT: same day 16:00 vs 18:00 no signal
        (_candidate(start=_jlm(10, 18, 0)), ReconSignal(), Verdict.CONFLICT),
        # MORE DETAIL: same instant, states a field the item lacks (E3)
        (
            _candidate(start=_jlm(10, 16, 0), location="פארק השרון"),
            ReconSignal(), Verdict.MORE_DETAIL,
        ),
        # ALREADY KNOWN: the same stated instant, nothing new (E2: diffs are
        # EXACT, so +-1h is identity only in the windows — 16:30 vs 16:00 is
        # now a differing value -> CONFLICT without a signal)
        (_candidate(start=_jlm(10, 16, 0)), ReconSignal(), Verdict.ALREADY_KNOWN),
        # E2: 16:30 vs 16:00 same day, no signal -> exact diff -> CONFLICT
        (_candidate(start=_jlm(10, 16, 30)), ReconSignal(), Verdict.CONFLICT),
        # E2: 16:30 vs 16:00 same day, with a change signal -> UPDATE
        (
            _candidate(title=BASE, start=_jlm(10, 16, 30)),
            ReconSignal(change=True), Verdict.UPDATE,
        ),
    ]
    for candidate, signal, expected in cases:
        result = classify(
            candidate, [day], similarity=_similarity, now=NOW, tz_name="Asia/Jerusalem",
            signal=signal,
        )
        assert result.verdict is expected, (candidate.title, signal, result.verdict)


# --- windows -------------------------------------------------------------------


def test_extended_window_requires_signal():
    """Dateless candidate, no signal: even a same-title item next week stays NEW."""
    result = classify(
        _candidate(kind="reminder", title="הטיול שנתי", start=None),
        [_existing(title="טיול שנתי", start=_jlm(10, 16, 0))],
        similarity=_similarity, now=NOW, tz_name="Asia/Jerusalem",
    )
    assert result.verdict is Verdict.NEW


def test_extended_window_with_signal_matches_within_30_days():
    result = classify(
        _candidate(title="בוטל: " + BASE, start=None),
        [_existing(title=BASE, start=_jlm(10, 16, 0))],
        similarity=_similarity, now=NOW, tz_name="Asia/Jerusalem",
        signal=ReconSignal(cancel=True),
    )
    assert result.verdict is Verdict.CANCELLATION


def test_extended_window_bounds_30_days():
    """A moved-date candidate matches only items within [today, today+30d]."""
    far = _existing(title=BASE, start=_jlm(15, 16, 0))  # 15.11 = +13d: inside
    beyond = _existing(
        id="e2", title=BASE, start=datetime(2027, 1, 10, 16, 0, tzinfo=JLM)
    )
    moved = _candidate(title=BASE, start=_jlm(16, 16, 0))
    inside = classify(moved, [far], similarity=_similarity, now=NOW,
                      tz_name="Asia/Jerusalem", signal=ReconSignal(change=True))
    assert inside.verdict is Verdict.UPDATE
    outside = classify(moved, [beyond], similarity=_similarity, now=NOW,
                       tz_name="Asia/Jerusalem", signal=ReconSignal(change=True))
    assert outside.verdict is Verdict.NEW


def test_mixed_dateless_dated_never_matches_without_signal():
    result = classify(
        _candidate(kind="fact", title="טיול שנתי", start=None),
        [_existing(type="fact", title="טיול שנתי", start=_jlm(10, 16, 0))],
        similarity=_similarity, now=NOW, tz_name="Asia/Jerusalem",
    )
    assert result.verdict is Verdict.NEW


def test_dated_never_matches_dateless():
    result = classify(
        _candidate(start=_jlm(10, 16, 0)),
        [_existing(title="טיול שנתי", start=None)],
        similarity=_similarity, now=NOW, tz_name="Asia/Jerusalem",
    )
    assert result.verdict is Verdict.NEW


def test_dateless_pair_matches_on_identity():
    """Two dateless facts of the same thing: item-21 identity without dates;
    a stated location change + signal -> UPDATE (fields carry old -> new)."""
    result = classify(
        _candidate(
            kind="fact", title="האספה באולם ספורט", start=None, location="אולם ב'"
        ),
        [
            _existing(
                type="fact", title="האספה באולם ספורט", start=None, location="אולם ספורט"
            )
        ],
        similarity=_similarity, now=NOW, tz_name="Asia/Jerusalem",
        signal=ReconSignal(change=True),
    )
    assert result.verdict is Verdict.UPDATE
    assert result.fields["location"] == ("אולם ספורט", "אולם ב'")


# --- guards --------------------------------------------------------------------


def test_different_applies_to_never_matched():
    result = classify(
        _candidate(applies_to=("Itamar",), start=_jlm(10, 16, 0)),
        [_existing(applies_to=("Rona",), start=_jlm(10, 16, 0))],
        similarity=_similarity, now=NOW, tz_name="Asia/Jerusalem",
    )
    assert result.verdict is Verdict.NEW


def test_similarity_below_threshold_never_matches():
    result = classify(
        _candidate(title="להביא סרגל", start=_jlm(10, 16, 0)),
        [_existing(title="טיול שנתי", start=_jlm(10, 16, 0))],
        similarity=_similarity, now=NOW, tz_name="Asia/Jerusalem",
    )
    assert result.verdict is Verdict.NEW


def test_type_must_match():
    result = classify(
        _candidate(kind="task", title="טיול שנתי", start=_jlm(10, 16, 0)),
        [_existing(type="event", title="טיול שנתי", start=_jlm(10, 16, 0))],
        similarity=_similarity, now=NOW, tz_name="Asia/Jerusalem",
    )
    assert result.verdict is Verdict.NEW


def test_ambiguous_margin_requires_0_05():
    """Two near-equal matches: never pick silently -> NEW + ambiguous flag."""
    a = _existing(id="e1", title="טיול שנתי כיתה ה", start=_jlm(10, 16, 0))
    b = _existing(id="e2", title="טיול שנתי כיתה ה", start=_jlm(10, 16, 0))
    result = classify(
        _candidate(title="טיול שנתי כיתה ה", start=_jlm(10, 18, 0)),
        [a, b],
        similarity=_similarity, now=NOW, tz_name="Asia/Jerusalem",
        signal=ReconSignal(change=True),
    )
    assert result.verdict is Verdict.NEW
    assert result.ambiguous is True
    assert result.matched_id is None


def test_clear_winner_beats_runner_up():
    """A >= 0.05 margin picks the best match (no ambiguity)."""
    a = _existing(id="e1", title=BASE, start=_jlm(10, 16, 0))
    b = _existing(id="e2", title=BASE + " (בוטל)", start=_jlm(10, 16, 0))
    result = classify(
        _candidate(title=BASE, start=_jlm(10, 18, 0)),
        [a, b],
        similarity=_similarity, now=NOW, tz_name="Asia/Jerusalem",
        signal=ReconSignal(change=True),
    )
    assert _similarity(BASE, BASE + " (בוטל)") >= 0.85  # b is a legal match too
    assert result.verdict is Verdict.UPDATE
    assert result.matched_id == "e1" and not result.ambiguous
    assert result.matched_id == "e1"
    assert result.ambiguous is False


def test_extractor_hints_cannot_create_a_match():
    """Hints select tiers; they can never beat type/applies_to/similarity."""
    result = classify(
        _candidate(title="דבר אחר לגמרי"),
        [_existing(title="טיול שנתי", start=_jlm(10, 16, 0))],
        similarity=_similarity, now=NOW, tz_name="Asia/Jerusalem",
        signal=ReconSignal(change=True, cancel=True),
    )
    assert result.verdict is Verdict.NEW


def test_signal_for_phrases():
    assert signal_for(["תיקון טעות: האספה ב-18:00"]).change is True
    assert signal_for(["שינוי בשעה"]).change is True
    assert signal_for(["update: the trip starts later"]).change is True
    assert signal_for(["הטיול בוטל"]).cancel is True
    assert signal_for(["הטיול מבוטל"]).cancel is True
    assert signal_for(["cancelled due to rain"]).cancel is True
    assert signal_for(["בוטל והועבר ל-19:00"]).cancel is True
    assert signal_for(["נא לשלוח חולצה לבנה"]).change is False
    assert signal_for(["הטיול מחר"]).cancel is False
    # multiple quotes OR together; hints add but never remove
    assert signal_for(["נא לשלוח חולצה", "הטיול בוטל"]).cancel is True
    assert signal_for([], hint_cancel=True).cancel is True


# --- DST (item 41): local-day windows resolve through ZoneInfo ------------------


def test_dst_boundary_same_local_day_across_the_shift():
    """Item 41: the same wall clock around the 25.10 shift is ONE local day
    and within the +/-1h identity — the window must never depend on a fixed
    offset (a +03:00 implementation shifts every post-shift wall time by 1h,
    exactly the tolerance). Both instants resolve to LOCAL day 25.10 through
    ZoneInfo, so the identity match holds across the boundary."""
    # Sun 25.10 01:30 exists TWICE (the clock repeats): fold=0 is +03:00,
    # fold=1 is +02:00 — same wall clock, one hour apart in UTC.
    jlm = ZoneInfo("Asia/Jerusalem")
    pre = _existing(title=BASE, start=datetime(2026, 10, 25, 1, 30, tzinfo=jlm))
    assert pre.start.utcoffset() == timedelta(hours=3)
    post = _candidate(
        title=BASE, start=datetime(2026, 10, 25, 1, 30, fold=1, tzinfo=jlm)
    )
    assert post.start.utcoffset() == timedelta(hours=2)
    result = classify(
        post, [pre], similarity=_similarity,
        now=datetime(2026, 10, 26, 12, 0, tzinfo=UTC), tz_name="Asia/Jerusalem",
    )
    assert result.verdict is Verdict.ALREADY_KNOWN
    assert result.matched_id == "e1"


# --- service-level: the DB write paths ------------------------------------------


class _Wipe:
    @staticmethod
    def wipe(session) -> None:
        session.execute(text("DELETE FROM item_relations"))
        session.execute(text("DELETE FROM enrichment_jobs"))
        session.execute(text("DELETE FROM item_sources"))
        session.execute(text("DELETE FROM ops_events"))
        session.execute(text("DELETE FROM memory_items"))
        session.execute(text("DELETE FROM memory_facts"))
        session.execute(text("DELETE FROM entity_aliases"))
        session.execute(text("DELETE FROM entities"))
        session.commit()


@pytest.fixture(name="db")
def db_fixture(database: None):
    assert_test_database(str(engine.url))
    session = SessionLocal()
    _Wipe.wipe(session)
    yield session
    session.rollback()
    session.close()
    _Wipe.wipe(session)


class _FakeMemory:
    def compact_block(self) -> str:
        return ""

    def resolve_applies_to(self, audience: dict) -> list[str]:
        return list(audience.get("named_kids", ["unknown"])) or ["unknown"]


def _make_source(
    session: Session, *, text: str, sender: str = "rinat@school.org.il",
    meta: dict[str, Any] | None = None,
) -> MemoryItem:
    item = MemoryItem(
        raw_text=text,
        source="email",
        meta=meta if meta is not None else {"sender": sender, "subject": "טיול שנתי"},
        occurred_at=datetime(2026, 11, 2, 8, 0, tzinfo=UTC),
    )
    session.add(item)
    session.commit()
    return item


def _make_existing_event(
    session: Session, *, start: datetime, title: str = "טיול שנתי"
) -> MemoryItem:
    item = MemoryItem(
        raw_text=title,
        source="email",
        type="event",
        occurred_at=start,
        applies_to=["unknown"],  # memory=None -> the pipeline resolves unknown
        status="filed",
    )
    session.add(item)
    session.commit()
    return item


def _candidate_child(
    title: str = "טיול שנתי",
    when_raw: str = "10.11 18:00",
    quote: str = "הטיול יוצא ב-18:00",
    recon: ReconHint | None = None,
    location: str | None = None,
    end: str | None = None,
) -> ChildCandidate:
    return ChildCandidate(
        kind="event",
        title=title,
        quote=quote,
        when_raw=when_raw,
        location=location,
        end=end,
        recon=recon,
    )


class TrigramEmbedder:
    """A hermetic stand-in for the recon embedder: deterministic character
    3-gram TF vectors over the WHOLE string (Unicode-aware, so Hebrew
    behaves like the real multilingual model: near-identical strings score
    high, distinct activities low). Not a quality claim — a wiring fake."""

    def embed(self, text: str) -> list[float]:
        grams: dict[str, int] = {}
        t = text.casefold()
        for i in range(max(0, len(t) - 2)):
            grams[t[i : i + 3]] = grams.get(t[i : i + 3], 0) + 1
        if not grams:
            return [0.0]
        return _trigram_vec(text)

    def embed_batch(self, texts: list[str]) -> list[list[float]]:
        return [self.embed(t) for t in texts]


_TRIGRAM_VOCAB: list[str] = []


def _trigram_vec(text: str) -> list[float]:
    """Sparse-in-dense: vector over the UNION of both strings' 3-grams is
    impossible for a single-call embedder, so TrigramEmbedder embeds into a
    GLOBAL vocab built lazily per process from every string it has seen."""
    t = text.casefold()
    for i in range(max(0, len(t) - 2)):
        g = t[i : i + 3]
        if g not in _TRIGRAM_INDEX:
            _TRIGRAM_INDEX[g] = len(_TRIGRAM_INDEX)
    vec = [0.0] * len(_TRIGRAM_INDEX)
    for i in range(max(0, len(t) - 2)):
        vec[_TRIGRAM_INDEX[t[i : i + 3]]] += 1.0
    norm = sum(v * v for v in vec) ** 0.5 or 1.0
    return [v / norm for v in vec]


_TRIGRAM_INDEX: dict[str, int] = {}


def _service(session, source: MemoryItem, *, tz_name: str = "Asia/Jerusalem") -> EnrichmentService:
    """EnrichmentService bound to the shared session (the REAL production
    construction path — the similarity factory is a hermetic fake, so the
    B1 lazy build actually runs without loading a model)."""
    from brain.services.extraction.reconciliation import embedder_similarity

    repo = ItemsRepository(session)
    return EnrichmentService(
        repo, session, extractor=None, memory=None, daily_cap=100,
        timezone=tz_name,
        similarity_factory=lambda: embedder_similarity(TrigramEmbedder()),
    )


def test_update_verdict_proposes_change_never_applies(db):
    """Cross-thread: the OLD item stays intact; a proposed supersedes lands."""
    source = _make_source(db, text="שינוי: הטיול יוצא ב-18:00", sender="other@school.org.il")
    existing = _make_existing_event(db, start=_jlm(10, 16, 0))
    service = _service(db, source)
    resolved = service._validate(
        _candidate_child(recon=ReconHint(change=True)), source, NOW
    )
    assert resolved is not None
    outcome = service._reconcile_child(resolved, source, {}, uuid.uuid4(), NOW)
    assert outcome.verdict is Verdict.UPDATE
    rel = db.query(ItemRelation).filter_by(kind="supersedes").one()
    assert rel.to_id == existing.id
    assert (rel.provenance or {}).get("state") == "proposed"
    db.refresh(existing)
    assert existing.superseded_by is None  # never applied silently


def test_same_thread_same_sender_update_auto_applies_with_undo(db):
    """20a + decision 3: same thread AND same sender -> applied + strike + undo.

    The existing event's ORIGINAL source is the teacher's email; the
    correction arrives from the SAME sender in the SAME thread (Re:)."""
    original = _make_source(db, text="טיול שנתי יוצא ב-16:00")
    source = _make_source(db, text="תיקון טעות: הטיול יוצא ב-18:00")  # same sender
    existing = _make_existing_event(db, start=_jlm(10, 16, 0))
    db.add(ItemSource(child_id=existing.id, source_id=original.id, quote="טיול שנתי"))
    db.commit()
    service = _service(db, source)
    resolved = service._validate(
        _candidate_child(recon=ReconHint(change=True)), source, NOW
    )
    outcome = service._reconcile_child(resolved, source, {}, uuid.uuid4(), NOW)
    assert outcome.verdict is Verdict.UPDATE
    rel = db.query(ItemRelation).filter_by(kind="supersedes").one()
    assert (rel.provenance or {}).get("state") == "applied"
    db.refresh(existing)
    assert existing.superseded_by == rel.from_id
    # undo restores (D4) — the service runs on its own session (default factory)
    relations = RelationsService()
    relations.undo_supersede(rel.id)
    db.refresh(existing)
    assert existing.superseded_by is None


def test_same_thread_different_sender_is_propose_only(db):
    """20a + decision 3: same thread but a DIFFERENT sender -> propose only."""
    original = _make_source(db, text="טיול שנתי יוצא ב-16:00")  # teacher
    source = _make_source(
        db, text="תיקון טעות: הטיול יוצא ב-18:00", sender="parent@gmail.com"
    )
    existing = _make_existing_event(db, start=_jlm(10, 16, 0))
    db.add(ItemSource(child_id=existing.id, source_id=original.id, quote="טיול שנתי"))
    db.commit()
    service = _service(db, source)
    resolved = service._validate(
        _candidate_child(recon=ReconHint(change=True)), source, NOW
    )
    outcome = service._reconcile_child(resolved, source, {}, uuid.uuid4(), NOW)
    assert outcome.verdict is Verdict.UPDATE
    rel = db.query(ItemRelation).filter_by(kind="supersedes").one()
    assert (rel.provenance or {}).get("state") == "proposed"
    db.refresh(existing)
    assert existing.superseded_by is None


def test_already_known_links_source_without_new_child(db):
    source = _make_source(db, text="לזכור: טיול שנתי ב-10.11")
    existing = _make_existing_event(db, start=_jlm(10, 16, 0))
    before = db.query(MemoryItem).count()
    service = _service(db, source)
    resolved = service._validate(
        _candidate_child(title="טיול שנתי", when_raw="10.11",
                         quote="לזכור: טיול שנתי ב-10.11"),
        source, NOW
    )
    outcome = service._reconcile_child(resolved, source, {}, uuid.uuid4(), NOW)
    assert outcome.verdict is Verdict.ALREADY_KNOWN
    assert db.query(MemoryItem).count() == before  # no new child
    rows = db.query(ItemSource).filter_by(source_id=source.id, child_id=existing.id).all()
    assert len(rows) == 1 and rows[0].quote


def test_cancellation_proposes_strike_then_restore(db):
    source = _make_source(db, text="בוטל: " + BASE)
    existing = _make_existing_event(db, start=_jlm(10, 16, 0), title=BASE)
    service = _service(db, source)
    resolved = service._validate(
        ChildCandidate(kind="event", title="בוטל: " + BASE,
                       quote="בוטל: " + BASE, when_raw=""),
        source, NOW,
    )
    assert resolved is not None  # dateless child passes grounding (verbatim quote)
    outcome = service._reconcile_child(resolved, source, {}, uuid.uuid4(), NOW)
    assert outcome.verdict is Verdict.CANCELLATION
    rel = db.query(ItemRelation).filter_by(kind="cancels").one()
    assert (rel.provenance or {}).get("state") == "proposed"
    db.refresh(existing)
    meta = existing.extraction_meta or {}
    assert not meta.get("cancelled")  # proposed: nothing applied silently
    # the ✅ path exists and strikes; ↩︎ restores (D4)
    service.recon.apply_cancellation(rel.id)
    db.refresh(existing)
    assert (existing.extraction_meta or {}).get("cancelled")
    service.recon.restore_cancellation(rel.id)
    db.refresh(existing)
    assert not (existing.extraction_meta or {}).get("cancelled")
    assert (rel.provenance or {}).get("state") == "undone"


def test_dropped_child_suppresses_only_same_thread(db):
    """Item 30: the same source's dropped child suppresses; a cross-source
    look-alike of a dropped child classifies NEW."""
    source = _make_source(db, text="הטיול יוצא ב-18:00 שוב")
    dropped = MemoryItem(
        raw_text="טיול שנתי", source="email", type="event",
        occurred_at=_jlm(10, 16, 0), child_state="dropped",
        applies_to=["unknown"], status="inbox",
    )
    db.add(dropped)
    db.commit()
    db.add(ItemSource(child_id=dropped.id, source_id=source.id, quote="טיול שנתי"))
    db.commit()
    service = _service(db, source)
    resolved = service._validate(_candidate_child(title="טיול שנתי"), source, NOW)
    outcome = service._reconcile_child(resolved, source, {}, uuid.uuid4(), NOW)
    assert outcome is None  # NEW -> the caller creates; here: nothing (dropped)
    other = _make_source(db, text="הטיול יוצא ב-18:00 מהצ'אט", sender="chat@whatsapp.local")
    resolved2 = service._validate(_candidate_child(title="טיול שנתי"), other, NOW)
    outcome2 = service._reconcile_child(resolved2, other, {}, uuid.uuid4(), NOW)
    assert outcome2 is None  # cross-source look-alike of a DROPPED child: NEW (-> None)


def test_recon_eval_offline_end_to_end(tmp_path, capsys):
    """brain recon-eval: per-verdict P/R + confusion matrix, no DB/network."""
    from brain.cli import run_recon_eval

    cases = [
        {"case_id": "c1", "existing_items": [
            {"id": "e1", "type": "event", "title": "טיול שנתי",
             "start": _jlm(10, 16, 0).isoformat(), "applies_to": ["Rona"],
             "location": None}],
         "new_text_quote": "הטיול בוטל — ניפגש במועד אחר",
         "candidate": {"kind": "event", "title": "טיול שנתי", "start": None,
                       "applies_to": ["Rona"], "location": None},
         "verdict": "cancellation"},
        {"case_id": "c2", "existing_items": [
            {"id": "e1", "type": "event", "title": "טיול שנתי",
             "start": _jlm(10, 16, 0).isoformat(), "applies_to": ["Rona"],
             "location": None}],
         "new_text_quote": "שינוי: הטיול ב-18:00",
         "candidate": {"kind": "event", "title": "טיול שנתי",
                       "start": _jlm(10, 18, 0).isoformat(),
                       "applies_to": ["Rona"], "location": None},
         "verdict": "update"},
    ]
    path = tmp_path / "cases.jsonl"
    path.write_text("\n".join(json.dumps(c) for c in cases), encoding="utf-8")
    report = run_recon_eval(path, now=NOW, tz_name="Asia/Jerusalem")
    assert report["cases"] == 2
    assert report["per_verdict"]["update"]["tp"] == 1
    assert report["per_verdict"]["cancellation"]["tp"] == 1
    assert sum(sum(row.values()) for row in report["confusion"].values()) == 2


def test_signal_scoped_to_the_candidates_own_quote(db):
    """BLOCKER-1 (review round 1): the change/cancel signal is read from the
    candidate's OWN grounded quote — never the whole source.

    One school email cancels activity X and lists activity Y with no signal
    word for it. Both candidates match existing same-day items. Exactly one
    CANCELLATION (X) and one ALREADY_KNOWN (Y) — a whole-source scan would
    give BOTH a cancel signal.
    """
    Y_BASE = "איסוף מוקדם מהחוג ביום שלישי 10.11"
    cancel_quote = "בוטל: " + BASE
    plain_quote = "נזכיר: " + Y_BASE
    source = _make_source(db, text=cancel_quote + "\n" + plain_quote)
    _make_existing_event(db, start=_jlm(10, 16, 0), title=BASE)
    _make_existing_event(db, start=_jlm(10, 18, 0), title=Y_BASE)
    service = _service(db, source)

    resolved_x = service._validate(
        ChildCandidate(kind="event", title="בוטל: " + BASE,
                       quote=cancel_quote, when_raw=""),
        source, NOW,
    )
    assert resolved_x is not None
    outcome_x = service._reconcile_child(resolved_x, source, {}, uuid.uuid4(), NOW)
    assert outcome_x.verdict is Verdict.CANCELLATION  # X's own quote has the phrase

    resolved_y = service._validate(
        ChildCandidate(kind="event", title=Y_BASE,
                       quote=plain_quote, when_raw="10.11 18:00"),
        source, NOW,
    )
    assert resolved_y is not None
    outcome_y = service._reconcile_child(resolved_y, source, {}, uuid.uuid4(), NOW)
    # Y's own quote is phrase-free: same-day identity with nothing new ->
    # ALREADY KNOWN, never CANCELLATION via the sibling's cancel words
    assert outcome_y.verdict is Verdict.ALREADY_KNOWN


def test_auto_apply_never_touches_a_user_acted_item(db):
    """BLOCKER-2 (review round 2): decision 6 — proposals MAY target kept
    items, but NOTHING on them changes until the user confirms. Same thread
    AND same sender AND a KEPT existing item -> the UPDATE stays proposed;
    superseded_by is untouched."""
    original = _make_source(db, text="טיול שנתי יוצא ב-16:00")
    source = _make_source(db, text="תיקון טעות: הטיול יוצא ב-18:00")  # same sender
    existing = _make_existing_event(db, start=_jlm(10, 16, 0))
    existing.child_state = "kept"  # the user already acted on this item
    db.add(ItemSource(child_id=existing.id, source_id=original.id, quote="טיול שנתי"))
    db.commit()
    service = _service(db, source)
    resolved = service._validate(
        _candidate_child(recon=ReconHint(change=True)), source, NOW
    )
    outcome = service._reconcile_child(resolved, source, {}, uuid.uuid4(), NOW)
    assert outcome.verdict is Verdict.UPDATE
    rel = db.query(ItemRelation).filter_by(kind="supersedes").one()
    assert (rel.provenance or {}).get("state") == "proposed"
    db.refresh(existing)
    assert existing.superseded_by is None  # user-acted: immutable until confirmed


def test_email_sender_comes_from_the_real_parse_path(db):
    """BUG 4 (review round 2): email sources carry the sender under
    meta['from'] ("Name <addr>"), not meta['sender'] — only WhatsApp writes
    'sender'. The source below is built through the REAL paste.parse() email
    path (no hand-written meta). Same thread + same sender -> auto-apply
    fires (20a) instead of failing safe forever."""
    from brain.services.paste import parse as parse_paste

    original = parse_paste(
        "From: Rinat <rinat@school.org.il>\nTo: parents@school.org.il\n"
        "Subject: טיול שנתי\nDate: Sun, 1 Nov 2026 10:00:00 +0200\n\n"
        "טיול שנתי יוצא ב-16:00\n",
        kind_hint="email", tz=JLM, now=NOW,
    )
    original_item = _make_source(
        db, text="טיול שנתי יוצא ב-16:00", meta=dict(original.chunks[0].meta)
    )
    reply = parse_paste(
        "From: Rinat <rinat@school.org.il>\nTo: parents@school.org.il\n"
        "Subject: Re: טיול שנתי\nDate: Sun, 1 Nov 2026 11:00:00 +0200\n\n"
        "תיקון טעות: הטיול יוצא ב-18:00\n",
        kind_hint="email", tz=JLM, now=NOW,
    )
    source = _make_source(db, text="תיקון טעות: הטיול יוצא ב-18:00",
                          meta=dict(reply.chunks[0].meta))
    existing = _make_existing_event(db, start=_jlm(10, 16, 0))
    db.add(ItemSource(child_id=existing.id, source_id=original_item.id,
                      quote="טיול שנתי"))
    db.commit()
    service = _service(db, source)
    resolved = service._validate(
        _candidate_child(recon=ReconHint(change=True)), source, NOW
    )
    outcome = service._reconcile_child(resolved, source, {}, uuid.uuid4(), NOW)
    assert outcome.verdict is Verdict.UPDATE
    rel = db.query(ItemRelation).filter_by(kind="supersedes").one()
    assert (rel.provenance or {}).get("state") == "applied"
    db.refresh(existing)
    assert existing.superseded_by == rel.from_id


def test_confirmed_cancellation_hides_until_restored(db):
    """BLOCKER-3 (review round 2): a confirmed cancellation hides the item in
    every default listing/search/ask leg — the same default-hiding rule as
    superseded_by (item 20/D4) — and restore makes it visible again."""
    from brain.repositories.items import ItemsRepository

    source = _make_source(db, text="בוטל: " + BASE)
    existing = _make_existing_event(db, start=_jlm(10, 16, 0), title=BASE)
    service = _service(db, source)
    resolved = service._validate(
        ChildCandidate(kind="event", title="בוטל: " + BASE,
                       quote="בוטל: " + BASE, when_raw=""),
        source, NOW,
    )
    outcome = service._reconcile_child(resolved, source, {}, uuid.uuid4(), NOW)
    assert outcome.verdict is Verdict.CANCELLATION
    rel = db.query(ItemRelation).filter_by(kind="cancels").one()
    service.recon.apply_cancellation(rel.id)
    db.refresh(existing)
    assert (existing.extraction_meta or {}).get("cancelled")  # truthy (D4 stamp)

    repo = ItemsRepository(db)
    ids = [i.id for i in repo.list(item_type=None, project=None, status=None, limit=50, offset=0)]
    assert existing.id not in ids  # default list hides
    occ = repo.by_occurrence(start=_jlm(10, 0, 0), end=_jlm(10, 23, 59), limit=50)
    assert existing.id not in [i.id for i in occ]
    hits = repo.by_text_any([BASE[:12]], limit=50)
    assert existing.id not in [i.id for i in hits]

    # restore: visible again everywhere
    service.recon.restore_cancellation(rel.id)
    db.refresh(existing)
    assert not (existing.extraction_meta or {}).get("cancelled")
    ids = [i.id for i in repo.list(item_type=None, project=None, status=None, limit=50, offset=0)]
    assert existing.id in ids
    occ = repo.by_occurrence(start=_jlm(10, 0, 0), end=_jlm(10, 23, 59), limit=50)
    assert existing.id in [i.id for i in occ]


def test_signal_for_hebrew_inflections_and_english_patterns():
    """E4 (review round 3): signals match STEMS/patterns, not a word list —
    my own examples for every inflection family the manager named."""
    # cancel family: בוטל* / מבוטל* / מתבטל* / ביטול / לא יתקיים / called off
    for sample in (
        "החוג בוטלה השבוע",          # בוטלה
        "הטיולים בוטלו",              # בוטלו
        "המסיבה מבוטלת",              # מבוטל*
        "השיעור מתבטל",               # מתבטל*
        "עקב הגשם: ביטול הטיול",      # ביטול
        "האספה לא תתקיים",            # לא תתקיים
        "the trip is cancelled due to rain",   # cancel family
        "the trip is canceled",                # one-l spelling
        "the trip is called off",              # called off
    ):
        assert signal_for([sample]).cancel is True, sample
    # change family: שונה / עודכן / נדחה / הוקדם / במקום / ולא ב
    for sample in (
        "השעה שונתה",                 # שונה*
        "המפגש עודכן ל-19:00",        # עודכן*
        "הטיול נדחה ליום חמישי",      # נדחה* (change)
        "האספה הוקדמה ל-17:00",       # הוקדם* stem of הוקדמה
        "ניפגש בפארק במקום הגן",      # במקום
        "ניפגש בפארק ולא בגן",        # ולא ב
    ):
        assert signal_for([sample]).change is True, sample
    # neutral text: no signal
    assert signal_for(["נא לשלוח כובע ובקבוק מים"]).change is False
    assert signal_for(["נא לשלוח כובע ובקבוק מים"]).cancel is False
    # a cancelled-STAYED-cancelled sentence is not a change
    assert signal_for(["הטיול בוטל"]).change is False


CAL_BASE = BASE + " עם כל ההורים והילדים והחברים של כיתה ה והמדריכים וגם הסבים"


def _make_calendar_item(
    session: Session, *, title: str, location: str | None,
    start: datetime, end: datetime | None = None,
) -> MemoryItem:
    """A REAL pasted calendar item: meta comes from the actual paste parser
    (title/location/start/end keys), not hand-written recon-friendly data."""
    from brain.services.paste import parse as parse_paste

    lines = [
        "BEGIN:VEVENT", f"UID:cal-{title.count(' ')}@example.com",
        f"SUMMARY:{title}",
    ]
    if location:
        lines.append(f"LOCATION:{location}")
    lines.append(f"DTSTART;TZID=Asia/Jerusalem:{start.strftime('%Y%m%dT%H%M%S')}")
    if end is not None:
        lines.append(f"DTEND;TZID=Asia/Jerusalem:{end.strftime('%Y%m%dT%H%M%S')}")
    lines.append("END:VEVENT")
    parsed = parse_paste("\n".join(lines) + "\n", kind_hint="calendar",
                         tz=JLM, now=NOW)
    meta = dict(parsed.chunks[0].meta)
    item = MemoryItem(
        raw_text=parsed.chunks[0].raw_text,
        source="calendar",
        type="event",
        occurred_at=start,
        meta=meta,
        status="filed",
    )
    session.add(item)
    session.commit()
    return item


def test_more_detail_when_candidate_states_a_missing_location(db):
    """F2 + E3 (rounds 2-3): the existing item's location/end come from the
    REAL paste meta; the candidate carries location through resolution. The
    existing calendar item states NO location; the candidate states one ->
    MORE DETAIL. Title length is never evidence."""
    _make_calendar_item(
        db, title=CAL_BASE, location=None, start=_jlm(10, 16, 0),
    )
    source = _make_source(db, text="לזכור: " + CAL_BASE + " בפארק השרון")
    service = _service(db, source)
    resolved = service._validate(
        _candidate_child(title=CAL_BASE, when_raw="10.11 16:00",
                         quote="לזכור: " + CAL_BASE + " בפארק השרון",
                         location="פארק השרון"), source, NOW
    )
    assert resolved is not None
    outcome = service._reconcile_child(resolved, source, {}, uuid.uuid4(), NOW)
    assert outcome.verdict is Verdict.MORE_DETAIL


def test_location_change_with_signal_is_update(db):
    """F2: differing location + change signal -> UPDATE (fields carry the
    location diff). Same day, same time — only the place moved."""
    _make_calendar_item(
        db, title=CAL_BASE, location="פארק השרון", start=_jlm(10, 16, 0),
    )
    source = _make_source(db, text="שינוי מקום: " + CAL_BASE + " בגן הלאומי")
    service = _service(db, source)
    resolved = service._validate(
        _candidate_child(title=CAL_BASE, when_raw="10.11 16:00",
                         quote="שינוי מקום: " + CAL_BASE + " בגן הלאומי",
                         location="גן הלאומי",
                         recon=ReconHint(change=True)), source, NOW
    )
    assert resolved is not None
    outcome = service._reconcile_child(resolved, source, {}, uuid.uuid4(), NOW)
    assert outcome.verdict is Verdict.UPDATE
    rel = db.query(ItemRelation).filter_by(kind="supersedes").one()
    assert (rel.provenance or {}).get("fields", {}).get("location") == [
        "פארק השרון", "גן הלאומי",
    ]


def test_update_never_targets_another_sources_proposed_child(db):
    """F3 (round 4): still-PROPOSED children of OTHER sources are matchable
    only for ALREADY KNOWN / MORE DETAIL. An UPDATE (or CONFLICT/CANCELLATION)
    against one is emitted as NEW + ops recon/target_unapproved. A KEPT child
    is a reliable target and takes the UPDATE."""
    other = _make_source(db, text="טיול שנתי יוצא ב-16:00", sender="chat@whatsapp.local")
    source = _make_source(db, text="תיקון טעות: הטיול יוצא ב-18:00")
    proposed = MemoryItem(
        raw_text="טיול שנתי", source="email", type="event",
        occurred_at=_jlm(10, 16, 0), child_state="proposed",
        applies_to=["unknown"], status="filed",
    )
    db.add(proposed)
    db.commit()
    db.add(ItemSource(child_id=proposed.id, source_id=other.id, quote="טיול שנתי"))
    db.commit()
    service = _service(db, source)
    resolved = service._validate(
        _candidate_child(recon=ReconHint(change=True)), source, NOW
    )
    outcome = service._reconcile_child(resolved, source, {}, uuid.uuid4(), NOW)
    assert outcome is None  # NEW: never update an unapproved proposal
    events = db.query(OpsEvent).filter_by(
        component="enrichment", kind="recon_target_unapproved").all()
    assert len(events) == 1
    # the kept side: the same child approved -> a reliable target
    db.query(OpsEvent).delete()
    db.commit()
    proposed.child_state = "kept"
    db.commit()
    resolved2 = service._validate(
        _candidate_child(recon=ReconHint(change=True)), source, NOW
    )
    outcome2 = service._reconcile_child(resolved2, source, {}, uuid.uuid4(), NOW)
    assert outcome2.verdict is Verdict.UPDATE
    rel = db.query(ItemRelation).filter_by(kind="supersedes").one()
    assert (rel.provenance or {}).get("state") == "proposed"


def test_matchable_items_windows_are_pushed_into_sql(db):
    """PERF 5 (round 2): the same-day/extended windows filter in SQL — an
    item outside every window is never loaded (the compiled WHERE carries
    the occurred_at bounds), and results equal the Python reference."""
    from brain.services.extraction.reconciliation import ReconSignal

    _make_existing_event(db, start=_jlm(10, 16, 0), title=BASE)          # in-window
    _make_existing_event(db, start=_jlm(20, 16, 0), title=BASE + " חלק ב")  # Dec 20: outside
    service = _service(db, _make_source(db, text="טיול שנתי"))
    cand = ReconCandidate(kind="event", title=BASE, start=_jlm(10, 16, 0),
                          applies_to=("unknown",))
    matches = service.recon.matchable_items(
        "event", set(), candidate=cand, now=NOW, signal=ReconSignal()
    )
    titles = [pair[0].title for pair in matches]
    assert len(titles) == 1 and BASE in titles[0]
    # the SQL statement itself carries the window bounds
    stmt = service.recon._window_statement("event", set(), cand, ReconSignal(), NOW)
    compiled = str(stmt)
    assert "occurred_at" in compiled and ">=" in compiled


def test_a_verdict_is_one_transaction(db):
    """F1 (round 4): a non-ALREADY_KNOWN verdict commits ONCE — child,
    relation and (rare) supersede land together or not at all. If write()
    fails after the child is flushed, a rollback leaves NEITHER: a re-run
    can then classify from a clean slate (no half-applied verdict)."""
    source = _make_source(db, text="בוטל: " + BASE)
    _make_existing_event(db, start=_jlm(10, 16, 0), title=BASE)
    service = _service(db, source)
    resolved = service._validate(
        ChildCandidate(kind="event", title="בוטל: " + BASE,
                       quote="בוטל: " + BASE, when_raw=""),
        source, NOW,
    )
    assert resolved is not None

    original_write = ReconciliationService.write

    def exploding_write(self, result, *, source, child, quotes=None):
        raise RuntimeError("boom between child and relation")

    ReconciliationService.write = exploding_write
    try:
        with pytest.raises(RuntimeError):
            service._reconcile_child(resolved, source, {}, uuid.uuid4(), NOW)
    finally:
        ReconciliationService.write = original_write

    db.rollback()  # the caller's crash recovery
    assert db.query(MemoryItem).filter(MemoryItem.type == "event").filter(
        MemoryItem.child_state == "proposed").count() == 0  # no verdict child
    assert db.query(ItemRelation).count() == 0  # no relation


def test_recon_eval_accepts_spec_shape(tmp_path):
    """E5 (round 3): the eval accepts the SPEC's case shape — `type` (kind
    as an alias), verdicts compared CASE-INSENSITIVELY, and an optional
    per-case `now` (the message's own timestamp, so a dated correction in
    the past is judged inside its own window, not today's)."""
    from brain.cli import run_recon_eval

    cases = [
        # `type` instead of `kind`, verdict in CAPS, dated correction in the
        # past: its own `now` (2 Nov) keeps 10.11 inside the extended window
        {"case_id": "spec-1", "now": "2026-11-02T12:00:00+02:00",
         "existing_items": [
             {"id": "e1", "type": "event", "title": "טיול שנתי",
              "start": _jlm(10, 16, 0).isoformat(), "applies_to": ["Rona"]}],
         "new_text_quote": "תיקון: הטיול ב-18:00",
         "candidate": {"type": "event", "title": "טיול שנתי",
                       "start": _jlm(10, 18, 0).isoformat(),
                       "applies_to": ["Rona"]},
         "verdict": "UPDATE"},
        # mixed-case verdict + kind still works
        {"case_id": "spec-2",
         "existing_items": [
             {"id": "e2", "type": "event", "title": "טיול שנתי",
              "start": _jlm(10, 16, 0).isoformat(), "applies_to": ["Rona"]}],
         "new_text_quote": "הטיול בוטל",
         "candidate": {"kind": "event", "title": "טיול שנתי", "start": None,
                       "applies_to": ["Rona"]},
         "verdict": "Cancellation"},
    ]
    path = tmp_path / "cases.jsonl"
    path.write_text("\n".join(json.dumps(c) for c in cases), encoding="utf-8")
    report = run_recon_eval(path, tz_name="Asia/Jerusalem")
    assert report["cases"] == 2
    assert report["per_verdict"]["update"]["tp"] == 1
    assert report["per_verdict"]["cancellation"]["tp"] == 1


def test_similarity_uses_the_embedder_for_paraphrases(db):
    """E1 (round 5): matching runs on EMBEDDING cosine (the local embedder),
    with the lexical ratio only as a fast accept >= 0.85. A paraphrase pair
    (lexical far below 0.85, embedding-close) MATCHES; the pure classify
    gets the embedding callable through ReconciliationService. Deterministic
    FakeEmbedder (English) proves the wiring; the dev set uses the real
    local model."""
    from brain.ai.embedder import FakeEmbedder

    source_row = _make_source(db, text="x")  # any session-bound row
    service = _service(db, source_row)
    service.recon.attach_embedder(FakeEmbedder())
    # lexical 0.564 < 0.85; FakeEmbedder cosine 0.882 >= 0.85: only the
    # EMBEDDING callable can see these as the same activity
    existing_row = _make_existing_event(
        db, start=_jlm(10, 16, 0), title="annual park trip"
    )
    candidate = ReconCandidate(
        kind="event", title="annual trip to the park",
        start=_jlm(10, 16, 0), applies_to=("unknown",),
    )
    result = service.recon.classify_universe(
        candidate, exclude_ids={source_row.id}, now=NOW, signal=ReconSignal()
    )
    from brain.services.relations import similarity_ratio as _lex
    assert _lex(candidate.title, existing_row.raw_text) < 0.85
    assert result.verdict is Verdict.ALREADY_KNOWN


def test_unknown_audience_never_matches_a_known_kid(db):
    """E1 point 3 (round 5): NULL/unknown matches unknown ONLY. A cancellation
    for one kid's activity must never strike an unscoped (NULL-audience)
    pasted item that is really another kid's."""
    source = _make_source(db, text="בוטל: " + BASE)
    _make_calendar_item(db, title=BASE, location=None, start=_jlm(10, 16, 0))
    # force the pasted item's audience to a KNOWN kid (not NULL/unknown)
    from sqlalchemy import update as sa_update
    db.execute(sa_update(MemoryItem).where(MemoryItem.raw_text.like(
        "%" + BASE[:12] + "%")).values(applies_to=["Noa"]))
    db.commit()
    service = _service(db, source)
    resolved = service._validate(
        ChildCandidate(kind="event", title="בוטל: " + BASE,
                       quote="בוטל: " + BASE, when_raw=""),
        source, NOW,
    )
    outcome = service._reconcile_child(resolved, source, {}, uuid.uuid4(), NOW)
    assert outcome is None  # no match across unknown->known: plain NEW


def test_recon_eval_uses_the_embedding_callable(tmp_path):
    """E1: recon-eval matches through the SAME embedding callable as
    production (built from the local embedder; injectable for tests)."""
    from brain.cli import run_recon_eval

    cases = [{
        "case_id": "para-1",
        # lexical 0.564 < 0.85; FakeEmbedder cosine 0.882: only the
        # embedding callable sees these as the same activity
        "now": _jlm(10, 12, 0).isoformat(),  # judged in its own local day
        "existing_items": [{"id": "e1", "type": "event",
                            "title": "annual park trip",
                            "start": _jlm(10, 16, 0).isoformat(),
                            "applies_to": ["Rona"]}],
        "new_text_quote": "לזכור: הטיול",
        "candidate": {"type": "event", "title": "annual trip to the park",
                      "start": _jlm(10, 16, 0).isoformat(),
                      "applies_to": ["Rona"]},
        "verdict": "already_known",
    }]
    path = tmp_path / "cases.jsonl"
    path.write_text(json.dumps(cases[0]), encoding="utf-8")
    from brain.ai.embedder import FakeEmbedder
    report = run_recon_eval(path, tz_name="Asia/Jerusalem",
                            embedder=FakeEmbedder())
    assert report["per_verdict"]["already_known"]["tp"] == 1


def test_end_flows_to_diff_and_persists_on_the_child(db):
    """Round-5 addition: the candidate's END reaches the diff (UPDATE) and
    the stored child carries end+location in extraction_meta, so a LATER
    run's reconciliation can compare against children too."""
    # no LOCATION in the fixture: with the hermetic fake embedder the
    # oneliner title must stay within the lexical fast-accept of CAL_BASE
    # (the "@ location" suffix drops it to 0.849; the real model rescues it
    # via cosine — the fake cannot, so keep the fixture lean)
    _make_calendar_item(
        db, title=CAL_BASE, location=None,
        start=_jlm(10, 16, 0), end=_jlm(10, 17, 0),
    )
    source = _make_source(db, text="שינוי: " + CAL_BASE + " עד 19:00")
    service = _service(db, source)
    resolved = service._validate(
        _candidate_child(title=CAL_BASE, when_raw="10.11 16:00",
                         quote="שינוי: " + CAL_BASE + " עד 19:00",
                         location="פארק השרון", end="19:00",
                         recon=ReconHint(change=True)), source, NOW
    )
    assert resolved is not None and resolved.end is not None
    assert resolved.end.astimezone(JLM).hour == 19  # local wall time
    outcome = service._reconcile_child(resolved, source, {}, uuid.uuid4(), NOW)
    assert outcome.verdict is Verdict.UPDATE
    rel = db.query(ItemRelation).filter_by(kind="supersedes").one()
    fields = (rel.provenance or {}).get("fields", {})
    assert fields.get("end") == ["17:00", "19:00"]  # beyond the +-1h identity
    # the verdict child persists end + location for later runs
    child = db.get(MemoryItem, rel.from_id)
    meta = child.extraction_meta or {}
    assert meta.get("end") == "2026-11-10T17:00:00+00:00"  # 19:00 JLM as UTC iso
    assert meta.get("location") == "פארק השרון"


def test_matchable_items_sql_windows_across_the_dst_change(db):
    """Round-5 addition: the SQL window legs are DAY-based through the 25 Oct
    DST fold — a same-local-day item matches, the previous day's 23:50 does
    not, and the extended window edge behaves the same way."""
    from brain.services.extraction.reconciliation import ReconSignal

    # Sun 25 Oct 2026: IDT->IST fold at 02:00; a 01:30 local item is ambiguous
    item_fold = _make_existing_event(db, start=datetime(2026, 10, 25, 1, 30, tzinfo=JLM),
                                     title=BASE)
    item_prev = _make_existing_event(
        db, start=datetime(2026, 10, 24, 23, 50, tzinfo=JLM), title=BASE + " א"
    )
    service = _service(db, _make_source(db, text="טיול שנתי"))
    cand = ReconCandidate(kind="event", title=BASE,
                          start=datetime(2026, 10, 25, 1, 30, tzinfo=JLM, fold=0),
                          applies_to=("unknown",))
    matches = service.recon.matchable_items(
        "event", set(), candidate=cand, now=NOW, signal=ReconSignal()
    )
    ids = [pair[1].id for pair in matches]
    assert item_fold.id in ids      # same local day through the fold
    assert item_prev.id not in ids  # the 24th's 23:50 is another local day
    # extended edge: a signal candidate sees a +30d item, not a +31d one
    item_edge_in = _make_existing_event(db, start=_jlm(24, 16, 0), title=BASE + " ב")
    item_edge_out = _make_existing_event(
        db, start=datetime(2026, 12, 3, 12, 0, tzinfo=JLM), title=BASE + " ג"
    )
    matches_signal = service.recon.matchable_items(
        "event", set(), candidate=cand, now=NOW,
        signal=ReconSignal(change=True),
    )
    ids_signal = [pair[1].id for pair in matches_signal]
    assert item_edge_in.id in ids_signal   # 24 Nov = today+29d (in)
    assert item_edge_out.id not in ids_signal  # 3 Dec = today+31d (out)


def test_exact_clock_diff_conflict_without_signal(db):
    """E2 (round 5): the +-1h tolerance is IDENTITY only (the windows); field
    diffs are EXACT — 16:00 vs 16:15 same day with NO signal is a CONFLICT,
    not a silent drop and not ALREADY_KNOWN."""
    _make_calendar_item(
        db, title=CAL_BASE, location=None,
        start=_jlm(10, 16, 0), end=_jlm(10, 17, 0),
    )
    source = _make_source(db, text=CAL_BASE + " 16:15")
    service = _service(db, source)
    resolved = service._validate(
        _candidate_child(title=CAL_BASE, when_raw="10.11 16:15",
                         quote=CAL_BASE + " 16:15"), source, NOW
    )
    assert resolved is not None and resolved.start is not None
    outcome = service._reconcile_child(resolved, source, {}, uuid.uuid4(), NOW)
    assert outcome.verdict is Verdict.CONFLICT


def test_exact_clock_diff_update_with_signal(db):
    """E2 (round 5): the same exact 15-minute diff WITH a change signal is an
    UPDATE whose provenance records the exact stated values."""
    _make_calendar_item(
        db, title=CAL_BASE, location=None,
        start=_jlm(10, 16, 0), end=_jlm(10, 17, 0),
    )
    source = _make_source(db, text="שינוי: " + CAL_BASE + " 16:15")
    service = _service(db, source)
    resolved = service._validate(
        _candidate_child(title=CAL_BASE, when_raw="10.11 16:15",
                         quote="שינוי: " + CAL_BASE + " 16:15",
                         recon=ReconHint(change=True)), source, NOW
    )
    outcome = service._reconcile_child(resolved, source, {}, uuid.uuid4(), NOW)
    assert outcome.verdict is Verdict.UPDATE
    rel = db.query(ItemRelation).filter_by(kind="supersedes").one()
    assert (rel.provenance or {}).get("fields", {}).get("time") == ["16:00", "16:15"]


def test_recon_uses_the_mini_lm_model_and_the_0_80_threshold():
    """Round-5 decision 1: RECON_EMBED_MODEL selects the recon embedder
    (MiniLM-L12, offline, in-memory; search/Qdrant keep potion); the match
    threshold is 0.80 and the lexical fast-accept fires only at >= 0.95."""
    from brain.config import Settings
    from brain.services.extraction.reconciliation import (
        RECON_LEXICAL_FAST_ACCEPT,
        RECON_SIMILARITY_THRESHOLD,
    )
    assert RECON_SIMILARITY_THRESHOLD == 0.80
    assert RECON_LEXICAL_FAST_ACCEPT == 0.95
    s = Settings(
        recon_embed_model="sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
    )
    assert s.recon_embed_model.endswith("MiniLM-L12-v2")
    import inspect

    from brain.services.extraction.reconciliation import (
        recon_similarity_from_settings,
    )
    from brain.services.extraction.service import EnrichmentService
    # the ONE builder reads RECON_EMBED_MODEL; production reaches it through
    # EnrichmentService._build_recon_similarity — no separate construction
    assert "recon_embed_model" in inspect.getsource(recon_similarity_from_settings)
    assert "recon_similarity_from_settings" in inspect.getsource(
        EnrichmentService._build_recon_similarity
    )
    # the callable: a 0.9x lexical near-identity pair must NOT fast-accept
    # (a Hebrew trap — the fake embedder gives it cosine ~0, so any score
    # the callable returns below 0.95 proves lexical did not fire)
    from brain.ai.embedder import FakeEmbedder
    from brain.services.extraction.reconciliation import embedder_similarity
    from brain.services.relations import similarity_ratio
    a, b = "טיול שנתי 2026", "טיול שנתי 2027"
    assert similarity_ratio(a.casefold(), b.casefold()) >= 0.9  # a lexical trap
    sim = embedder_similarity(FakeEmbedder())
    assert sim(a, b) < RECON_LEXICAL_FAST_ACCEPT


def test_production_similarity_factory_is_called_without_attach(db):
    """B1 (final check): EnrichmentService constructed EXACTLY as production
    does — no attach_embedder anywhere — must call the similarity factory on
    the first reconciled candidate and match a paraphrase pair through the
    EMBEDDING callable. Fails while _ensure_embedder is dead code."""
    from brain.ai.embedder import FakeEmbedder
    from brain.services.extraction.reconciliation import embedder_similarity
    from brain.services.extraction.service import EnrichmentService

    calls = {"n": 0}

    def factory():
        calls["n"] += 1
        return embedder_similarity(FakeEmbedder())

    # lexical 0.564 < 0.85; FakeEmbedder cosine 0.882 >= 0.80: only the
    # embedding callable can see these as the same activity
    _make_existing_event(db, start=_jlm(10, 16, 0), title="annual park trip")
    source = _make_source(db, text="annual trip to the park")
    service = EnrichmentService(
        ItemsRepository(db), db, extractor=None, memory=None,
        daily_cap=100, timezone="Asia/Jerusalem", similarity_factory=factory,
    )
    resolved = service._validate(
        _candidate_child(title="annual trip to the park", when_raw="10.11 16:00",
                         quote="annual trip to the park"), source, NOW
    )
    outcome = service._reconcile_child(resolved, source, {}, uuid.uuid4(), NOW)
    assert calls["n"] == 1  # the factory IS the production path
    assert outcome.verdict is Verdict.ALREADY_KNOWN


def test_embedder_load_failure_degrades_to_lexical(db):
    """B2 (final check): the recon embedder failing to LOAD must not kill the
    enrichment pass — lexical verdicts continue, ONE ops event
    enrichment/recon_embedder_unavailable is recorded, no exception escapes,
    and the failed load is not retried within the process."""
    from brain.ai.embedder import EmbedderError
    from brain.services.extraction.service import EnrichmentService

    calls = {"n": 0}

    def broken_factory():
        calls["n"] += 1
        raise EmbedderError("model download failed (offline)")

    _make_existing_event(db, start=_jlm(10, 16, 0), title=BASE)
    source = _make_source(db, text=BASE)
    service = EnrichmentService(
        ItemsRepository(db), db, extractor=None, memory=None,
        daily_cap=100, timezone="Asia/Jerusalem", similarity_factory=broken_factory,
    )
    resolved = service._validate(
        _candidate_child(title=BASE, when_raw="10.11 16:00",
                         quote=BASE), source, NOW
    )
    outcome = service._reconcile_child(resolved, source, {}, uuid.uuid4(), NOW)  # no raise
    assert outcome.verdict is Verdict.ALREADY_KNOWN  # lexical verdict produced
    assert calls["n"] == 1  # the failed load is NOT retried in this process
    row = db.query(OpsEvent).filter_by(
        component="enrichment", kind="recon_embedder_unavailable"
    ).one()
    assert "EmbedderError" in json.dumps(row.detail)


def test_recon_eval_carries_end_through_to_the_diff(tmp_path):
    """NIT (final check): eval cases may state `end` on the existing item and
    the candidate so the end branches are exercisable from the gold."""
    from brain.cli import run_recon_eval

    case = {
        "case_id": "end-diff-1",
        "now": _jlm(10, 12, 0).isoformat(),
        "existing_items": [{"id": "e1", "type": "event",
                            "title": CAL_BASE,
                            "start": _jlm(10, 16, 0).isoformat(),
                            "end": _jlm(10, 17, 0).isoformat(),
                            "applies_to": ["unknown"]}],
        "new_text_quote": "שינוי: " + CAL_BASE + " עד 19:00",
        "candidate": {"type": "event", "title": CAL_BASE,
                      "start": _jlm(10, 16, 0).isoformat(),
                      "end": _jlm(10, 19, 0).isoformat(),
                      "applies_to": ["unknown"]},
        "verdict": "update",
    }
    path = tmp_path / "cases.jsonl"
    path.write_text(json.dumps(case), encoding="utf-8")
    from brain.ai.embedder import FakeEmbedder
    report = run_recon_eval(path, tz_name="Asia/Jerusalem",
                            embedder=FakeEmbedder())
    assert report["per_verdict"]["update"]["tp"] == 1
