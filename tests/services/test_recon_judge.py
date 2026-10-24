"""The recon second-stage judge (approved plan; D-recon-1 follow-up (a)).

Band [RECON_JUDGE_FLOOR, RECON_SIMILARITY_THRESHOLD): type + applies_to +
window + exclusion already passed and the ambiguity margin is clear — a
typed yes/no decides SAME REAL-WORLD THING or not. Jev first, then exactly
one strict LLM call, then fall CLOSED (never a guessed match). Cap 20 per
pass, in-process cache, RECON_JUDGE_ENABLED default FALSE.
"""

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy.orm import Session

from brain.db.models import OpsEvent
from brain.db.session import SessionLocal, engine
from brain.repositories.items import ItemsRepository
from brain.services.extraction.reconciliation import (
    RECON_JUDGE_FLOOR,
    ExistingItem,
    ReconCandidate,
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
