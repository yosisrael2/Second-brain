"""D-recon-2 PR-b: series scope + cross-kind cancellation + the ask conflict leg.

On top of D-recon-1 (#73). Three legs, nothing applies silently:

- SERIES SCOPE: an UPDATE on a series member (meta series_uid / recurring_event_id)
  with no stated scope stores the scope QUESTION on the child; an answered or
  stated 'from now on' proposes the SAME change for already-captured LATER
  members (proposed supersedes, one 'apply to all N' tap — PR-a renders); a
  stored series decision PRE-FILLS future member cards (one tap, never silent);
  'this time only' spreads nothing.
- CROSS-KIND CANCELLATION: the cancel signal alone relaxes ONLY the type gate
  of the match (applies_to, similarity, windows, margin unchanged);
  provenance.cross_kind=True on the relation. A change phrased cross-kind
  stays NEW.
- ASK CONFLICT LEG: a conflicts relation whose counterpart is not recalled
  (a proposed child never reaches search) is pulled into the context anyway,
  both sides marked conflict_with, capped at 4.
"""

from __future__ import annotations

import dataclasses
import hashlib
import uuid
from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from brain.ai.ask import FakeLLMClient
from brain.db.models import ItemRelation, MemoryItem, OpsEvent
from brain.db.session import SessionLocal, engine
from brain.repositories.items import ItemsRepository
from brain.services.ask import AskService
from brain.services.extraction.candidates import ChildCandidate, ReconHint
from brain.services.extraction.reconciliation import (
    ExistingItem,
    ReconCandidate,
    ReconSignal,
    Verdict,
    classify,
)
from brain.services.extraction.service import EnrichmentService
from brain.services.search import SearchHit
from conftest import assert_test_database

JLM = ZoneInfo("Asia/Jerusalem")
NOW = datetime(2026, 11, 2, 12, 0, tzinfo=UTC)  # Mon 2.11.2026

# Series members carry their date AND weekday in the paste one-liner so the
# ambiguity margin (>=0.05 over the runner-up) can separate the matched
# instance from the later ones (identical titles would always be ambiguous —
# #73's guard). Weekday swaps keep the trigram margin ~0.11.
TITLE = "טיול שנתי של כיתה ה לפארק השרון יום שלישי 10.11 אוסף את התלמידים בשער"
TITLE_LATER = "טיול שנתי של כיתה ה לפארק השרון יום רביעי 17.11 אוסף את התלמידים בשער"
TITLE_BEFORE = "טיול שנתי של כיתה ה לפארק השרון יום שלישי 30.10 אוסף את התלמידים בשער"
TITLE_FUTURE = "טיול שנתי של כיתה ה לפארק השרון יום חמישי 26.11 אוסף את התלמידים בשער"
# The cancel world: a reminder restating an event's title verbatim.
CLUB = "חוג כדורגל לכיתה ה במגרש בית הספר"


def _jlm(day: int, hour: int, minute: int = 0, *, month: int = 11) -> datetime:
    return datetime(2026, month, day, hour, minute, tzinfo=JLM)


class _Wipe:
    @staticmethod
    def wipe(session) -> None:
        session.execute(text("DELETE FROM item_relations"))
        session.execute(text("DELETE FROM enrichment_jobs"))
        session.execute(text("DELETE FROM item_sources"))
        session.execute(text("DELETE FROM ops_events"))
        session.execute(text("DELETE FROM memory_items"))
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


class TrigramEmbedder:
    """Hermetic recon embedder: trigrams hashed into FIXED 512 buckets, so
    similarity never depends on which strings were embedded before (a
    growing index made margins order-dependent across tests)."""

    def embed(self, value: str) -> list[float]:
        return _trigram_vec(value)


_TRIGRAM_DIM = 512


def _trigram_bucket(t: str) -> int:
    return int(hashlib.md5(t.encode()).hexdigest(), 16) % _TRIGRAM_DIM


def _trigram_vec(value: str) -> list[float]:
    t = value.casefold()
    vec = [0.0] * _TRIGRAM_DIM
    for i in range(max(0, len(t) - 2)):
        vec[_trigram_bucket(t[i : i + 3])] += 1.0
    norm = sum(v * v for v in vec) ** 0.5 or 1.0
    return [v / norm for v in vec]


def _service(session: Session, source: MemoryItem) -> EnrichmentService:
    from brain.services.extraction.reconciliation import embedder_similarity

    return EnrichmentService(
        ItemsRepository(session),
        session,
        extractor=None,
        memory=None,
        daily_cap=100,
        timezone="Asia/Jerusalem",
        similarity_factory=lambda: embedder_similarity(TrigramEmbedder()),
    )


