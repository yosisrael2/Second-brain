"""The recon second-stage judge (approved plan; D-recon-1 follow-up (a)).

Band [RECON_JUDGE_FLOOR, RECON_SIMILARITY_THRESHOLD): type + applies_to +
window + exclusion already passed and the ambiguity margin is clear — a
typed yes/no decides SAME REAL-WORLD THING or not. Jev first, then exactly
one strict LLM call, then fall CLOSED (never a guessed match). Cap 20 per
pass, in-process cache, RECON_JUDGE_ENABLED default FALSE.
"""

from datetime import UTC, datetime
from typing import Any
from uuid import uuid4
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy.orm import Session

from brain.db.models import MemoryItem, OpsEvent
from brain.db.session import SessionLocal, engine
from brain.repositories.items import ItemsRepository
from brain.services.extraction.reconciliation import (
    RECON_JUDGE_FLOOR,
    ExistingItem,
    ReconCandidate,
    ReconSignal,
    classify,
)
from brain.services.extraction.service import EnrichmentService
from conftest import assert_test_database

JLM = ZoneInfo("Asia/Jerusalem")


class _Wipe:
    @staticmethod
    def wipe(session: Session) -> None:
        from sqlalchemy import text as _text

        for table in (
            "item_sources", "item_relations", "memory_facts",
            "entity_aliases", "entities", "memory_items",
        ):
            session.execute(_text(f"DELETE FROM {table}"))
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
NOW = datetime(2026, 11, 2, 12, 0, tzinfo=UTC)

TITLE_A = (
    "טיול שנתי של כיתה ה לפארק השרון ביום שלישי 10.11 "
    "עם כל ההורים והילדים והחברים של כיתה ה והמדריכים וגם הסבים"
)


def _pair(similarity: float):
    """A similarity stub pinned to one value (drives the band logic)."""
    return lambda a, b: similarity


def _band_pair(candidate_title: str = TITLE_A, existing_title: str | None = None):
    existing_title = existing_title or (TITLE_A + " ועוד תוספות רבות מאוד בכותרת")
    return (
        ReconCandidate(kind="event", title=candidate_title,
                       start=datetime(2026, 11, 10, 16, 0, tzinfo=JLM),
                       applies_to=("unknown",)),
        ExistingItem(id="e1", type="event", title=existing_title,
                     start=datetime(2026, 11, 10, 16, 0, tzinfo=JLM),
                     applies_to=("unknown",)),
    )


class FakeJudge:
    """The judge double: records calls, answers from a table."""

    def __init__(self, answers: dict | None = None, default: bool | None = True):
        self.calls: list[tuple[str, str]] = []
        self.answers = answers or {}
        self.default = default

    def __call__(self, candidate, existing) -> bool | None:
        self.calls.append((candidate.title[:20], existing.title[:20]))
        return self.answers.get((candidate.title, existing.title), self.default)


def test_judge_disabled_by_default_and_off_matches_today():
    """RECON_JUDGE_ENABLED defaults false; with no judge the 0.60-0.80 band
    behaves exactly as today (NEW, no call — nothing to call)."""
    from brain.config import Settings
    s = Settings()
    assert s.recon_judge_enabled is False
    assert s.recon_judge_floor == 0.60
    assert s.recon_judge_confidence == 0.9
    assert s.recon_judge_max_calls == 20

    cand, existing = _band_pair()
    result = classify(
        cand, [existing], similarity=_pair(0.70), now=NOW,
        tz_name="Asia/Jerusalem", signal=None, judge=None,
    )
    assert result.verdict.value == "new"


def test_band_below_floor_never_calls_the_judge():
    """Only the band [floor, threshold) triggers; 0.59 stays NEW untouched."""
    cand, existing = _band_pair()
    judge = FakeJudge()
    result = classify(
        cand, [existing], similarity=_pair(0.59), now=NOW,
        tz_name="Asia/Jerusalem", judge=judge,
    )
    assert result.verdict.value == "new"
    assert judge.calls == []  # below the floor: the judge is never consulted


