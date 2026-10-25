"""Ask-path leg robustness (post-bug-hunt follow-up to PR #104/#119).

One bad leg never kills an answer; the answer never hides a contradiction
it knows about. Fakes + the real *_test DB only.
"""

from datetime import UTC, datetime

import pytest
from sqlalchemy import text as sql_text
from sqlalchemy.orm import Session
from tests.conftest import assert_test_database

from brain.ai.ask import FakeLLMClient
from brain.db.models import ItemRelation, MemoryItem
from brain.db.session import SessionLocal, engine
from brain.repositories.items import ItemsRepository
from brain.repositories.memory import MemoryRepository
from brain.services.ask import AskService
from brain.services.memory import MemoryService
from brain.services.search import SearchHit

NOW = datetime(2026, 10, 24, 9, 0, tzinfo=UTC)


class RecordingSearch:
    def __init__(self, hits: list[SearchHit]) -> None:
        self._hits = hits
        self.seen_extra: list[list[MemoryItem]] = []

    def search(self, query, *, limit, offset=0, extra_items=None) -> list[SearchHit]:
        extra = list(extra_items or [])
        self.seen_extra.append(extra)
        return (list(self._hits) + [SearchHit(item=i, score=None) for i in extra])[:limit]

    def by_occurrence(self, start, end, *, limit) -> list[SearchHit]:
        return []


class PromptCapturingLLM(FakeLLMClient):
    """FakeLLMClient that also keeps every prompt it was given."""

    def __init__(self) -> None:
        super().__init__(reply=self._echo)
        self.prompts: list[str] = []

    def _echo(self, prompt: str) -> str:
        return "ok"

    def complete(self, prompt: str):
        self.prompts.append(prompt)
        return super().complete(prompt)


def _wipe() -> None:
    assert_test_database(str(engine.url))
    with engine.begin() as conn:
        conn.execute(sql_text("DELETE FROM memory_items"))
        conn.execute(sql_text("DELETE FROM item_relations"))
        conn.execute(sql_text("DELETE FROM memory_facts"))
        conn.execute(sql_text("DELETE FROM entity_aliases"))
        conn.execute(sql_text("DELETE FROM entities"))


_LEAKED: list[Session] = []


@pytest.fixture(autouse=True)
def _close_leaked_sessions():
    yield
    while _LEAKED:
        _LEAKED.pop().close()


def _world():
    _wipe()
    session = SessionLocal()
    _LEAKED.append(session)
    memory = MemoryService(MemoryRepository(session))
    return memory, ItemsRepository(session), session


def _wa(repo, channel, body, *, day=20, hour=15):
    return repo.add(
        MemoryItem(
            source="whatsapp",
            raw_text=f"[{day}/10/2026, {hour}:00] {channel} / מעין: {body}",
            occurred_at=datetime(2026, 10, day, hour - 2, 0, tzinfo=UTC),
            created_at=datetime(2026, 10, day, 12, 0, tzinfo=UTC),
            meta={
                "kind": "whatsapp_conversation",
                "channel": channel,
                "count": 1,
                "messages": [],
            },
        )
    )


def _seed_itamar(memory: MemoryService) -> None:
    memory.add_entity("person", "Itamar", aliases=["איתמר"])
    memory.set_kid("Itamar", gender="boy", school="בראשית", grade="2", class_="ב' ברקת")
    memory.set_channel("ב' ברקת - הורים", kind="parents", applies_to="Itamar")


def test_kid_leg_broken_sql_does_not_kill_the_ask(clean_items, memory_db) -> None:
    """RED: the kid leg runs GENUINELY broken SQL (a missing table, not a
    raised mock). The ask must still answer from the semantic hits, and a
    later query on the SAME session must still work — no aborted
    transaction (the #117 trap)."""
    memory, repo, session = _world()
    _seed_itamar(memory)
    hit = repo.add(MemoryItem(raw_text="מכתב הנהלה על תשלומים"))

    class BrokenSQLMemory:
        """overview() that really breaks the shared session's transaction."""

        def __init__(self, s: Session) -> None:
            self._s = s

        def overview(self, **kw):
            self._s.execute(sql_text("SELECT * FROM no_such_table_ask_probe"))
            return []  # pragma: no cover — the SQL raises first

        def channel_names_for_kid(self, kid: str) -> list[str]:
            return []

    service = AskService(
        RecordingSearch([SearchHit(item=hit, score=0.9)]),
        FakeLLMClient(reply=lambda p: "ok"),  # type: ignore[arg-type]
        memory=BrokenSQLMemory(session),  # type: ignore[arg-type]
        items_repo=repo,
    )
    result = service.ask("מה איתמר צריך?", now=NOW)
    assert [str(h.item.id) for h in result.sources] == [str(hit.id)], (
        "a broken kid leg must not kill the ask"
    )
    # the same session still works afterwards (savepoint scoped the rollback)
    still = repo.by_text_any(["תשלומים"], limit=5)
    assert [str(i.id) for i in still] == [str(hit.id)], (
        "a later query on the same session must work after a broken kid leg"
    )