def _make_source(
    session: Session,
    *,
    text: str,
    sender: str = "rinat@school.org.il",
    series_uid: str | None = None,
) -> MemoryItem:
    meta: dict[str, Any] = {"sender": sender, "subject": "טיול שנתי"}
    if series_uid:
        meta["series_uid"] = series_uid
    item = MemoryItem(
        raw_text=text,
        source="email",
        meta=meta,
        occurred_at=datetime(2026, 11, 2, 8, 0, tzinfo=UTC),
    )
    session.add(item)
    session.commit()
    return item


def _member(
    session: Session,
    *,
    start: datetime,
    title: str = TITLE,
    series_uid: str | None = "S1",
    type_: str = "event",
) -> MemoryItem:
    item = MemoryItem(
        raw_text=title,
        source="email",
        type=type_,
        occurred_at=start,
        applies_to=["unknown"],
        status="filed",
        meta={"series_uid": "S1"} if series_uid else {},
    )
    session.add(item)
    session.commit()
    return item


def _candidate_child(
    *,
    kind: str = "event",
    title: str = TITLE,
    when_raw: str = "10.11 18:00",
    quote: str = "הטיול יוצא ב-18:00",
    recon: ReconHint | None = None,
) -> ChildCandidate:
    return ChildCandidate(
        kind=kind,
        title=title,
        quote=quote,
        when_raw=when_raw,
        location=None,
        end=None,
        recon=recon,
    )


def _run_update(
    db: Session,
    source: MemoryItem,
    *,
    hint: ReconHint | None = None,
    title: str = TITLE,
    when_raw: str = "10.11 18:00",
    quote: str = "הטיול יוצא ב-18:00",
):
    service = _service(db, source)
    resolved = service._validate(
        _candidate_child(title=title, when_raw=when_raw, quote=quote, recon=hint),
        source,
        NOW,
    )
    assert resolved is not None, "the candidate must validate"
    outcome = service._reconcile_child(resolved, source, {}, uuid.uuid4(), NOW)
    return service, outcome


# --- series scope -------------------------------------------------------------


def test_series_update_without_stated_scope_stores_the_question(db):
    """UPDATE on a series member, no scope in the text: the child carries the
    scope QUESTION (the card asks; nothing spreads until an answer)."""
    source = _make_source(db, text="שינוי: הטיול יוצא ב-18:00", series_uid="S1")
    _member(db, start=_jlm(10, 16, 0))  # the matched member (earlier same day)
    _member(db, start=_jlm(17, 16, 0), title=TITLE_LATER)  # a LATER captured member
    service, outcome = _run_update(db, source, hint=ReconHint(change=True))
    assert outcome is not None and outcome.verdict is Verdict.UPDATE
    child = db.query(MemoryItem).filter(MemoryItem.child_state == "proposed").one()
    scope = child.extraction_meta["recon_series_scope"]
    assert scope["series_id"] == "S1"
    assert scope["scope"] == "question"
    assert scope.get("decided_at") is None
    # and nothing spread: only the ONE update relation exists
    rels = db.query(ItemRelation).filter_by(kind="supersedes").all()
    assert len(rels) == 1


def test_series_stated_from_now_on_proposes_for_later_members(db):
    """A stated 'from now on' skips the question and proposes the SAME change
    for every already-captured LATER member (proposed, one tap confirms)."""
    source = _make_source(db, text="שינוי: הטיול יוצא ב-18:00 מעכשיו לכל המפגשים", series_uid="S1")
    earlier = _member(db, start=_jlm(10, 16, 0))
    later = _member(db, start=_jlm(17, 16, 0), title=TITLE_LATER)
    # BEFORE today: outside every window
    before = _member(db, start=_jlm(30, 16, 0, month=10), title=TITLE_BEFORE)
    service, outcome = _run_update(
        db,
        source,
        hint=ReconHint(change=True),
        quote="הטיול יוצא ב-18:00 מעכשיו לכל המפגשים",
    )
    assert outcome is not None and outcome.verdict is Verdict.UPDATE
    child = db.query(MemoryItem).filter(MemoryItem.child_state == "proposed").one()
    scope = child.extraction_meta["recon_series_scope"]
    assert scope["scope"] == "series"  # stated, not a question
    assert scope["decided_at"] is not None
    rels = db.query(ItemRelation).filter_by(kind="supersedes", from_id=child.id).all()
    targets = {str(rel.to_id) for rel in rels}
    assert str(later.id) in targets  # the later member got the proposal
    assert str(earlier.id) in targets  # the matched member: the original update
    assert str(before.id) not in targets  # earlier members are never touched
    for rel in rels:
        if str(rel.to_id) == str(later.id):
            assert (rel.provenance or {}).get("state") == "proposed"  # never silent


