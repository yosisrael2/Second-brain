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
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from brain.db.models import ItemRelation, ItemSource, MemoryItem
from brain.db.session import SessionLocal, engine
from brain.repositories.items import ItemsRepository
from brain.services.extraction.candidates import ChildCandidate
from brain.services.extraction.reconciliation import (
    ExistingItem,
    ReconCandidate,
    ReconSignal,
    Verdict,
    classify,
    detect_signal,
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
    result = classify(
        _candidate(title=BASE, start=_jlm(10, 16, 30)),
        [_existing(start=_jlm(10, 16, 0))],
        similarity=_similarity, now=NOW, tz_name="Asia/Jerusalem",
    )
    assert result.verdict is Verdict.ALREADY_KNOWN
    assert result.matched_id == "e1"


def test_plus_minus_1h_adds_info_is_more_detail():
    detail_title = BASE + " להביא כובע"
    assert _similarity(detail_title, BASE) >= 0.85  # the key still matches
    result = classify(
        _candidate(title=detail_title, start=_jlm(10, 16, 0)),
        [_existing(title=BASE, start=_jlm(10, 16, 0))],
        similarity=_similarity, now=NOW, tz_name="Asia/Jerusalem",
    )
    assert result.verdict is Verdict.MORE_DETAIL


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
        # MORE DETAIL: same instant, extra content
        (
            _candidate(title=BASE + " להביא כובע", start=_jlm(10, 16, 0)),
            ReconSignal(), Verdict.MORE_DETAIL,
        ),
        # ALREADY KNOWN: ±1h, nothing new
        (_candidate(start=_jlm(10, 16, 30)), ReconSignal(), Verdict.ALREADY_KNOWN),
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


def test_detect_signal_phrases():
    assert detect_signal("תיקון טעות: האספה ב-18:00").change is True
    assert detect_signal("שינוי בשעה").change is True
    assert detect_signal("update: the trip starts later").change is True
    assert detect_signal("הטיול בוטל").cancel is True
    assert detect_signal("הטיול מבוטל").cancel is True
    assert detect_signal("cancelled due to rain").cancel is True
    assert detect_signal("בוטל והועבר ל-19:00").cancel is True
    assert detect_signal("נא לשלוח חולצה לבנה").change is False
    assert detect_signal("הטיול מחר").cancel is False


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


def _make_source(session: Session, *, text: str, sender: str = "rinat@school") -> MemoryItem:
    item = MemoryItem(
        raw_text=text,
        source="email",
        meta={"sender": sender, "subject": "טיול שנתי"},
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
        child_state="kept",
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
) -> ChildCandidate:
    return ChildCandidate(
        kind="event",
        title=title,
        quote=quote,
        when_raw=when_raw,
    )


def _service(session, source: MemoryItem, *, tz_name: str = "Asia/Jerusalem") -> EnrichmentService:
    repo = ItemsRepository(session)
    return EnrichmentService(repo, session, extractor=None, memory=None,
                             daily_cap=100, timezone=tz_name)


def test_update_verdict_proposes_change_never_applies(db):
    """Cross-thread: the OLD item stays intact; a proposed supersedes lands."""
    source = _make_source(db, text="שינוי: הטיול יוצא ב-18:00", sender="other@school")
    existing = _make_existing_event(db, start=_jlm(10, 16, 0))
    service = _service(db, source)
    resolved = service._validate(_candidate_child(), source, NOW)
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
    resolved = service._validate(_candidate_child(), source, NOW)
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
    resolved = service._validate(_candidate_child(), source, NOW)
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
    other = _make_source(db, text="הטיול יוצא ב-18:00 מהצ'אט", sender="chat")
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