def test_dedupe_repoints_conflict_markers(clean_items, memory_db) -> None:
    """RED: the counterpart's raw_text duplicates ANOTHER in-context item
    (the WhatsApp split-chunk shape). The text dedupe drops the counterpart;
    the conflict markers must be RE-POINTED to the surviving item so both
    ends of the contradiction stay in the prompt."""
    memory, repo, session = _world()
    a = repo.add(MemoryItem(raw_text="הטיול ביום רביעי בוטל"))
    c = repo.add(MemoryItem(raw_text="הטיול ביום רביעי בוטל — עדכון מהמורה"))
    b = repo.add(MemoryItem(raw_text="הטיול ביום רביעי בוטל — עדכון מהמורה"))  # dupe of c
    repo._db.add(ItemRelation(kind="conflicts", from_id=a.id, to_id=b.id))
    repo.commit()
    llm = PromptCapturingLLM()
    service = AskService(
        RecordingSearch([SearchHit(item=a, score=0.9), SearchHit(item=c, score=0.8)]),
        llm,
        memory=memory,
        items_repo=repo,
    )
    result = service.ask("מה עם הטיול?", now=NOW)
    ids = [str(h.item.id) for h in result.sources]
    assert str(a.id) in ids and str(c.id) in ids and str(b.id) not in ids, (
        f"the counterpart collapses into its text-duplicate; both survivors stay ({ids})"
    )
    prompt = llm.prompts[-1]
    assert f"⚠️ conflicts with: [{b.id}]" not in prompt, "no marker for a dropped id"
    assert f"⚠️ conflicts with: [{a.id}]" in prompt, (
        "the surviving counterpart carries the re-pointed marker"
    )
    assert f"⚠️ conflicts with: [{c.id}]" in prompt, (
        "the original item carries the re-pointed marker too"
    )


def test_conflict_counterparts_exclude_noise(clean_items, memory_db) -> None:
    """RED: the kid leg excludes noise-triaged conversations; the conflict
    repo query must apply the same rule — a noise counterpart is not a live
    contradiction for the ask context."""
    memory, repo, session = _world()
    a = repo.add(MemoryItem(raw_text="האימון היום בוטל"))
    noise = repo.add(
        MemoryItem(
            source="whatsapp",
            raw_text="[20/10/2026, 10:00] קבוצה / מישהו: צ'אט על המשחק אתמול",
            triage_label="noise",
            meta={"channel": "קבוצה", "count": 1, "messages": []},
        )
    )
    repo._db.add(ItemRelation(kind="conflicts", from_id=a.id, to_id=noise.id))
    repo.commit()
    pairs = repo.conflict_counterparts([a.id], limit=4)
    assert list(pairs) == [], "noise-triaged counterparts must not be pulled"


def test_conflict_cap_truncation_note_says_how_many(clean_items, memory_db) -> None:
    """RED: 6 live conflict relations, cap 4 -> 4 counterparts pulled and the
    prompt carries one line saying 2 more conflicting items were not shown."""
    memory, repo, session = _world()
    a = repo.add(MemoryItem(raw_text="מקור: הפגישה בשעה 10:00"))
    others = [
        repo.add(MemoryItem(raw_text=f"גרסה {n}: הפגישה בשעה {n}:00")) for n in range(6)
    ]
    for o in others:
        repo._db.add(ItemRelation(kind="conflicts", from_id=a.id, to_id=o.id))
    repo.commit()
    llm = PromptCapturingLLM()
    service = AskService(
        RecordingSearch([SearchHit(item=a, score=0.9)]),
        llm,
        memory=memory,
        items_repo=repo,
    )
    result = service.ask("מתי הפגישה?", now=NOW)
    ids = [str(h.item.id) for h in result.sources]
    assert str(a.id) in ids
    assert len(ids) == 5, "4 counterparts + the source (cap unchanged)"
    assert "2" in llm.prompts[-1] and "not shown" in llm.prompts[-1], (
        "the prompt must carry one truncation line counting the hidden conflicts"
    )