def test_series_stated_this_time_only_does_not_spread(db):
    source = _make_source(db, text="שינוי: הטיול יוצא ב-18:00 רק הפעם", series_uid="S1")
    _member(db, start=_jlm(10, 16, 0))
    later = _member(db, start=_jlm(17, 16, 0), title=TITLE_LATER)
    service, outcome = _run_update(
        db, source, hint=ReconHint(change=True), quote="הטיול יוצא ב-18:00 רק הפעם"
    )
    assert outcome is not None and outcome.verdict is Verdict.UPDATE
    child = db.query(MemoryItem).filter(MemoryItem.child_state == "proposed").one()
    assert child.extraction_meta["recon_series_scope"]["scope"] == "this_time"
    rels = db.query(ItemRelation).filter_by(kind="supersedes").all()
    assert len(rels) == 1  # only the original update; the later member untouched
    db.refresh(later)
    assert later.superseded_by is None


def test_series_answer_from_now_on_proposes_for_later_members(db):
    """The card's scope answer: 'from now on' -> the same change is PROPOSED
    for the already-captured later members (one 'apply to all N' tap)."""
    source = _make_source(db, text="שינוי: הטיול יוצא ב-18:00", series_uid="S1")
    _member(db, start=_jlm(10, 16, 0))
    later = _member(db, start=_jlm(17, 16, 0), title=TITLE_LATER)
    service, outcome = _run_update(db, source, hint=ReconHint(change=True))
    assert outcome is not None and outcome.verdict is Verdict.UPDATE
    child = db.query(MemoryItem).filter(MemoryItem.child_state == "proposed").one()
    rel = db.query(ItemRelation).filter_by(kind="supersedes").one()
    created = service.answer_series_scope(rel.id, "from_now_on")
    new_rels = [
        r
        for r in db.query(ItemRelation).filter_by(kind="supersedes").all()
        if str(r.to_id) == str(later.id)
    ]
    assert len(new_rels) == 1
    assert (new_rels[0].provenance or {}).get("state") == "proposed"
    assert new_rels[0].id in {r.id for r in created}
    scope = db.refresh(child) or child.extraction_meta["recon_series_scope"]
    assert scope["scope"] == "series"
    assert scope["decided_at"] is not None


def test_series_answer_this_time_spreads_nothing(db):
    source = _make_source(db, text="שינוי: הטיול יוצא ב-18:00", series_uid="S1")
    _member(db, start=_jlm(10, 16, 0))
    later = _member(db, start=_jlm(17, 16, 0), title=TITLE_LATER)
    service, outcome = _run_update(db, source, hint=ReconHint(change=True))
    assert outcome is not None
    rel = db.query(ItemRelation).filter_by(kind="supersedes").one()
    created = service.answer_series_scope(rel.id, "this_time")
    assert created == []  # nothing to apply beyond the one instance
    db.refresh(later)
    assert later.superseded_by is None


def test_series_scope_prefills_future_member_cards(db):
    """A stored series decision PRE-FILLS a future member's card (one tap,
    never silent): the new child inherits the decision; its relation stays
    proposed."""
    source1 = _make_source(db, text="שינוי: הטיול יוצא ב-18:00", series_uid="S1")
    _member(db, start=_jlm(10, 16, 0))
    _run_update(db, source1, hint=ReconHint(change=True))
    child1 = db.query(MemoryItem).filter(MemoryItem.child_state == "proposed").one()
    rel1 = db.query(ItemRelation).filter_by(kind="supersedes").one()
    service1 = _service(db, source1)
    service1.answer_series_scope(rel1.id, "from_now_on")
    # the user CONFIRMS card 1 (the tap that keeps the child): only a KEPT
    # child's decision is evidence for pre-filling (follow-up major 4 — a
    # proposed sibling's answer is a proposal, not a confirmation)
    child1.child_state = "kept"
    db.commit()

    # a FUTURE member of the same series arrives as its own correction
    source2 = _make_source(db, text="שינוי: הטיול יוצא ב-19:00", series_uid="S1")
    future_member = _member(db, start=_jlm(24, 16, 0), title=TITLE_FUTURE)
    service2, outcome2 = _run_update(
        db,
        source2,
        hint=ReconHint(change=True),
        title=TITLE_FUTURE,
        when_raw="24.11 19:00",
        quote="הטיול יוצא ב-19:00",
    )
    assert outcome2 is not None and outcome2.verdict is Verdict.UPDATE
    children = (
        db.query(MemoryItem)
        .filter(MemoryItem.child_state == "proposed")
        .filter(MemoryItem.id != child1.id)
        .all()
    )
    assert len(children) == 1
    scope = children[0].extraction_meta["recon_series_scope"]
    assert scope["series_id"] == "S1"
    assert scope["scope"] == "series"
    assert scope.get("pre_confirmed") is True  # pre-marked 'from now on'
    rel2 = db.query(ItemRelation).filter_by(kind="supersedes", from_id=children[0].id).one()
    assert rel2.to_id == future_member.id
    assert (rel2.provenance or {}).get("state") == "proposed"  # one tap, never silent