def test_band_pair_judged_same_maps_to_normal_verdicts():
    """judge yes + no diffs + same day -> ALREADY_KNOWN; with a change
    signal and a stated clock diff -> UPDATE (the normal path)."""
    cand, existing = _band_pair()
    judge = FakeJudge(default=True)
    result = classify(
        cand, [existing], similarity=_pair(0.70), now=NOW,
        tz_name="Asia/Jerusalem", judge=judge,
    )
    assert result.verdict.value == "already_known"
    assert len(judge.calls) == 1

    # a stated clock diff + change signal -> UPDATE through the judge
    cand2 = ReconCandidate(kind="event", title=cand.title,
                           start=datetime(2026, 11, 10, 18, 0, tzinfo=JLM),
                           applies_to=("unknown",))
    from brain.services.extraction.reconciliation import ReconSignal
    result2 = classify(
        cand2, [existing], similarity=_pair(0.70), now=NOW,
        tz_name="Asia/Jerusalem", signal=ReconSignal(change=True), judge=judge,
    )
    assert result2.verdict.value == "update"


def test_judge_no_or_none_falls_closed():
    """judge False -> NEW; judge None (error path) -> NEW; never a guess."""
    cand, existing = _band_pair()
    result = classify(
        cand, [existing], similarity=_pair(0.70), now=NOW,
        tz_name="Asia/Jerusalem", judge=FakeJudge(default=False),
    )
    assert result.verdict.value == "new"
    result2 = classify(
        cand, [existing], similarity=_pair(0.70), now=NOW,
        tz_name="Asia/Jerusalem", judge=FakeJudge(default=None),
    )
    assert result2.verdict.value == "new"


def test_ambiguity_short_circuits_before_the_judge():
    """Two band candidates within the margin -> NEW + ambiguous, NO call."""
    cand, existing = _band_pair()
    other = ExistingItem(id="e2", type="event", title=existing.title + " ועוד עוד תוספות",
                         start=existing.start, applies_to=("unknown",))
    judge = FakeJudge()
    result = classify(
        cand, [existing, other], similarity=_pair(0.70), now=NOW,
        tz_name="Asia/Jerusalem", judge=judge,
    )
    assert result.verdict.value == "new"
    assert result.ambiguous is True
    assert judge.calls == []


def test_cap_stops_calls_and_records_one_ops_event(db):
    """The cap (20) bounds per-pass cost: further band candidates fall
    CLOSED and ONE recon/judge_capped event is recorded."""
    from brain.services.extraction.judge import ReconJudge

    judge = ReconJudge(
        jev=None, llm=None, floor=RECON_JUDGE_FLOOR, confidence=0.9, max_calls=20
    )
    judge._decide = lambda c, e: True  # bypass I/O; the cap still counts
    assert judge._calls == 0
    assert judge._capped is False
    assert judge._calls == 0
    cand, existing = _band_pair()
    # 25 distinct band pairs: 20 calls, then the cap closes the rest
    answers = 0
    for n in range(25):
        e = ExistingItem(id=f"e{n}", type="event", title=f"{TITLE_A} ועוד הודעה מספר {n}",
                         start=existing.start, applies_to=("unknown",))
        out = judge(cand, e)
        if out is True:
            answers += 1
    assert answers == 20
    assert judge._calls == 20
    assert judge._capped is True
    # ONE capped ops event, not one per candidate
    events = db.query(OpsEvent).filter_by(component="recon", kind="judge_capped").all()
    assert len(events) == 1


def test_cache_means_one_call_per_pair():
    from brain.services.extraction.judge import ReconJudge

    inner_calls = {"n": 0}

    def decide(c, e):
        inner_calls["n"] += 1
        return True

    judge = ReconJudge(jev=None, llm=None, floor=RECON_JUDGE_FLOOR,
                       confidence=0.9, max_calls=20)
    judge._decide = decide
    cand, existing = _band_pair()
    assert judge(cand, existing) is True
    assert judge(cand, existing) is True  # same pair -> cached
    assert inner_calls["n"] == 1


