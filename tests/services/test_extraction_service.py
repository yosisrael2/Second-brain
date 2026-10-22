"""Enrichment queue + runner (PR A1 brief: stub e2e, cap, re-run diff, listing)."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from brain.db.models import EnrichmentJob, ItemSource, MemoryItem
from brain.db.session import SessionLocal, engine
from brain.repositories.items import ItemsRepository
from brain.repositories.memory import MemoryRepository
from brain.services.extraction.candidates import ChildCandidate
from brain.services.extraction.service import EnrichmentService, qualifies
from brain.services.memory import MemoryService
from conftest import assert_test_database

NOW = datetime(2026, 10, 22, 12, 0, tzinfo=UTC)

TWO_DATES = "אספה 22.10 בבוקר ואז טיול 23.10"


class CountingExtractor:
    """Fake extractor: counts calls, returns fixed candidates, records context."""

    name = "counting-fake"

    def __init__(self, candidates: list[ChildCandidate]) -> None:
        self.candidates = candidates
        self.calls: list[tuple[str, str]] = []

    def extract(self, source: MemoryItem, memory_context: str) -> list[ChildCandidate]:
        self.calls.append((source.raw_text[:40], memory_context[:40]))
        return list(self.candidates)


@dataclass
class World:
    repo: ItemsRepository
    session: Session
    memory: MemoryService
    service: EnrichmentService

    def with_extractor(
        self, extractor: CountingExtractor, *, daily_cap: int = 100
    ) -> EnrichmentService:
        return EnrichmentService(
            self.repo, self.session, extractor=extractor, memory=self.memory, daily_cap=daily_cap
        )


def _wipe(session: Session) -> None:
    session.execute(text("DELETE FROM enrichment_jobs"))
    session.execute(text("DELETE FROM item_sources"))
    session.execute(text("DELETE FROM memory_items"))
    session.execute(text("DELETE FROM memory_facts"))
    session.execute(text("DELETE FROM entity_aliases"))
    session.execute(text("DELETE FROM entities"))
    session.commit()


@pytest.fixture(name="world")
def world_fixture(database: None) -> World:
    """Fresh DB world for one extraction test; wipes both layers around it."""
    assert_test_database(str(engine.url))  # any *_test database, same guard as conftest
    session = SessionLocal()
    _wipe(session)
    repo = ItemsRepository(session)
    memory = MemoryService(MemoryRepository(session))
    yield World(repo, session, memory, EnrichmentService(repo, session, memory=memory))
    session.rollback()
    session.close()
    _wipe(session)


def _capture(
    repo: ItemsRepository, body: str, *, source: str = "manual"
) -> MemoryItem:
    """Capture with a PINNED occurred_at (= NOW) so relative dates ('מחר')
    resolve deterministically regardless of the wall clock."""
    item = MemoryItem(raw_text=body, source=source, occurred_at=NOW)
    repo.add(item)
    repo.commit()
    return item


def _children(session: Session) -> list[MemoryItem]:
    return [row for row in session.query(MemoryItem).all() if row.child_state]


# --- D1(b): which captures qualify ---------------------------------------


def test_qualifies_emails_and_long_and_multi_date_text():
    assert qualifies(MemoryItem(raw_text="x", source="email"))
    assert qualifies(MemoryItem(raw_text="word " * 61, source="manual"))  # > 300 chars
    assert qualifies(MemoryItem(raw_text=TWO_DATES, source="manual"))  # 2 dates
    assert not qualifies(MemoryItem(raw_text="קצר", source="manual"))


def test_enqueue_is_noop_for_non_qualifying_captures(world: World):
    item = _capture(world.repo, "קצר")
    assert world.service.enqueue(item) is None
    assert world.service.queue_depth() == 0


def test_enqueue_is_idempotent_per_item(world: World):
    item = _capture(world.repo, TWO_DATES)
    first = world.service.enqueue(item)
    second = world.service.enqueue(item)
    assert first is not None and second is not None
    assert first.id == second.id
    assert world.service.queue_depth() == 1


# --- stub-driven end-to-end ------------------------------------------------


def test_stub_end_to_end_creates_linked_children(world: World):
    body = (
        "שלום,\n"
        "מחר כינוס הורים באמפי 22.10\n"
        "חולצה לבנה ליום חמישי 23.10\n"
        "שבוע הציפורים 1.11\n"
    )
    source = _capture(world.repo, body, source="email")
    assert world.service.enqueue(source) is not None
    report = world.service.run_once(NOW)

    assert report.processed == 1
    assert report.children_created == 3  # the greeting line is too short to qualify
    links = list(world.session.query(ItemSource).all())
    assert len(links) == 3
    for link in links:
        assert link.source_id == source.id
        assert link.quote in body  # grounding guard evidence lives with the link
    children = [row for row in world.session.query(MemoryItem).all() if row.child_state]
    assert len(children) == 3
    for child in children:
        assert child.child_state == "proposed"
        assert child.occurred_at is not None  # dates resolved, not left raw
        assert child.extraction_meta["model"] == "stub-v0"
    assert source.child_state is None  # the parent row is not a child


def test_ungrounded_dropped_and_logged(
    world: World, caplog: pytest.LogCaptureFixture
):
    """A quote that never appears in the source is dropped, never stored (item 8)."""
    import logging

    caplog.set_level(logging.INFO, logger="brain.services.extraction.service")
    source = _capture(world.repo, "הודעה על אספת הורים בחצר", source="email")
    world.service.enqueue(source)
    liar = CountingExtractor(
        [ChildCandidate(kind="event", title="אספה ביום ראשון", quote="מעולם לא נכתב")]
    )
    report = world.with_extractor(liar).run_once(NOW)

    assert report.dropped_ungrounded == 1
    assert report.children_created == 0
    assert world.session.query(ItemSource).count() == 0
    assert "ungrounded" in caplog.text


def test_date_conflict_child_lands_in_needs_review(world: World):
    """'יום שלישי 7.10' when 7.10.2026 is a Wednesday: flagged, not fixed (item 2b)."""
    source = _capture(world.repo, "תזכורת: יום שלישי 7.10 איסוף מוקדם", source="email")
    world.service.enqueue(source)
    world.service.run_once(NOW)
    child = [row for row in world.session.query(MemoryItem).all() if row.child_state][0]
    assert child.date_conflict is True
    assert child.status.value == "needs_review"
    assert "Tuesday" in child.extraction_meta["date_conflict_note"]
    assert "Wednesday" in child.extraction_meta["date_conflict_note"]


def test_applies_to_resolved_via_memory_unknown_stays_unknown(world: World):
    """Audience resolves against memory (R1-R5); no audience info stays 'unknown' (R4)."""
    world.memory.set_kid(
        "Rona", gender="girl", school="ברקת", grade="2", class_="ב2", teacher="מעין"
    )
    source = _capture(world.repo, "אספת הורים 22.10 בכיתה ב2", source="email")
    extractor = CountingExtractor(
        [
            ChildCandidate(
                kind="event",
                title="אספת הורי כיתה ב2",
                quote="אספת הורים 22.10",
                when_raw="22.10",
                audience={"school": "ברקת", "class_": "ב2"},
            )
        ]
    )
    service = world.with_extractor(extractor)
    service.enqueue(source)
    service.run_once(NOW)
    child = [row for row in world.session.query(MemoryItem).all() if row.child_state][0]
    assert child.applies_to == ["Rona"]

    # a candidate with NO audience info resolves to 'unknown', never 'nobody'
    other = _capture(world.repo, "הודעה כללית ללא קהל 22.10 חשובה", source="email")
    generic = CountingExtractor(
        [ChildCandidate(kind="fact", title="מידע כללי חשוב", quote="הודעה כללית")]
    )
    service2 = world.with_extractor(generic)
    service2.enqueue(other)
    service2.run_once(NOW)
    fact = [
        row
        for row in world.session.query(MemoryItem).all()
        if row.child_state and row.raw_text == "מידע כללי חשוב"
    ]
    assert fact and fact[0].applies_to == ["unknown"]


# --- D5: daily cap -----------------------------------------------------------


def test_daily_cap_defers_the_rest_to_tomorrow(world: World):
    extractor = CountingExtractor([])
    service = world.with_extractor(extractor, daily_cap=2)
    for _ in range(5):
        service.enqueue(_capture(world.repo, TWO_DATES))
    report = service.run_once(NOW, limit=10)

    assert report.processed == 2  # the cap
    assert report.deferred_over_cap == 1  # hit the cap -> deferred the rest
    assert service.cap_used_today(NOW) == 2
    queued = list(world.session.query(EnrichmentJob).filter(EnrichmentJob.state == "queued"))
    assert len(queued) == 3
    # the next-due job was pushed to tomorrow; the rest stay due (oldest-first)
    assert any(job.not_before is not None and job.not_before > NOW for job in queued)
    assert all(job.not_before is None or job.not_before > NOW for job in queued)
    # tomorrow the cap resets and the queue drains oldest-first — but the
    # cap is DAILY: 2 more parents today, the last one the day after (D5)
    tomorrow = datetime(2026, 10, 23, 9, 0, tzinfo=UTC)
    final = service.run_once(tomorrow, limit=10)
    assert final.processed == 2
    assert service.queue_depth() == 1
    day_after = datetime(2026, 10, 24, 9, 0, tzinfo=UTC)
    last = service.run_once(day_after, limit=10)
    assert last.processed == 1
    assert service.queue_depth() == 0


# --- item 18/30: re-run diffs, user actions are respected ---------------------


def test_rerun_never_duplicates_children(world: World):
    source = _capture(world.repo, "אספה 22.10 בבוקר באולם\nחולצה לבנה 23.10", source="email")
    world.service.enqueue(source)
    world.service.run_once(NOW)
    first_count = len([row for row in world.session.query(MemoryItem).all() if row.child_state])
    assert first_count == 2

    # same extractor, same proposals: the diff matches all children -> no new rows
    report = world.service.run_once(NOW)  # enqueue is idempotent, job already done
    world.service.enqueue(source)  # re-enqueue is a no-op (unique item_id, done)
    assert world.service.run_once(NOW).processed == 0
    still = len([row for row in world.session.query(MemoryItem).all() if row.child_state])
    assert still == first_count == 2
    assert world.session.query(ItemSource).count() == 2
    assert report.children_created == 0


def test_kept_child_is_immutable_and_dropped_child_never_reappears(world: World):
    """Item 18 + item 30: the same candidates re-proposed respect user actions."""
    body = "אספה 22.10 בבוקר באולם\nחולצה לבנה ביום חמישי 23.10"
    source = _capture(world.repo, body, source="email")
    world.service.enqueue(source)
    world.service.run_once(NOW)
    children = sorted(
        (row for row in world.session.query(MemoryItem).all() if row.child_state),
        key=lambda row: row.raw_text,
    )
    assert len(children) == 2
    kept, dropped = children[0], children[1]
    kept.child_state = "kept"  # user pressed Keep
    dropped.child_state = "dropped"  # user pressed Drop
    world.session.commit()

    # a job re-opens for the same source (new run id) and re-proposes the SAME
    # titles: the kept child is not modified, the dropped one is not recreated
    job = world.session.query(EnrichmentJob).one()
    job.state = "queued"
    world.session.commit()
    extractor = CountingExtractor(
        [
            ChildCandidate(
                kind="event", title=kept.raw_text, quote=kept.raw_text, when_raw="22.10"
            ),
            ChildCandidate(
                kind="event", title=dropped.raw_text, quote=dropped.raw_text, when_raw="23.10"
            ),
        ]
    )
    report = world.with_extractor(extractor).run_once(NOW)

    assert report.children_created == 0  # neither candidate came back as a new row
    after = sorted(
        (row for row in world.session.query(MemoryItem).all() if row.child_state),
        key=lambda row: row.raw_text,
    )
    assert [row.id for row in after] == [kept.id, dropped.id]
    assert after[0].child_state == "kept" and after[1].child_state == "dropped"


def test_untouched_proposed_child_updates_in_place_when_memory_changes(world: World):
    """Item 18: a proposed (user-untouched) child may be updated in place.

    Run 1: no kid in memory -> applies_to ['unknown']. Then the user seeds the
    kid. Run 2 (same identity): the child now carries ['Rona'].
    """
    body = "אספת הורים לכיתה ב2 ביום חמישי 22.10 בחצר הגדולה"
    source = _capture(world.repo, body, source="email")
    candidate = ChildCandidate(
        kind="event",
        title="אספת הורים ביום חמישי 22.10",
        quote="ביום חמישי 22.10",
        when_raw="22.10",
        audience={"school": "ברקת", "class_": "ב2"},
    )
    service = world.with_extractor(CountingExtractor([candidate]))
    service.enqueue(source)
    service.run_once(NOW)
    child = [row for row in world.session.query(MemoryItem).all() if row.child_state][0]
    assert child.applies_to == ["unknown"]

    world.memory.set_kid(
        "Rona", gender="girl", school="ברקת", grade="2", class_="ב2", teacher="מעין"
    )
    job = world.session.query(EnrichmentJob).one()
    job.state = "queued"
    world.session.commit()
    report = world.with_extractor(CountingExtractor([candidate])).run_once(NOW)

    assert report.children_created == 0
    assert report.children_updated == 1
    world.session.refresh(child)
    assert child.applies_to == ["Rona"]
    assert child.child_state == "proposed"  # still proposed until the user keeps it


# --- item 19: proposed children searchable but unlisted -----------------------


def test_recent_and_review_hide_proposed_children(world: World):
    source = _capture(world.repo, "אספת הורים 22.10 בבוקר באולם הגדול", source="email")
    world.service.enqueue(source)
    world.service.run_once(NOW)
    child = [row for row in world.session.query(MemoryItem).all() if row.child_state][0]

    listed = world.repo.list(item_type=None, project=None, status=None, limit=50, offset=0)
    assert source in listed
    assert all(item.id != child.id for item in listed)  # hidden from /recent + /review

    with_children = world.repo.list(
        item_type=None, project=None, status=None, limit=50, offset=0, include_children=True
    )
    assert any(item.id == child.id for item in with_children)  # opt-in view shows them


def test_kept_children_are_listed_again(world: World):
    source = _capture(world.repo, "אספה 22.10 בבוקר באולם הגדול", source="email")
    world.service.enqueue(source)
    world.service.run_once(NOW)
    child = [row for row in world.session.query(MemoryItem).all() if row.child_state][0]
    child.child_state = "kept"
    world.session.commit()
    listed = world.repo.list(item_type=None, project=None, status=None, limit=50, offset=0)
    assert any(item.id == child.id for item in listed)


def test_ask_can_cite_proposed_children(world: World):
    """Proposed children are ordinary searchable items: /ask cites them (item 19)."""
    from tests.fakes import FakeRepository

    from brain.ai.ask import FakeLLMClient
    from brain.services.ask import AskService
    from brain.services.search import SearchHit

    source = _capture(world.repo, "אספת הורים 22.10 בבוקר באולם", source="email")
    world.service.enqueue(source)
    world.service.run_once(NOW)
    child = [row for row in world.session.query(MemoryItem).all() if row.child_state][0]

    fake_repo = FakeRepository()
    hits = [
        SearchHit(item=fake_repo.add(MemoryItem(raw_text=child.raw_text, id=child.id)), score=0.9)
    ]

    def cite_every_source(prompt: str) -> str:
        ids = [line[2:].split("]")[0] for line in prompt.splitlines() if line.startswith("- [")]
        return "Based on: " + " ".join(f"[{item_id}]" for item_id in ids)

    class StubSearch:
        def search(self, query: str, *, limit: int, offset: int = 0) -> list[SearchHit]:
            return hits[:limit]

        def by_occurrence(self, start, end, *, limit):  # noqa: ANN001
            return []

    ask = AskService(StubSearch(), FakeLLMClient(reply=cite_every_source))  # type: ignore[arg-type]
    result = ask.ask("מה יש ביום חמישי?")
    assert str(child.id) in result.cited_ids


def test_extraction_meta_carries_model_and_run_id(world: World):
    body = "הודעה על אספה 22.10 בחצר הגדולה"
    source = _capture(world.repo, body, source="email")
    world.service.enqueue(source)
    world.service.run_once(NOW)
    child = [row for row in world.session.query(MemoryItem).all() if row.child_state][0]
    meta = child.extraction_meta
    assert meta["model"] == "stub-v0"
    assert uuid.UUID(meta["run_id"])  # a valid run id
    assert meta["when_raw"]


# --- review round 1 (PR #33): kind is preserved, identity per items 18/30 -----


def test_child_kind_round_trips_and_rerun_does_not_duplicate(world: World):
    """RED (defect 1): task/reminder/fact children keep their kind; re-run safe."""
    body = "הודעה על אספה 22.10\nלהביא כיבוד 22.10\nלזכור שהצ'ופר ב-22.10\nחולצה לבנה 22.10"
    source = _capture(world.repo, body, source="email")
    candidates = [
        ChildCandidate(
            kind="event", title="אספת הורים", quote="אספה 22.10", when_raw="22.10"
        ),
        ChildCandidate(
            kind="task", title="להביא כיבוד", quote="להביא כיבוד 22.10", when_raw="22.10"
        ),
        ChildCandidate(
            kind="fact", title="הצ'ופר ב-22.10", quote="הצ'ופר ב-22.10", when_raw="22.10"
        ),
        ChildCandidate(
            kind="reminder", title="חולצה לבנה", quote="חולצה לבנה 22.10", when_raw="22.10"
        ),
    ]
    service = world.with_extractor(CountingExtractor(candidates))
    service.enqueue(source)
    report = service.run_once(NOW)
    assert report.children_created == 4

    kinds = sorted(
        (row.type.value, row.raw_text)
        for row in world.session.query(MemoryItem).all()
        if row.child_state
    )
    assert kinds == [
        ("event", "אספת הורים"),
        ("fact", "הצ'ופר ב-22.10"),
        ("reminder", "חולצה לבנה"),
        ("task", "להביא כיבוד"),
    ]

    # re-run with the SAME candidates: every kind matches its stored child
    job = world.session.query(EnrichmentJob).one()
    job.state = "queued"
    world.session.commit()
    report2 = world.with_extractor(CountingExtractor(candidates)).run_once(NOW)
    assert report2.children_created == 0  # no duplicates for ANY kind (item 18)
    assert len([row for row in world.session.query(MemoryItem).all() if row.child_state]) == 4


def test_dropped_task_child_stays_suppressed_after_kind_fix(world: World):
    """RED (defect 1b): a DROPPED task child must never be recreated (item 30)."""
    body = "להגיש טופס 22.10 בהקדם"
    source = _capture(world.repo, body, source="email")
    candidates = [
        ChildCandidate(kind="task", title="להגיש טופס", quote="להגיש טופס 22.10", when_raw="22.10")
    ]
    service = world.with_extractor(CountingExtractor(candidates))
    service.enqueue(source)
    service.run_once(NOW)
    child = _children(world.session)[0]
    child.child_state = "dropped"  # user pressed Drop
    world.session.commit()
    job = world.session.query(EnrichmentJob).one()
    job.state = "queued"
    world.session.commit()

    report = world.with_extractor(CountingExtractor(candidates)).run_once(NOW)
    assert report.children_created == 0  # was recreated before the fix
    assert len(_children(world.session)) == 1


def test_reworded_title_still_matches_on_rerun(world: World):
    """RED (defect 2): an LLM rewording must match the same child (items 18/30)."""
    body = "דיאלוג בנות ביום שני 19.10 איסוף 13:35"
    source = _capture(world.repo, body, source="email")
    v1 = ChildCandidate(
        kind="event", title="דיאלוג בנות 13:35", quote="דיאלוג בנות", when_raw="19.10"
    )
    service = world.with_extractor(CountingExtractor([v1]))
    service.enqueue(source)
    service.run_once(NOW)
    assert len(_children(world.session)) == 1

    # the next run rewords the title — same child, not a second one
    v2 = ChildCandidate(
        kind="event", title="דיאלוג בנות, איסוף 13:35", quote="דיאלוג בנות", when_raw="19.10"
    )
    job = world.session.query(EnrichmentJob).one()
    job.state = "queued"
    world.session.commit()
    report = world.with_extractor(CountingExtractor([v2])).run_once(NOW)
    assert report.children_created == 0
    assert len(_children(world.session)) == 1


def test_same_title_on_a_different_date_is_a_new_child(world: World):
    """RED (defect 2): date is part of identity — 22.10 vs 23.10 do not match."""
    body = "אספה 22.10 בבוקר\nאספה 23.10 בבוקר"
    source = _capture(world.repo, body, source="email")
    candidates = [
        ChildCandidate(kind="event", title="אספה בבוקר", quote="אספה 22.10", when_raw="22.10"),
        ChildCandidate(kind="event", title="אספה בבוקר", quote="אספה 23.10", when_raw="23.10"),
    ]
    service = world.with_extractor(CountingExtractor(candidates))
    service.enqueue(source)
    report = service.run_once(NOW)
    assert report.children_created == 2  # NOT deduped: different dates
    dates = sorted(str(row.occurred_at) for row in _children(world.session))
    assert len(set(dates)) == 2


def test_same_title_different_applies_to_is_not_the_same_child(world: World):
    """RED (defect 2): applies_to is part of identity."""
    body = "אספת הורים 22.10 בכיתה ב2 ואספת הורים 22.10 בכיתה ג3"
    source = _capture(world.repo, body, source="email")
    candidates = [
        ChildCandidate(
            kind="event", title="אספת הורים לכיתה ב2",
            quote="אספת הורים 22.10 בכיתה ב2",
            when_raw="22.10", audience={"school": "ברקת", "grades": "2"},
        ),
        ChildCandidate(
            kind="event", title="אספת הורים לכיתה ה",
            quote="אספת הורים 22.10 בכיתה ג3",
            when_raw="22.10", audience={"school": "ברקת", "grades": "5"},
        ),
    ]
    world.memory.set_kid(
        "Rona", gender="girl", school="ברקת", grade="2", class_="ב2", teacher="מעין"
    )
    service = world.with_extractor(CountingExtractor(candidates))
    service.enqueue(source)
    report = service.run_once(NOW)
    assert report.children_created == 2  # different audiences: two children
    audiences = sorted(tuple(row.applies_to) for row in _children(world.session))
    assert audiences == [("Rona",), ("nobody",)]


def test_same_title_with_different_applies_to_stays_two_children(world: World):
    """Red (defect 2): identity includes applies_to even with IDENTICAL titles."""
    body = "מסיבת יום הולדת 22.10 בכיתה ב2 ומסיבת יום הולדת 22.10 בכיתה ה"
    source = _capture(world.repo, body, source="email")
    world.memory.set_kid(
        "Rona", gender="girl", school="ברקת", grade="2", class_="ב2", teacher="מעין"
    )
    candidates = [
        ChildCandidate(
            kind="event", title="מסיבת יום הולדת", quote="מסיבת יום הולדת 22.10",
            when_raw="22.10", audience={"school": "ברקת", "grades": "2"},
        ),
        ChildCandidate(
            kind="event", title="מסיבת יום הולדת", quote="מסיבת יום הולדת 22.10",
            when_raw="22.10", audience={"school": "ברקת", "grades": "5"},
        ),
    ]
    service = world.with_extractor(CountingExtractor(candidates))
    service.enqueue(source)
    report = service.run_once(NOW)
    assert report.children_created == 2  # same title, different audiences
    audiences = sorted(tuple(row.applies_to) for row in _children(world.session))
    assert audiences == [("Rona",), ("nobody",)]


def test_dropped_child_suppressed_even_after_rewording(world: World):
    """RED (defect 2): item 30 suppression follows the RELAXED matcher."""
    body = "דיאלוג בנות ביום שני 19.10 איסוף 13:35"
    source = _capture(world.repo, body, source="email")
    v1 = ChildCandidate(
        kind="event", title="דיאלוג בנות איסוף 13:35", quote="דיאלוג בנות", when_raw="19.10"
    )
    service = world.with_extractor(CountingExtractor([v1]))
    service.enqueue(source)
    service.run_once(NOW)
    child = _children(world.session)[0]
    child.child_state = "dropped"  # user pressed Drop
    world.session.commit()
    job = world.session.query(EnrichmentJob).one()
    job.state = "queued"
    world.session.commit()

    v2 = ChildCandidate(
        kind="event", title="דיאלוג בנות, איסוף 13:35", quote="דיאלוג בנות", when_raw="19.10"
    )
    report = world.with_extractor(CountingExtractor([v2])).run_once(NOW)
    assert report.children_created == 0  # reworded, but still the SAME child
    assert len(_children(world.session)) == 1


def test_very_long_quote_does_not_break_the_link_table(world: World):
    """RED (defect 3): a 5,000-char INCOMPRESSIBLE quote must not hit the btree.

    Repetitive Hebrew compresses below the btree row limit and hides the bug
    (verified: it passes against the broken schema); random hex words do not.
    """
    import uuid as _uuid

    body = " ".join(_uuid.uuid4().hex for _ in range(420))[:5100] + " 22.10"
    source = _capture(world.repo, body, source="email")
    long_quote = body[:5000]  # 5,000 incompressible chars (random hex words)
    candidate = ChildCandidate(
        kind="event", title="יום הספורט", quote=long_quote, when_raw="22.10"
    )
    service = world.with_extractor(CountingExtractor([candidate]))
    service.enqueue(source)
    report = service.run_once(NOW)
    assert report.children_created == 1
    link = world.session.query(ItemSource).one()
    assert link.quote == long_quote


# --- review round 1 small items ---------------------------------------------


def test_enrich_cli_refuses_the_live_store(
    monkeypatch: pytest.MonkeyPatch,
):
    """`brain enrich` carries the PR #31 live-store guard (small 5).

    The API lifespan guard covers the app only; the CLI must fail fast the
    same way when pointed at the live store, unless BRAIN_ALLOW_LIVE=1.
    """
    from brain import cli
    from brain.config import LIVE_DATABASE_URL, LIVE_QDRANT_COLLECTION, get_settings

    monkeypatch.setenv("DATABASE_URL", LIVE_DATABASE_URL)
    monkeypatch.setenv("QDRANT_COLLECTION", LIVE_QDRANT_COLLECTION)
    monkeypatch.delenv("BRAIN_ALLOW_LIVE", raising=False)
    get_settings.cache_clear()
    try:
        with pytest.raises(RuntimeError, match="LIVE store"):
            cli.main(["enrich", "--limit", "1"])
    finally:
        get_settings.cache_clear()


# --- review round 2: identity threshold + best-match --------------------------


def test_different_same_day_reminders_stay_two_children(world: World):
    """Red (round 2): short same-day reminders must not merge at 0.3-0.6.

    'חולצה לבנה' vs 'להביא סרגל' measures 0.40 and 'Bring a reading book' vs
    'Bring a ruler' 0.61 — both cleared the old eval-pairing threshold and
    one child silently disappeared.
    """
    body = "לזכור: חולצה לבנה 22.10 ולהביא סרגל 22.10"
    source = _capture(world.repo, body, source="email")
    candidates = [
        ChildCandidate(
            kind="reminder", title="חולצה לבנה", quote="חולצה לבנה 22.10", when_raw="22.10"
        ),
        ChildCandidate(
            kind="reminder", title="להביא סרגל", quote="להביא סרגל 22.10", when_raw="22.10"
        ),
    ]
    service = world.with_extractor(CountingExtractor(candidates))
    service.enqueue(source)
    report = service.run_once(NOW)
    assert report.children_created == 2  # two DIFFERENT reminders, same day
    titles = sorted(row.raw_text for row in _children(world.session))
    assert titles == ["חולצה לבנה", "להביא סרגל"]


def test_different_english_reminders_stay_two_children(world: World):
    """Red (round 2): 'Bring a reading book' vs 'Bring a ruler' = 0.61."""
    body = "Bring a reading book on 22.10 and bring a ruler on 22.10"
    source = _capture(world.repo, body, source="email")
    candidates = [
        ChildCandidate(
            kind="reminder", title="Bring a reading book",
            quote="Bring a reading book on 22.10", when_raw="22.10",
        ),
        ChildCandidate(
            kind="reminder", title="Bring a ruler",
            quote="bring a ruler on 22.10", when_raw="22.10",
        ),
    ]
    service = world.with_extractor(CountingExtractor(candidates))
    service.enqueue(source)
    report = service.run_once(NOW)
    assert report.children_created == 2


def test_reworded_duplicate_still_matches_at_the_stricter_threshold(world: World):
    """The reworded dialogue (0.83) still matches at 0.7 — same single child."""
    body = "דיאלוג בנות ביום שני 19.10 איסוף 13:35"
    source = _capture(world.repo, body, source="email")
    v1 = ChildCandidate(
        kind="event", title="דיאלוג בנות איסוף 13:35", quote="דיאלוג בנות", when_raw="19.10"
    )
    service = world.with_extractor(CountingExtractor([v1]))
    service.enqueue(source)
    service.run_once(NOW)
    job = world.session.query(EnrichmentJob).one()
    job.state = "queued"
    world.session.commit()
    v2 = ChildCandidate(
        kind="event", title="דיאלוג בנות, איסוף 13:35", quote="דיאלוג בנות", when_raw="19.10"
    )
    report = world.with_extractor(CountingExtractor([v2])).run_once(NOW)
    assert report.children_created == 0
    assert report.children_updated == 1
    assert len(_children(world.session)) == 1


def test_of_two_qualifying_children_the_most_similar_is_updated(world: World):
    """Red (round 2): several qualifying children -> the MOST similar updates."""
    body = "דיאלוג בנות כיתה ב2 ביום שני 19.10 איסוף 13:35"
    source = _capture(world.repo, body, source="email")
    base = ChildCandidate(
        kind="event", title="דיאלוג בנות כיתה ב2", quote="דיאלוג בנות", when_raw="19.10"
    )
    service = world.with_extractor(CountingExtractor([base]))
    service.enqueue(source)
    service.run_once(NOW)
    # a second child, clearly a DIFFERENT thing (sim to child 1 = 0.38 < 0.7)
    other = ChildCandidate(
        kind="event", title="אספת מורים כיתה ג3", quote="דיאלוג בנות", when_raw="19.10"
    )
    job = world.session.query(EnrichmentJob).one()
    job.state = "queued"
    world.session.commit()
    service2 = world.with_extractor(CountingExtractor([other]))
    service2.run_once(NOW)
    assert len(_children(world.session)) == 2

    # now re-propose a title clearly closest to the SECOND child: it must
    # update THAT one (higher similarity), not the first-iterated match
    job = world.session.query(EnrichmentJob).one()
    job.state = "queued"
    world.session.commit()
    # sim to child 2 = 0.73 (>= 0.7), sim to child 1 = 0.32 -> the MOST
    # similar (child 2) must be the one updated
    closer = ChildCandidate(
        kind="event", title="אספת מורים כיתה ג3 מיוחדת ביותר", quote="דיאלוג בנות", when_raw="19.10"
    )
    report = world.with_extractor(CountingExtractor([closer])).run_once(NOW)
    assert report.children_created == 0
    assert report.children_updated == 1
    titles = sorted(row.raw_text for row in _children(world.session))
    assert "אספת מורים כיתה ג3 מיוחדת ביותר" in titles  # child 2 was updated
    assert "דיאלוג בנות כיתה ב2" in titles  # child 1 untouched