def test_non_series_update_stores_no_scope_key(db):
    source = _make_source(db, text="שינוי: הטיול יוצא ב-18:00")  # no series uid
    _member(db, start=_jlm(10, 16, 0), series_uid=None)
    service, outcome = _run_update(db, source, hint=ReconHint(change=True))
    assert outcome is not None and outcome.verdict is Verdict.UPDATE
    child = db.query(MemoryItem).filter(MemoryItem.child_state == "proposed").one()
    assert "recon_series_scope" not in (child.extraction_meta or {})


# --- cross-kind cancellation ----------------------------------------------------


def test_cross_kind_cancellation_matches_the_event(db):
    """A REMINDER stating an event is cancelled matches the EVENT (only the
    type gate relaxes); the typed flow alone would have said NEW."""
    source = _make_source(db, text="תזכיר לי: החוג בוטל", sender="other@school.org.il")
    _member(db, start=_jlm(10, 16, 0), title=CLUB)  # the event
    service = _service(db, source)
    resolved = service._validate(
        _candidate_child(
            kind="reminder",
            title=CLUB,
            quote="החוג בוטל",
            when_raw=None,
            recon=ReconHint(cancel=True),
        ),
        source,
        NOW,
    )
    assert resolved is not None
    # the candidate is a reminder; the existing item is an event: the TYPED
    # window can never match — the pre-pass must.
    outcome = service._reconcile_child(resolved, source, {}, uuid.uuid4(), NOW)
    assert outcome is not None and outcome.verdict is Verdict.CANCELLATION


def test_cross_kind_cancellation_relation_carries_cross_kind(db):
    source = _make_source(db, text="תזכיר לי: החוג בוטל", sender="other@school.org.il")
    event = _member(db, start=_jlm(10, 16, 0), title=CLUB)
    service = _service(db, source)
    resolved = service._validate(
        _candidate_child(
            kind="reminder",
            title=CLUB,
            quote="החוג בוטל",
            when_raw=None,
            recon=ReconHint(cancel=True),
        ),
        source,
        NOW,
    )
    outcome = service._reconcile_child(resolved, source, {}, uuid.uuid4(), NOW)
    assert outcome is not None and outcome.matched_id == event.id
    rel = db.query(ItemRelation).filter_by(kind="cancels").one()
    assert (rel.provenance or {}).get("cross_kind") is True


def test_cross_kind_still_respects_the_applies_to_gate(db):
    """R5: ONLY the type gate relaxes — a different known audience never
    matches, even with a cancel signal."""
    source = _make_source(db, text="תזכיר לי: החוג בוטל", sender="other@school.org.il")
    _member(db, start=_jlm(10, 16, 0), title=CLUB)
    service = _service(db, source)
    resolved = service._validate(
        _candidate_child(
            kind="reminder",
            title=CLUB,
            quote="החוג בוטל",
            when_raw=None,
            recon=ReconHint(cancel=True),
        ),
        source,
        NOW,
    )
    # force a DIFFERENT known audience on the candidate (the world resolves
    # 'unknown' == 'unknown'; a known mismatch must block the match)
    resolved = dataclasses.replace(resolved, applies_to=["Noa"])
    outcome = service._reconcile_child(resolved, source, {}, uuid.uuid4(), NOW)
    assert outcome is None  # NEW: the gate held


def test_cross_kind_change_stays_new(db):
    """Only the CANCEL signal relaxes the gate: an update phrased as a
    reminder is ambiguous speech, not evidence (decision: NEW)."""
    source = _make_source(db, text="תזכיר: החוג עודכן ל-18:00", sender="other@school.org.il")
    _member(db, start=_jlm(10, 16, 0), title=CLUB)
    service = _service(db, source)
    resolved = service._validate(
        _candidate_child(
            kind="reminder",
            title=CLUB,
            quote="החוג עודכן ל-18:00",
            when_raw="10.11 18:00",
            recon=ReconHint(change=True),
        ),
        source,
        NOW,
    )
    outcome = service._reconcile_child(resolved, source, {}, uuid.uuid4(), NOW)
    assert outcome is None  # NEW