def test_recon_judge_service_layer_jev_then_llm_then_closed(db):
    """The service-layer judge: Jev yes+confident wins (LLM untouched);
    Jev down -> exactly one strict LLM call; both fail -> CLOSED + ops."""
    from brain.ai.jev import JevChoice, JevError
    from brain.services.extraction.judge import ReconJudge

    class FakeJev:
        def __init__(self, behavior):
            self.behavior = behavior
            self.calls = 0

        def choice(self, question, options, state):
            self.calls += 1
            return self.behavior()

    class FakeLLM:
        def __init__(self, text):
            self.text = text
            self.calls = 0

        def complete(self, prompt):
            self.calls += 1
            return type("C", (), {"text": self.text})()

    import json as _json

    # 1. Jev confident same -> True, LLM untouched
    jev = FakeJev(lambda: JevChoice(option="same", confidence=0.95))
    llm = FakeLLM("")
    judge = ReconJudge(jev=jev, llm=llm, enabled=True, floor=RECON_JUDGE_FLOOR,
                       confidence=0.9, max_calls=20)
    cand, existing = _band_pair()
    assert judge(cand, existing) is True
    assert llm.calls == 0

    # 2. Jev down -> one strict LLM call: same, confident
    jev2 = FakeJev(lambda: (_ for _ in ()).throw(JevError("down")))
    llm2 = FakeLLM(_json.dumps({"same": True, "confidence": 0.93}))
    judge2 = ReconJudge(jev=jev2, llm=llm2, floor=RECON_JUDGE_FLOOR,
                        confidence=0.9, max_calls=20)
    assert judge2(cand, existing) is True
    assert jev2.calls == 1 and llm2.calls == 1

    # 3. both fail -> CLOSED (None), no exception escapes
    jev3 = FakeJev(lambda: (_ for _ in ()).throw(JevError("down")))
    llm3 = FakeLLM("not json at all")
    judge3 = ReconJudge(jev=jev3, llm=llm3, enabled=True, floor=RECON_JUDGE_FLOOR,
                        confidence=0.9, max_calls=20)
    assert judge3(cand, existing) is None

    # 4. LLM low confidence -> CLOSED
    llm4 = FakeLLM(_json.dumps({"same": True, "confidence": 0.5}))
    jev4 = FakeJev(lambda: (_ for _ in ()).throw(JevError("down")))
    judge4 = ReconJudge(jev=jev4, llm=llm4, floor=RECON_JUDGE_FLOOR,
                        confidence=0.9, max_calls=20)
    assert judge4(cand, existing) is None


def test_production_wiring_builds_judge_only_when_enabled(db):
    """The reconciliation service builds the judge through the factory when
    the pass runs; build_judge returns None when disabled (default)."""
    from brain.config import Settings
    from brain.services.extraction.judge import build_judge

    assert build_judge(Settings(), jev=None, llm=None) is None  # default off
    service = EnrichmentService(
        ItemsRepository(db), db, extractor=None, memory=None, daily_cap=100,
        timezone="Asia/Jerusalem",
        similarity_factory=lambda: None,
        judge_factory=lambda: "sentinel-judge",
    )
    assert service.recon._judge is None  # nothing built before a candidate


# --- review fixes 1-4 (feature/recon-judge) -----------------------------------


def _judge_service(db: Session, judge: Any, *, similarity: float = 0.70) -> Any:
    """An enrichment service whose matching is pinned to one similarity and
    whose judge factory returns the given double, plus one source item."""
    from tests.services.test_recon_scope_ask import (  # noqa: PLC0415
        _make_source,
    )

    service = EnrichmentService(
        ItemsRepository(db), db, extractor=None, memory=None, daily_cap=100,
        timezone="Asia/Jerusalem",
        similarity_factory=lambda: (lambda a, b: similarity),
        judge_factory=lambda: judge,
    )
    source = _make_source(db, text="טיול שנתי בפארק — הטיול יוצא ב-18:00")
    return service, source


def test_wiring_the_service_consults_the_judge_on_a_band_pair(db):
    """Review fix 1, RED FIRST: with RECON_JUDGE_ENABLED-style injection the
    service must pass the BUILT judge into classify — a gray-band pair
    (similarity 0.70 < 0.80) is consulted, and a judge 'same' turns the
    pre-judge NEW into a real matched verdict. On the cherry-picked code
    (judge built, never passed) this is red: the judge is never called and
    the outcome stays NEW."""
    from tests.services.test_recon_scope_ask import _candidate_child

    fake = FakeJudge(default=True)
    service, source = _judge_service(db, fake)
    resolved = service._validate(
        _candidate_child(kind="event", title="טיול שנתי בפארק",
                         when_raw="10.11 18:00", quote="הטיול יוצא ב-18:00"),
        source, NOW,
    )
    assert resolved is not None
    # the band pair in the DB: same kind, same audience, same local day,
    # title pinned equal (the similarity stub, not the text, drives the band)
    member = _member_for(db, title="טיול שנתי בפארק")
    outcome = service._reconcile_child(resolved, source, {}, uuid4(), NOW)
    assert fake.calls, (
        "the service must CONSULT the judge for a gray-band pair — built but "
        "never passed is the gap this test pins"
    )
    assert outcome is not None, "a judge 'same' must produce a matched verdict"
    assert outcome.verdict.value == "already_known"
    assert outcome.matched_id == member.id


