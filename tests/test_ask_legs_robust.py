"""Ask-path leg robustness (post-bug-hunt follow-up to PR #104/#119).

One bad leg never kills an answer; the answer never hides a contradiction
it knows about. Fakes + the real *_test DB only.
"""

from datetime import UTC, datetime, timedelta

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
    repo.commit()  # production shape: the ask path is read-only, its reads
    # see COMMITTED rows — the overview failure must not eat them

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


def test_kid_repo_query_broken_sql_savepoint_keeps_uncommitted(
    clean_items, memory_db
) -> None:
    """The savepoint layer: by_text_any runs genuinely broken SQL INSIDE the
    kid leg's savepoint (the leg's repo queries are savepointed after the
    kid-mention check). The ask answers without kid additions, a later query
    on the same session works, and — the property the full-rollback overview
    path cannot give — UNCOMMITTED work from earlier in the transaction
    survives the scoped rollback."""
    memory, repo, session = _world()
    _seed_itamar(memory)
    hit = repo.add(MemoryItem(raw_text="מכתב הנהלה על תשלומים"))  # uncommitted

    class BrokenByTextInputRepo:
        """The real repo: by_text_any runs genuinely broken SQL."""

        def __init__(self, real: ItemsRepository) -> None:
            self._real = real
            self._db = real._db

        def by_text_any(self, needles, *, limit):
            self._db.execute(sql_text("SELECT * FROM no_such_table_ask_kid_text"))
            return []  # pragma: no cover — the SQL raises first

        def by_channels(self, channels, *, limit, ranges=None):
            return self._real.by_channels(channels, limit=limit, ranges=ranges)

        def conflict_counterparts(self, item_ids, *, limit=4):
            return self._real.conflict_counterparts(item_ids, limit=limit)

    service = AskService(
        RecordingSearch([SearchHit(item=hit, score=0.9)]),
        FakeLLMClient(reply=lambda p: "ok"),  # type: ignore[arg-type]
        memory=memory,
        items_repo=BrokenByTextInputRepo(repo),  # type: ignore[arg-type]
    )
    result = service.ask("מה איתמר צריך?", now=NOW)
    assert [str(h.item.id) for h in result.sources] == [str(hit.id)], (
        "a broken kid-leg query must not kill the ask"
    )
    still = repo.by_text_any(["תשלומים"], limit=5)
    assert str(hit.id) in {str(i.id) for i in still}, (
        "the session works AND the uncommitted seed survived the savepoint rollback"
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
    assert "2 more conflicting items were not shown" in llm.prompts[-1], (
        "the prompt must carry the full truncation line, not a bare count"
    )


class _BrokenCountRepo:
    """Wraps the real repo: conflict_counterparts works, the COUNT runs
    genuinely broken SQL on the shared session."""

    def __init__(self, real: ItemsRepository) -> None:
        self._real = real
        self._db = real._db

    def conflict_counterparts(self, item_ids, *, limit=4):
        return self._real.conflict_counterparts(item_ids, limit=limit)

    def count_conflict_relations(self, item_ids):
        self._db.execute(sql_text("SELECT * FROM no_such_table_ask_count"))
        return 0  # pragma: no cover — the SQL raises first


def test_conflict_count_broken_sql_no_note_and_session_alive(
    clean_items, memory_db
) -> None:
    """RED: the truncation count ran OUTSIDE the savepoint (in the return
    statement) — a real DB error there aborted the ask's transaction (the
    #117 trap again). The count must be savepoint-scoped: the ask answers,
    the 4 pairs stay in context with BOTH conflict markers and NO note, and
    the session still works. (The count runs genuinely broken SQL — a real
    DB error, not a raised mock.)"""
    memory, repo, session = _world()
    a = repo.add(MemoryItem(raw_text="מקור: הפגישה בשעה 10:00"))
    others = [
        repo.add(MemoryItem(raw_text=f"גרסה {n}: הפגישה בשעה {n}:00")) for n in range(6)
    ]
    for o in others:
        repo._db.add(ItemRelation(kind="conflicts", from_id=a.id, to_id=o.id))
    repo.commit()
    broken = _BrokenCountRepo(repo)
    llm = PromptCapturingLLM()
    service = AskService(
        RecordingSearch([SearchHit(item=a, score=0.9)]),
        llm,
        memory=memory,
        items_repo=broken,  # type: ignore[arg-type]
    )
    result = service.ask("מתי הפגישה?", now=NOW)
    ids = [str(h.item.id) for h in result.sources]
    assert len(ids) == 5, "the 4 capped pairs stay in context"
    prompt = llm.prompts[-1]
    assert prompt.count("⚠️ conflicts with:") == 5, (
        "both ends of every kept pair stay marked: the source lists its 4 "
        "counterparts and each counterpart lists the source"
    )
    assert "NOTE:" not in prompt, "a broken count loses the note, never the ask"
    still = repo.by_text_any(["פגישה"], limit=5)
    assert str(a.id) in {str(i.id) for i in still}, (
        "a later query on the same session must work after a broken count "
        "(membership, not position: equal created_at ties order to random ids)"
    )


def test_non_uuid_pool_id_no_failure_event(clean_items, memory_db) -> None:
    """RED: _conflict_leg's ValueError guard still returned a 2-tuple against
    the 3-tuple signature — a non-uuid pool id raised an unpack error inside
    ask()'s try and was MISREPORTED as conflict_leg_failed. With the fix: no
    failure event, the leg just contributes nothing."""
    memory, repo, session = _world()
    hit = repo.add(MemoryItem(raw_text="מכתב הנהלה על תשלומים"))

    class _StrIdItem:
        id = "not-a-uuid"
        raw_text = "רשומה עם מזהה לא תקני"
        occurred_at = None
        meta: dict = {}

    search = RecordingSearch(
        [SearchHit(item=hit, score=0.9), SearchHit(item=_StrIdItem(), score=0.8)]
    )
    llm = FakeLLMClient(reply=lambda p: "ok")
    llm.synthetic = False  # recording ON: a misreported failure would be visible
    service = AskService(search, llm, memory=memory, items_repo=repo)
    result = service.ask("מה עם התשלומים?", now=NOW)
    ids = [str(h.item.id) for h in result.sources]
    assert str(hit.id) in ids, "the ask still answers"
    with engine.begin() as conn:
        failed = conn.execute(
            sql_text("SELECT count(*) FROM ops_events WHERE kind='conflict_leg_failed'")
        ).scalar()
    assert failed == 0, "a non-uuid pool id must not be misreported as a leg failure"


def test_by_text_any_excludes_noise(clean_items, memory_db) -> None:
    """RED: the kid leg's text needles used the same repo query family as
    by_channels but WITHOUT the noise rule — one rule now: noise-triaged
    conversations are never ask-context material."""
    memory, repo, session = _world()
    _seed_itamar(memory)
    repo.add(
        MemoryItem(
            source="whatsapp",
            raw_text="[20/10/2026, 10:00] ב' ברקת - הורים / מישהו: צ'אט סתם על המשחק",
            triage_label="noise",
            meta={"channel": "ב' ברקת - הורים", "count": 1, "messages": []},
        )
    )
    recalled = repo.by_text_any(["ב' ברקת - הורים"], limit=16)
    assert [str(i.id) for i in recalled] == [], (
        "noise-triaged rows must not come back through the kid-leg needles"
    )


def test_conflicts_below_the_cut_no_orphans_no_note(
    clean_items, memory_db
) -> None:
    """RED (MAJOR 2): the conflict leg ran on the PRE-top_k pool. X at pool
    rank 3+ (below the cut, top_k=2) conflicts with Y outside the pool: Y
    was pulled into the context, then the pair was DROPPED because X is not
    in the final ids — Y appeared with no conflict marker, the 4-pair cap
    was spent on relations that touch nothing in the prompt, and the note
    counted items that were never candidates. The leg must run on the ids
    ACTUALLY in the final context: no orphan counterpart, no misleading
    note."""
    memory, repo, session = _world()
    h1 = repo.add(MemoryItem(raw_text="מקור: הטיול ביום רביעי"))
    h2 = repo.add(MemoryItem(raw_text="מקור: האימון ביום חמישי"))
    below = [repo.add(MemoryItem(raw_text=f"מתחת לקאט {n}")) for n in range(6)]
    ys = [
        repo.add(MemoryItem(raw_text=f"גרסה נגדית {n}: הטיול בוטל")) for n in range(6)
    ]
    for x, y in zip(below, ys, strict=False):
        repo._db.add(ItemRelation(kind="conflicts", from_id=x.id, to_id=y.id))
    repo.commit()
    llm = PromptCapturingLLM()
    service = AskService(
        RecordingSearch(
            [SearchHit(item=h1, score=0.9), SearchHit(item=h2, score=0.8)]
            + [SearchHit(item=x, score=0.5) for x in below]
        ),
        llm,
        memory=memory,
        items_repo=repo,
        top_k=2,
    )
    result = service.ask("מה עם הטיול?", now=NOW)
    ids = [str(h.item.id) for h in result.sources]
    assert set(ids) == {str(h1.id), str(h2.id)}, (
        f"no orphan counterparts from below-the-cut conflicts, got {ids}"
    )
    assert "⚠️" not in llm.prompts[-1], "no in-context conflict, so no marker"
    assert "NOTE:" not in llm.prompts[-1], (
        "the count covers only ids actually in the final context — no "
        "misleading note about items that were never candidates"
    )


def test_prompt_byte_identical_no_dedupe_multi_conflict(
    clean_items, memory_db
) -> None:
    """RED (MINOR 1): the rebuilt map re-INSERTS entries (forward+backward)
    instead of mapping the leg's lists in place — a 3-cycle reorders the
    ⚠️ lists (c's [b,a] rendered as [a,b]) and the prompt is no longer
    byte-identical to the pre-#135 pipeline. The rebuild must map the leg's
    lists in place, keeping the original order."""
    memory, repo, session = _world()
    named = {
        "a": repo.add(MemoryItem(raw_text="גרסה א: הטיול ביום רביעי")),
        "b": repo.add(MemoryItem(raw_text="גרסה ב: הטיול ביום רביעי")),
        "c": repo.add(MemoryItem(raw_text="גרסה ג: הטיול ביום רביעי")),
    }
    base = datetime(2026, 10, 20, 10, 0, tzinfo=UTC)
    # created order: c-a oldest, then b-c, then a-b newest -> the leg reads
    # them newest first (a-b, b-c, c-a) and the map is deterministic:
    # {a:[b,c], b:[a,c], c:[b,a]} — the pre-#135 prompt kept exactly that.
    for i, (x, y) in enumerate([("c", "a"), ("b", "c"), ("a", "b")]):
        repo._db.add(
            ItemRelation(
                kind="conflicts",
                from_id=named[x].id,
                to_id=named[y].id,
                created_at=base + timedelta(minutes=i),
            )
        )
    repo.commit()
    llm = PromptCapturingLLM()
    service = AskService(
        RecordingSearch(
            [
                SearchHit(item=named["a"], score=0.9),
                SearchHit(item=named["b"], score=0.8),
                SearchHit(item=named["c"], score=0.7),
            ]
        ),
        llm,
        memory=memory,
        items_repo=repo,
        conflict_leg_limit=6,  # 3 relations, 6 rows: no truncation, no note
    )
    service.ask("מה עם הטיול?", now=NOW)
    prompt = llm.prompts[-1]
    a, b, c = named["a"].id, named["b"].id, named["c"].id
    assert f"⚠️ conflicts with: [{b}] [{c}]" in prompt, "a's list keeps leg order"
    assert f"⚠️ conflicts with: [{a}] [{c}]" in prompt, "b's list keeps leg order"
    assert f"⚠️ conflicts with: [{b}] [{a}]" in prompt, (
        "c's list keeps the leg's original order (RED today: renders [a] [b])"
    )
    assert "NOTE:" not in prompt, "3 relations <= cap 6: no truncation line"


def test_no_truncation_note_at_the_cap_boundary(clean_items, memory_db) -> None:
    """Negative (review TESTS): 4 relations == the cap -> nothing hidden ->
    NO NOTE line (the note appears only when the cap left live conflicts
    out)."""
    memory, repo, session = _world()
    a = repo.add(MemoryItem(raw_text="מקור: הפגישה בשעה 10:00"))
    others = [
        repo.add(MemoryItem(raw_text=f"גרסה {n}: הפגישה בשעה {n}:00")) for n in range(4)
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
    assert len(result.sources) == 5, "all 4 counterparts pulled, cap exactly met"
    assert "NOTE:" not in llm.prompts[-1] and "not shown" not in llm.prompts[-1], (
        "4 relations <= cap 4: no truncation line"
    )


def test_no_kid_ask_takes_no_extra_savepoint(clean_items, memory_db) -> None:
    """RED (MINOR 4): the kid leg's savepoints wrapped the whole leg in
    ask() — every ask paid SAVEPOINT round trips even when no kid was
    named. The savepoint belongs inside _kid_recall_items, AFTER the
    kid-mention check: a no-kid ask takes none (the conflict leg's own
    single savepoint is pre-existing)."""
    memory, repo, session = _world()
    _seed_itamar(memory)
    hit = repo.add(MemoryItem(raw_text="מכתב הנהלה על תשלומים"))
    repo.commit()
    calls: list[int] = []
    orig = session.begin_nested

    def spy():
        calls.append(1)
        return orig()

    session.begin_nested = spy  # type: ignore[method-assign]
    service = AskService(
        RecordingSearch([SearchHit(item=hit, score=0.9)]),
        FakeLLMClient(reply=lambda p: "ok"),  # type: ignore[arg-type]
        memory=memory,
        items_repo=repo,
    )
    result = service.ask("מה עם התשלומים?", now=NOW)  # no kid named
    assert [str(h.item.id) for h in result.sources] == [str(hit.id)], (
        "the ask still answers"
    )
    assert len(calls) == 1, (
        f"the no-kid ask must take exactly the conflict leg's savepoint, "
        f"took {len(calls)}"
    )


def test_second_session_kid_leg_still_savepointed(
    clean_items, memory_db
) -> None:
    """MINOR 4: with the savepoints moved inside _kid_recall_items, a kid
    ask whose memory repo and items repo sit on DIFFERENT sessions must
    still savepoint BOTH — the dedupe is by session identity, never 'skip
    the second session'."""
    _wipe()
    s1 = SessionLocal()
    _LEAKED.append(s1)
    s2 = SessionLocal()
    _LEAKED.append(s2)
    memory = MemoryService(MemoryRepository(s1))
    repo = ItemsRepository(s2)
    _seed_itamar(memory)  # entities on s1
    wa = _wa(repo, "ב' ברקת - הורים", "מחר יום כיף בבית הספר", day=21)
    repo.commit()  # items on s2
    calls: dict[str, int] = {"s1": 0, "s2": 0}
    for name, s in (("s1", s1), ("s2", s2)):
        orig = s.begin_nested

        def make_spy(orig=orig, name=name):
            def spy():
                calls[name] += 1
                return orig()

            return spy

        s.begin_nested = make_spy()  # type: ignore[method-assign]
    llm = PromptCapturingLLM()
    service = AskService(
        RecordingSearch([SearchHit(item=wa, score=0.9)]),
        llm,
        memory=memory,  # type: ignore[arg-type]
        items_repo=repo,
    )
    result = service.ask("מה איתמר צריך?", now=NOW)
    ids = [str(h.item.id) for h in result.sources]
    assert str(wa.id) in ids, "the kid leg still recalls across two sessions"
    assert calls["s1"] >= 1 and calls["s2"] >= 1, (
        f"both sessions must be savepointed by the kid leg, got {calls}"
    )