def test_same_kind_cancellation_is_not_cross_kind(db):
    source = _make_source(db, text="הטיול בוטל", sender="other@school.org.il")
    _member(db, start=_jlm(10, 16, 0), title=CLUB)
    service = _service(db, source)
    resolved = service._validate(
        _candidate_child(
            kind="event",
            title=CLUB,
            quote="הטיול בוטל",
            when_raw=None,
            recon=ReconHint(cancel=True),
        ),
        source,
        NOW,
    )
    outcome = service._reconcile_child(resolved, source, {}, uuid.uuid4(), NOW)
    assert outcome is not None and outcome.verdict is Verdict.CANCELLATION
    rel = db.query(ItemRelation).filter_by(kind="cancels").one()
    assert (rel.provenance or {}).get("cross_kind") is not True


def test_pure_classify_relax_type_only_for_cancel():
    """The pure core: relax_type lets a cross-kind CANCELLATION through but
    never an UPDATE/CONFLICT."""
    existing = ExistingItem(
        id="e1",
        type="event",
        title=CLUB,
        start=_jlm(10, 16, 0),
        applies_to=("Rona",),
    )
    cancel_candidate = ReconCandidate(
        kind="reminder",
        title=CLUB,
        applies_to=("Rona",),
    )

    result = classify(
        cancel_candidate,
        [existing],
        now=NOW,
        tz_name="Asia/Jerusalem",
        signal=ReconSignal(cancel=True),
        relax_type=True,
    )
    assert result.verdict is Verdict.CANCELLATION
    assert result.cross_kind is True
    update_candidate = ReconCandidate(
        kind="reminder",
        title=CLUB,
        start=_jlm(10, 18, 0),
        applies_to=("Rona",),
    )
    result2 = classify(
        update_candidate,
        [existing],
        now=NOW,
        tz_name="Asia/Jerusalem",
        signal=ReconSignal(change=True),
        relax_type=True,
    )
    assert result2.verdict is Verdict.NEW  # the relaxed pass only cancels


# --- ask conflict leg -----------------------------------------------------------


class RecordingSearch:
    def __init__(self, hits: list[SearchHit]) -> None:
        self._hits = hits

    def search(self, query, *, limit, offset=0, extra_items=None):
        return self._hits[:limit]

    def by_occurrence(self, start, end, *, limit):
        return []


def _wipe_ask_world() -> None:
    assert_test_database(str(engine.url))
    with SessionLocal() as session:
        _Wipe.wipe(session)


def _ask_world():
    """E (kept, searchable) + a PROPOSED conflict child linked to it."""
    _wipe_ask_world()
    session = SessionLocal()
    existing = MemoryItem(
        raw_text="טיול שנתי יוצא ב-16:00",
        source="email",
        type="event",
        occurred_at=_jlm(10, 16, 0),
        status="filed",
    )
    session.add(existing)
    session.commit()
    child = MemoryItem(
        raw_text="טיול שנתי יוצא ב-18:00",
        source="email",
        type="event",
        occurred_at=_jlm(10, 18, 0),
        child_state="proposed",
    )
    session.add(child)
    session.commit()
    session.add(
        ItemRelation(
            from_id=child.id,
            to_id=existing.id,
            kind="conflicts",
            provenance={"how": "recon", "fields": {"time": ["16:00", "18:00"]}},
        )
    )
    session.commit()
    return session, existing, child


def test_ask_surfaces_conflict_counterpart(db):
    """Only the existing item is searchable; the proposed conflict child is
    pulled into the context anyway, both sides marked conflict_with."""
    session, existing, child = _ask_world()
    llm = FakeLLMClient(
        reply=lambda prompt: (
            "Conflict: " + " ".join(line for line in prompt.splitlines() if line.startswith("- ["))
        )
    )
    ask = AskService(
        RecordingSearch([SearchHit(item=existing, score=0.9)]),
        llm,
        items_repo=ItemsRepository(session),
    )
    result = ask.ask("מתי הטיול?", now=NOW)
    ids = {source.item.id for source in result.sources}
    assert existing.id in ids
    assert child.id in ids  # the proposed counterpart joined the context
    prompt = llm.prompts[-1]
    assert f"[{existing.id}]" in prompt and f"[{child.id}]" in prompt
    assert "conflicts with" in prompt  # the conflict marker renders
    session.close()


def test_ask_conflict_leg_caps_at_four_counterparts(db):
    session, existing, _child = _ask_world()
    for i in range(6):
        alt = MemoryItem(
            raw_text=f"טיול שנתי גרסה {i}",
            source="email",
            type="event",
            child_state="proposed",
        )
        session.add(alt)
        session.commit()
        session.add(
            ItemRelation(
                from_id=alt.id, to_id=existing.id, kind="conflicts", provenance={"how": "recon"}
            )
        )
        session.commit()
    llm = FakeLLMClient()
    ask = AskService(
        RecordingSearch([SearchHit(item=existing, score=0.9)]),
        llm,
        items_repo=ItemsRepository(session),
    )
    result = ask.ask("מתי הטיול?", now=NOW)
    counterpart_ids = [s.item.id for s in result.sources if s.item.id != existing.id]
    assert len(counterpart_ids) == 4  # capped
    session.close()