def _member_for(db: Session, *, title: str, hour_utc: int = 16) -> MemoryItem:
    item = MemoryItem(
        raw_text=title, source="email", type="event",
        occurred_at=datetime(2026, 11, 10, hour_utc, 0, tzinfo=UTC),  # 18:00 JLM at 16
        applies_to=["unknown"], status="filed",
    )
    db.add(item)
    db.commit()
    return item


def test_flag_off_constructs_no_clients_and_caches_the_none(db, monkeypatch):
    """Review fix 2: with the flag OFF the flag is checked BEFORE the client
    constructors (patched to raise — they must never run), and the None
    build is CACHED: the factory runs exactly once even across candidates."""
    from brain import config as brain_config
    from brain.api import deps
    from brain.config import Settings

    monkeypatch.setattr(brain_config, "get_settings", lambda: Settings())

    def _no_client(*a: Any, **k: Any) -> Any:
        raise AssertionError("a disabled judge must never construct a client")

    monkeypatch.setattr(deps, "get_jev_client", _no_client)
    monkeypatch.setattr(deps, "get_llm_client", _no_client)
    assert EnrichmentService._build_recon_judge() is None

    calls = {"n": 0}

    def _counting_factory() -> Any:
        calls["n"] += 1
        return None

    service = EnrichmentService(
        ItemsRepository(db), db, extractor=None, memory=None, daily_cap=100,
        timezone="Asia/Jerusalem",
        similarity_factory=lambda: None,
        judge_factory=_counting_factory,
    )
    service.recon._ensure_judge()
    service.recon._ensure_judge()
    service.recon._ensure_judge()
    assert calls["n"] == 1, "a None build is final: the factory never re-runs"
    assert service.recon._judge is None


def test_recon_eval_judge_flag_off_by_default(tmp_path, monkeypatch, capsys):
    """Review fix 3: `brain recon-eval` never builds a judge (byte-identical
    default); --judge passes the built judge into run_recon_eval."""
    import json as _json

    from brain import cli

    case = {
        "case_id": "c1",
        "existing_items": [{"id": "e1", "type": "event", "title": "טיול שנתי",
                            "start": "2026-11-10T16:00:00+00:00",
                            "applies_to": ["unknown"]}],
        "new_text_quote": "הטיול בוטל",
        "candidate": {"kind": "event", "title": "טיול שנתי",
                      "start": "2026-11-10T16:00:00+00:00",
                      "applies_to": ["unknown"]},
        "verdict": "new",
    }
    cases = tmp_path / "cases.jsonl"
    cases.write_text(_json.dumps(case, ensure_ascii=False) + "\n", encoding="utf-8")

    monkeypatch.setattr(
        cli.EnrichmentService if hasattr(cli, "EnrichmentService") else EnrichmentService,
        "_build_recon_judge",
        lambda: (_ for _ in ()).throw(AssertionError("no judge without --judge")),
    )
    code = cli.main(["recon-eval", "--cases", str(cases)])
    assert code == 0
    assert "cases: 1" in capsys.readouterr().out

    fake = FakeJudge(default=True)
    monkeypatch.setattr(EnrichmentService, "_build_recon_judge", lambda: fake)
    seen: dict[str, Any] = {}

    def _spy(cases_path: Any, **kwargs: Any) -> dict[str, Any]:
        seen["judge"] = kwargs.get("judge")
        return {"cases": 1, "per_verdict": {}, "confusion": {}}

    monkeypatch.setattr(cli, "run_recon_eval", _spy)
    code = cli.main(["recon-eval", "--cases", str(cases), "--judge"])
    assert code == 0
    assert seen["judge"] is fake, "--judge must pass the built judge through"
    assert fake.calls == [], "building the judge must not call it by itself"


def test_off_path_verdicts_are_byte_identical():
    """Review fix 4: with judge=None the verdict table is exactly the
    pre-judge behavior — the band stays CLOSED at 0.80, cross-kind cancels
    stay CANCELLATION, nothing consults anything."""
    cand, existing = _band_pair()  # similarity 0.70 territory
    # same-title pair scores 1.0 -> ABOVE the threshold: unchanged by the judge
    same, existing_same = _band_pair(existing_title="כותרת אחרת לגמרי")
    result_same = classify(
        same, [existing_same], similarity=_pair(1.0), now=NOW,
        tz_name="Asia/Jerusalem", judge=None,
    )
    assert result_same.verdict.value == "already_known"
    # a band pair with judge=None stays NEW (the 0.80 gate, byte-identical)
    result_band = classify(
        cand, [existing], similarity=_pair(0.70), now=NOW,
        tz_name="Asia/Jerusalem", judge=None,
    )
    assert result_band.verdict.value == "new"
    assert result_band.ambiguous is False
    # cross-kind + cancel: main's rule, untouched by the judge plumbing
    cross = ReconCandidate(kind="reminder", title=cand.title,
                           start=cand.start, applies_to=("unknown",))
    result_cross = classify(
        cross, [existing], similarity=_pair(1.0), now=NOW,
        tz_name="Asia/Jerusalem",
        signal=ReconSignal(cancel=True), relax_type=True, judge=None,
    )
    assert result_cross.verdict.value == "cancellation"
    assert result_cross.cross_kind is True