def test_ask_without_conflicts_is_byte_identical(db):
    session, existing, _child = _ask_world()
    session.execute(text("DELETE FROM item_relations"))
    session.commit()
    llm = FakeLLMClient()
    ask = AskService(
        RecordingSearch([SearchHit(item=existing, score=0.9)]),
        llm,
        items_repo=ItemsRepository(session),
    )
    result = ask.ask("מתי הטיול?", now=NOW)
    prompt = llm.prompts[-1]
    assert "conflicts with" not in prompt  # no leg, no markers
    assert [s.item.id for s in result.sources] == [existing.id]
    session.close()


def test_cross_kind_cancellation_of_a_proposed_target_stays_gated(db):
    """The F3 gate (decision 6) applies to the relaxed verdict too: a cancel
    aimed at a still-PROPOSED child of ANOTHER source is NEW + an ops event —
    the user never approved that proposal yet."""
    source = _make_source(db, text="תזכיר לי: החוג בוטל", sender="other@school.org.il")
    proposed_event = _member(db, start=_jlm(10, 16, 0), title=CLUB)
    db.refresh(proposed_event)
    proposed_event.child_state = "proposed"
    db.commit()
    service = _service(db, source)
    resolved = service._validate(
        _candidate_child(
            kind="reminder",
            title=CLUB,
            quote="החוג בוטל",
            when_raw=None,
            recon=ReconHint(cancel=True),
        ),
        source,
        NOW,
    )
    outcome = service._reconcile_child(resolved, source, {}, uuid.uuid4(), NOW)
    assert outcome is None  # gated: NEW, nothing written
    rels = db.query(ItemRelation).filter_by(kind="cancels").all()
    assert rels == []
    kinds = [row[0] for row in db.execute(text("SELECT kind FROM ops_events")).all()]
    assert "recon_target_unapproved" in kinds


def test_series_scope_phrase_outside_the_quotes_leaves_question(db):
    """The quotes-only discipline, proved directly: the SOURCE carries a
    series phrase ('לכל המפגשים') but the candidate's grounded quote does
    not — the decision stays 'question' and nothing spreads. One email
    mentioning both a one-off change and a policy must not guess
    (same rule as signal_for: only the candidate's own words count)."""
    source = _make_source(
        db,
        text="שינוי: הטיול יוצא ב-18:00\n\nהודעה כללית: השעה החדשה תחול לכל המפגשים",
        series_uid="S1",
    )
    _member(db, start=_jlm(10, 16, 0))
    later = _member(db, start=_jlm(17, 16, 0), title=TITLE_LATER)
    service, outcome = _run_update(
        db, source, hint=ReconHint(change=True), quote="הטיול יוצא ב-18:00"
    )
    assert outcome is not None and outcome.verdict is Verdict.UPDATE
    child = db.query(MemoryItem).filter(MemoryItem.child_state == "proposed").one()
    scope = child.extraction_meta["recon_series_scope"]
    assert scope["scope"] == "question"  # the phrase was outside the quote
    assert scope["decided_at"] is None
    rels = db.query(ItemRelation).filter_by(kind="supersedes").all()
    assert len(rels) == 1  # only the original update; the later member untouched
    db.refresh(later)
    assert later.superseded_by is None


# --- post-merge follow-ups (review majors 1-5 + minor) --------------------------


def test_conflict_counterparts_exclude_dead_counterparts(db):
    """MAJOR 1: a DROPPED, SUPERSEDED or CANCELLED counterpart is never a
    conflict for the ask leg — only live counterparts are."""
    existing = MemoryItem(
        raw_text="טיול שנתי יוצא ב-16:00",
        source="email",
        type="event",
        occurred_at=_jlm(10, 16, 0),
        status="filed",
    )
    db.add(existing)
    db.commit()
    states = {
        "proposed": "proposed",
        "dropped": "dropped",
        "superseded": "kept",  # superseded BY someone else
        "cancelled": "kept",  # cancelled stamp
    }
    made: dict[str, MemoryItem] = {}
    for name, state in states.items():
        other = MemoryItem(
            raw_text=f"טיול שנתי גרסה {name}",
            source="email",
            type="event",
            occurred_at=_jlm(10, 17 + list(states).index(name), 0),
            child_state=state,
            extraction_meta=(
                {"cancelled": {"by": "x", "relation_id": "r"}} if name == "cancelled" else {}
            ),
        )
        if name == "superseded":
            other.superseded_by = existing.id  # a real row: the FK needs one
        db.add(other)
        db.commit()
        db.add(
            ItemRelation(
                from_id=other.id, to_id=existing.id, kind="conflicts",
                provenance={"how": "recon"},
            )
        )
        db.commit()
        made[name] = other
    repo = ItemsRepository(db)
    pairs = repo.conflict_counterparts([existing.id])
    counterpart_ids = {str(other.id) for _rel, other in pairs}
    assert counterpart_ids == {str(made["proposed"].id)}