def test_judged_update_never_auto_applies(db):
    """Review B1, RED FIRST: a judge 'yes' in the 0.60-0.80 band must NEVER
    auto-apply — auto-apply was approved for matches ABOVE the 0.80
    threshold only. The relation stays 'proposed', the existing item stays
    visible, and the provenance carries judged=true."""
    from tests.services.test_recon_scope_ask import _candidate_child
    from tests.services.test_reconciliation import _make_source

    from brain.db.models import ItemRelation, ItemSource
    from brain.services.extraction.candidates import ReconHint

    original = _make_source(db, text="טיול שנתי יוצא ב-16:00")
    source = _make_source(db, text="תיקון טעות: הטיול יוצא ב-18:00")  # same sender+thread
    existing = _member_for(db, title="טיול שנתי בפארק", hour_utc=14)  # 16:00 JLM: a 2h diff
    db.add(ItemSource(child_id=existing.id, source_id=original.id, quote="טיול שנתי"))
    db.commit()
    fake = FakeJudge(default=True)
    service = EnrichmentService(
        ItemsRepository(db), db, extractor=None, memory=None, daily_cap=100,
        timezone="Asia/Jerusalem",
        similarity_factory=lambda: (lambda a, b: 0.70),
        judge_factory=lambda: fake,
    )
    resolved = service._validate(
        _candidate_child(kind="event", title="טיול שנתי בפארק",
                         when_raw="10.11 18:00", quote="הטיול יוצא ב-18:00",
                         recon=ReconHint(change=True)),
        source, NOW,
    )
    outcome = service._reconcile_child(resolved, source, {}, uuid4(), NOW)
    assert outcome.verdict.value == "update"
    assert fake.calls, "the band pair must be judged"
    rel = db.query(ItemRelation).filter_by(kind="supersedes").one()
    assert (rel.provenance or {}).get("state") == "proposed", (
        "a judged update NEVER auto-applies"
    )
    assert (rel.provenance or {}).get("judged") is True
    db.refresh(existing)
    assert existing.superseded_by is None, "the existing item stays visible"


def test_above_threshold_update_still_auto_applies(db):
    """CONTROL (B1): the SAME setup at similarity 0.95 — above the band, no
    judge involved — still auto-applies exactly as before the judge
    existed."""
    from tests.services.test_recon_scope_ask import _candidate_child
    from tests.services.test_reconciliation import _make_source

    from brain.db.models import ItemRelation, ItemSource
    from brain.services.extraction.candidates import ReconHint

    original = _make_source(db, text="טיול שנתי יוצא ב-16:00")
    source = _make_source(db, text="תיקון טעות: הטיול יוצא ב-18:00")
    existing = _member_for(db, title="טיול שנתי בפארק", hour_utc=14)  # 16:00 JLM: a 2h diff
    db.add(ItemSource(child_id=existing.id, source_id=original.id, quote="טיול שנתי"))
    db.commit()
    fake = FakeJudge(default=True)
    service = EnrichmentService(
        ItemsRepository(db), db, extractor=None, memory=None, daily_cap=100,
        timezone="Asia/Jerusalem",
        similarity_factory=lambda: (lambda a, b: 0.95),
        judge_factory=lambda: fake,
    )
    resolved = service._validate(
        _candidate_child(kind="event", title="טיול שנתי בפארק",
                         when_raw="10.11 18:00", quote="הטיול יוצא ב-18:00",
                         recon=ReconHint(change=True)),
        source, NOW,
    )
    outcome = service._reconcile_child(resolved, source, {}, uuid4(), NOW)
    assert outcome.verdict.value == "update"
    assert fake.calls == [], "an above-threshold pair never reaches the judge"
    rel = db.query(ItemRelation).filter_by(kind="supersedes").one()
    assert (rel.provenance or {}).get("state") == "applied"
    assert (rel.provenance or {}).get("judged") is None
    db.refresh(existing)
    assert existing.superseded_by == rel.from_id