def test_ask_conflict_leg_failure_degrades_to_plain_answer(db):
    """MAJOR 2: the conflict leg runs INSIDE the fail-safe — a repo raising
    from conflict_counterparts must not crash the ask; one ops event records
    the failure and the answer proceeds without the leg."""
    session, existing, _child = _ask_world()

    class ExplodingRepo(ItemsRepository):
        def conflict_counterparts(self, item_ids, *, limit=4):
            raise RuntimeError("boom from the leg")

    class RealStubLLM:
        """NOT synthetic: its ask events are evidence (the failure event too)."""

        synthetic = False

        def complete(self, prompt: str):
            from tests.test_ask_service import cite_every_source

            from brain.ai.ask import Completion

            return Completion(cite_every_source(prompt), model="real-stub")

        def close(self) -> None:
            return None

    ask = AskService(
        RecordingSearch([SearchHit(item=existing, score=0.9)]),
        RealStubLLM(),
        items_repo=ExplodingRepo(session),
    )
    result = ask.ask("מתי הטיול?", now=NOW)  # must NOT raise
    assert result.answer
    assert [s.item.id for s in result.sources] == [existing.id]
    events = db.query(OpsEvent).filter_by(
        component="ask", kind="conflict_leg_failed"
    ).all()
    assert len(events) == 1
    session.close()


def test_conflict_map_only_marks_ids_in_the_final_context(db):
    """MAJOR 3: a counterpart dropped by the text dedupe must not keep a
    dangling "conflicts with [id]" marker in the prompt — the map is built
    against the FINAL context ids."""
    session, existing, child = _ask_world()
    child.raw_text = existing.raw_text  # the dedupe will drop the counterpart
    db.commit()
    llm = FakeLLMClient(
        reply=lambda prompt: (
            "Ctx: " + " ".join(line for line in prompt.splitlines() if line.startswith("- ["))
        )
    )
    ask = AskService(
        RecordingSearch([SearchHit(item=existing, score=0.9)]),
        llm,
        items_repo=ItemsRepository(session),
    )
    result = ask.ask("מתי הטיול?", now=NOW)
    assert [s.item.id for s in result.sources] == [existing.id]  # child deduped away
    prompt = llm.prompts[-1]
    assert f"[{child.id}]" not in prompt  # no marker names an absent source
    session.close()


def test_series_scope_prefill_ignores_a_proposed_sibling(db):
    """MAJOR 4: a prior decision on a STILL-PROPOSED sibling is not a user
    confirmation — no pre_confirmed, the future member's card stays a
    QUESTION. Only a KEPT child's decision pre-fills."""
    source1 = _make_source(db, text="שינוי: הטיול יוצא ב-18:00", series_uid="S1")
    _member(db, start=_jlm(10, 16, 0))
    _run_update(db, source1, hint=ReconHint(change=True))
    child1 = db.query(MemoryItem).filter(MemoryItem.child_state == "proposed").one()
    rel1 = db.query(ItemRelation).filter_by(kind="supersedes").one()
    service1 = _service(db, source1)
    service1.answer_series_scope(rel1.id, "from_now_on")
    db.refresh(child1)
    assert child1.extraction_meta["recon_series_scope"]["decided_at"] is not None
    # child1 is STILL proposed: the user has not confirmed anything
    assert child1.child_state == "proposed"

    source2 = _make_source(db, text="שינוי: הטיול יוצא ב-19:00", series_uid="S1")
    future_member = _member(db, start=_jlm(24, 16, 0), title=TITLE_FUTURE)
    _service(db, source2)
    service2, outcome2 = _run_update(
        db, source2, hint=ReconHint(change=True),
        title=TITLE_FUTURE, when_raw="24.11 19:00",
        quote="הטיול יוצא ב-19:00",
    )
    assert outcome2 is not None and outcome2.verdict is Verdict.UPDATE
    child2 = (
        db.query(MemoryItem)
        .filter(MemoryItem.child_state == "proposed")
        .filter(MemoryItem.id != child1.id)
        .one()
    )
    scope2 = child2.extraction_meta.get("recon_series_scope") or {}
    assert scope2.get("scope") == "question"  # a question, NOT pre_confirmed
    assert scope2.get("pre_confirmed") is not True
    rels2 = [
        r for r in db.query(ItemRelation).filter_by(kind="supersedes").all()
        if str(r.to_id) == str(future_member.id)
    ]
    # exactly ONE relation reaches the future member: the original UPDATE
    # from child2 itself — no spread, and nothing from the unconfirmed child1
    assert len(rels2) == 1 and str(rels2[0].from_id) == str(child2.id)
    assert (rels2[0].provenance or {}).get("state") == "proposed"
    assert (
        db.query(ItemRelation)
        .filter_by(kind="supersedes", from_id=child1.id)
        .filter(ItemRelation.id != rel1.id)
        .all()
    ) == []  # child1 spread nothing new either


def test_series_spread_skips_dropped_members(db):
    """MAJOR 5: dropped series members get NO proposed supersedes relation
    (the docstring promises it; the filter now enforces it)."""
    source = _make_source(db, text="שינוי: הטיול יוצא ב-18:00 מעכשיו לכל המפגשים", series_uid="S1")
    _member(db, start=_jlm(10, 16, 0))
    later = _member(db, start=_jlm(17, 16, 0), title=TITLE_LATER)
    later.child_state = "dropped"
    db.commit()
    service, outcome = _run_update(
        db, source, hint=ReconHint(change=True),
        quote="הטיול יוצא ב-18:00 מעכשיו לכל המפגשים",
    )
    assert outcome is not None and outcome.verdict is Verdict.UPDATE
    rels = db.query(ItemRelation).filter_by(kind="supersedes").all()
    assert str(later.id) not in {str(r.to_id) for r in rels}


def test_answer_series_scope_with_a_missing_child_is_a_value_error(db):
    """MINOR: a supersedes relation whose child row is gone answers with a
    clean ValueError, never an AttributeError on None."""
    source = _make_source(db, text="שינוי: הטיול יוצא ב-18:00", series_uid="S1")
    _member(db, start=_jlm(10, 16, 0))
    service, outcome = _run_update(db, source, hint=ReconHint(change=True))
    assert outcome is not None
    rel = db.query(ItemRelation).filter_by(kind="supersedes").one()
    db.execute(text("DELETE FROM memory_items WHERE child_state = 'proposed'"))
    db.commit()
    with pytest.raises(ValueError):
        service.answer_series_scope(rel.id, "from_now_on")


def test_ask_conflict_leg_survives_a_poisoned_query(db):
    """MAJOR 2, round 2 (the #117 trap): a REAL db error inside the leg
    aborts the ask's whole transaction — the catch alone only moves the
    crash one step later, and the abort silently discards the session's
    other uncommitted work. The leg's DB work must run in a SAVEPOINT so
    only it rolls back: the ask answers, one ops event is recorded, and the
    outer transaction's own work survives."""
    existing = MemoryItem(
        raw_text="טיול שנתי יוצא ב-16:00",
        source="email",
        type="event",
        occurred_at=_jlm(10, 16, 0),
        status="filed",
    )
    db.add(existing)
    db.commit()

    class PoisonedRepo(ItemsRepository):
        def conflict_counterparts(self, item_ids, *, limit=4):
            db.execute(text("SELECT * FROM no_such_table_reconfix"))
            return []

    class RealStubLLM:
        """NOT synthetic: the leg-failure event must be recorded."""

        synthetic = False

        def complete(self, prompt: str):
            from tests.test_ask_service import cite_every_source

            from brain.ai.ask import Completion

            return Completion(cite_every_source(prompt), model="real-stub")

        def close(self) -> None:
            return None

    ask = AskService(
        RecordingSearch([SearchHit(item=existing, score=0.9)]),
        RealStubLLM(),
        items_repo=PoisonedRepo(db),
    )
    # UNCOMMITTED outer-transaction work that must survive the failed leg:
    # without the savepoint the poisoned statement aborts the WHOLE
    # transaction and the rollback silently discards it
    outer_write = MemoryItem(
        raw_text="uncommitted outer work", source="email", type="fact"
    )
    db.add(outer_write)
    db.flush()
    result = ask.ask("מתי הטיול?", now=NOW)  # must NOT raise
    assert result.answer
    assert [s.item.id for s in result.sources] == [existing.id]
    events = db.query(OpsEvent).filter_by(
        component="ask", kind="conflict_leg_failed"
    ).all()
    assert len(events) == 1
    # the outer transaction's own work SURVIVES the failed leg (the savepoint
    # scopes the rollback to the leg; without it the abort eats this row)
    assert db.get(MemoryItem, outer_write.id) is not None
    db.commit()
    assert db.get(MemoryItem, outer_write.id) is not None
