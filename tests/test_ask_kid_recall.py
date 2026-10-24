"""PR 0d tests: the kid-recall leg of /ask (memory-aware retrieval boost).

When a question names a confirmed kid, items from that kid's channels (and
the kid's class name) join the recall pool BEFORE the Jev rerank. Offline:
real MemoryService + real ItemsRepository over brain_w5_test, search stubbed.
"""

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import text as sql_text
from sqlalchemy.orm import Session
from tests.conftest import assert_test_database

from brain.ai.ask import FakeLLMClient
from brain.db.models import MemoryItem
from brain.db.session import SessionLocal, engine
from brain.repositories.items import ItemsRepository
from brain.repositories.memory import MemoryRepository
from brain.services.ask import AskService
from brain.services.memory import MemoryService
from brain.services.search import SearchHit

JLM = ZoneInfo("Asia/Jerusalem")


class RecordingSearch:
    """SearchService stand-in: fixed semantic hits + visible extra_items."""

    def __init__(self, hits: list[SearchHit]) -> None:
        self._hits = hits
        self.seen_extra: list[list[MemoryItem]] = []

    def search(
        self,
        query: str,
        *,
        limit: int,
        offset: int = 0,
        extra_items: list[MemoryItem] | None = None,
    ) -> list[SearchHit]:
        extra = list(extra_items or [])
        self.seen_extra.append(extra)
        pool = self._hits + [SearchHit(item=item, score=None) for item in extra]
        return pool[:limit]

    def by_occurrence(self, start: object, end: object, *, limit: int) -> list[SearchHit]:
        return []


def cite_every_source(prompt: str) -> str:
    """LLM stand-in: echo every source id from the prompt as a citation."""
    ids = [line[2:].split("]")[0] for line in prompt.splitlines() if line.startswith("- [")]
    return "Based on: " + " ".join(f"[{item_id}]" for item_id in ids)


def _wipe_items() -> None:
    assert_test_database(str(engine.url))
    with engine.begin() as conn:
        conn.execute(sql_text("DELETE FROM memory_items"))


# Sessions opened by _world(); closed after each test (see _close_leaked_sessions).
_LEAKED: list[Session] = []


@pytest.fixture(autouse=True)
def _close_leaked_sessions():
    """Close _world()'s session after every test.

    A leaked open transaction here used to hold locks that deadlocked the
    alembic downgrade in test_datetime_serialization (flaky full-suite hang:
    DROP TABLE waiting on an 'idle in transaction' pool connection).
    """
    yield
    while _LEAKED:
        _LEAKED.pop().close()


def _world() -> tuple[MemoryService, ItemsRepository]:
    """Real memory + items repositories over the test DB, wiped clean."""
    _wipe_items()
    session = SessionLocal()
    _LEAKED.append(session)
    memory = MemoryService(MemoryRepository(session))
    for entity in memory.overview(status="confirmed", q=None):
        memory.delete_entity(entity["id"])
    return memory, ItemsRepository(session)


def _seed_itamar_world(memory: MemoryService, repo: ItemsRepository) -> MemoryItem:
    """Itamar + his class channel ('family' channel for Rona-leak contrast)."""
    memory.add_entity("person", "Itamar", aliases=["איתמר"])
    memory.add_entity("person", "Rona")
    memory.set_kid(
        "Itamar",
        gender="boy",
        school="בראשית",
        grade="2",
        class_="ב' ברקת",
        teacher="Maayan",
    )
    memory.set_kid("Rona", gender="girl", school="בראשית", grade="5", class_="ה' ספיר")
    memory.set_channel("ב' ברקת - הורים", kind="parents", applies_to="Itamar")
    memory.set_channel("רונה כדורגל", kind="club", applies_to="Rona")  # leak guard
    memory.set_channel("כל המשפחה", kind="committee", applies_to="family")

    # the item the semantic leg can NEVER find: no 'Itamar'/'איתמר' anywhere
    class_item = repo.add(
        MemoryItem(
            raw_text="[21/10/2026, 18:23] ב' ברקת - הורים / מעין: מחר חולצה לבנה וקלמר עם סרגל",
            occurred_at=datetime(2026, 10, 21, 15, 23, tzinfo=UTC),
            meta={
                "count": 1,
                "messages": [
                    {
                        "sender": "ב' ברקת - הורים / מעין",
                        "at": "2026-10-21T15:23:00+00:00",
                        "at_raw": "21/10/2026, 18:23",
                    }
                ],
            },
        )
    )
    return class_item


def _service(
    memory: MemoryService,
    repo: ItemsRepository,
    search: RecordingSearch,
    *,
    kid_recall_limit: int = 16,
) -> AskService:
    return AskService(
        search,
        FakeLLMClient(reply=cite_every_source),  # type: ignore[arg-type]
        memory=memory,
        items_repo=repo,
        kid_recall_limit=kid_recall_limit,
    )


def test_kid_question_recalls_channel_item_without_kid_name(clean_items, memory_db) -> None:
    memory, repo = _world()
    _seed_itamar_world(memory, repo)
    search = RecordingSearch([])  # semantic leg finds nothing (the failure mode)
    service = _service(memory, repo, search)

    result = service.ask(
        "מה איתמר צריך להביא מחר לבית ספר?", now=datetime(2026, 10, 22, 15, 0, tzinfo=UTC)
    )

    assert len(search.seen_extra) == 1 and search.seen_extra[0], "kid leg must feed the pool"
    assert any(
        "איתמר" not in hit.item.raw_text and "Itamar" not in hit.item.raw_text
        for hit in result.sources
    ), "the recalled item must not contain the kid's name"


def test_family_channel_recalled_for_named_kid(clean_items, memory_db) -> None:
    memory, repo = _world()
    _seed_itamar_world(memory, repo)
    family_item = repo.add(
        MemoryItem(
            raw_text="[20/10/2026, 09:00] כל המשפחה / אבא: קמפינג בערב הבחירות בהר עגול",
            occurred_at=datetime(2026, 10, 20, 6, 0, tzinfo=UTC),
        )
    )
    search = RecordingSearch([])
    service = _service(memory, repo, search)

    result = service.ask(
        "מה קורה בערב הבחירות, איתמר?", now=datetime(2026, 10, 22, 15, 0, tzinfo=UTC)
    )

    assert str(family_item.id) in [str(hit.item.id) for hit in result.sources]


def test_no_kid_named_leaves_pool_unchanged(clean_items, memory_db) -> None:
    memory, repo = _world()
    _seed_itamar_world(memory, repo)
    semantic_hit = repo.add(MemoryItem(raw_text="הקמפינג של כיתת ב' ברקת בהר עגול"))
    search = RecordingSearch([SearchHit(item=semantic_hit, score=0.9)])
    service = _service(memory, repo, search)

    result = service.ask("מה קורה בערב הבחירות?", now=datetime(2026, 10, 22, 15, 0, tzinfo=UTC))

    assert search.seen_extra == [[]], "no kid named -> no extra leg"
    assert [str(hit.item.id) for hit in result.sources] == [str(semantic_hit.id)]
    # and with memory disabled entirely, the very same result
    bare = _service(None, None, RecordingSearch([SearchHit(item=semantic_hit, score=0.9)]))
    bare_result = bare.ask("מה קורה בערב הבחירות?", now=datetime(2026, 10, 22, 15, 0, tzinfo=UTC))
    assert [str(hit.item.id) for hit in bare_result.sources] == [
        str(hit.item.id) for hit in result.sources
    ]


def test_other_kids_channels_never_leak(clean_items, memory_db) -> None:
    memory, repo = _world()
    _seed_itamar_world(memory, repo)
    rona_item = repo.add(
        MemoryItem(
            raw_text="[19/10/2026, 17:00] רונה כדורגל / המאמנת: אימון רביעי 15:15 באשכולות",
            occurred_at=datetime(2026, 10, 19, 14, 0, tzinfo=UTC),
        )
    )
    search = RecordingSearch([])
    service = _service(memory, repo, search)

    result = service.ask(
        "איתמר צריך חולצה לבנה מחר?", now=datetime(2026, 10, 22, 15, 0, tzinfo=UTC)
    )

    assert str(rona_item.id) not in [str(hit.item.id) for hit in result.sources]


def test_kid_recall_cap_respected(clean_items, memory_db) -> None:
    memory, repo = _world()
    _seed_itamar_world(memory, repo)
    for n in range(20):
        repo.add(
            MemoryItem(
                raw_text=(
                    f"[2{n // 10}/10/2026, 1{n % 10}:00] ב' ברקת - הורים / מעין: הודעה מספר {n}"
                )
            )
        )
    search = RecordingSearch([])
    service = _service(memory, repo, search, kid_recall_limit=16)

    service.ask("מה חדש אצל איתמר?", now=datetime(2026, 10, 22, 15, 0, tzinfo=UTC))

    assert len(search.seen_extra[0]) == 16, "the extra leg must respect its cap"


def test_kid_name_matches_word_boundaries_only(clean_items, memory_db) -> None:
    """'May' inside 'mayor' (or 'שחר' inside 'שחרור') must NOT name a kid.

    Substring matching is too loose: a kid whose name appears as a proper
    sub-word of another word in the question must not widen the pool.
    """
    memory, repo = _world()
    _seed_itamar_world(memory, repo)  # unrelated world so only May can fire
    memory.add_entity("person", "May")
    memory.set_kid("May", gender="girl", school="בראשית", grade="3", class_="ג' שקד")
    memory.set_channel("ג' שקד - הורים", kind="parents", applies_to="May")
    repo.add(MemoryItem(
        raw_text="[21/10/2026, 18:00] ג' שקד - הורים / המחנכת: מחר יום ספור",
        occurred_at=datetime(2026, 10, 21, 15, 0, tzinfo=UTC),
    ))
    search = RecordingSearch([])
    service = _service(memory, repo, search)

    service.ask(
        "When is the mayor visiting the school?", now=datetime(2026, 10, 22, 15, 0, tzinfo=UTC)
    )
    assert not search.seen_extra[-1], "'may' inside 'mayor' must not fire the kid leg"

    service.ask("מה חדש עם השחרור הזה?", now=datetime(2026, 10, 22, 15, 0, tzinfo=UTC))
    assert not search.seen_extra[-1], "a Hebrew sub-word must not fire the kid leg"

    service.ask("What did May bring to school?", now=datetime(2026, 10, 22, 15, 0, tzinfo=UTC))
    assert search.seen_extra[-1], "the real name as a whole word still fires"


def test_hebrew_prefixes_still_name_a_kid(clean_items, memory_db) -> None:
    """Hebrew glues prefixes onto words: 'לאיתמר'/'ואיתמר'/'שלאיתמר' are all
    questions that NAME Itamar, so the prefix run [והבכלמש]{0,3} must be
    allowed before the name — while 'שחר' inside 'שחרור' still must not fire
    (the word continues with a letter, the name does not end there).
    """
    memory, repo = _world()
    _seed_itamar_world(memory, repo)
    memory.add_entity("person", "Shachar", aliases=["שחר"])
    memory.set_kid("Shachar", gender="boy", school="בראשית", grade="4", class_="ד' נוף")
    memory.set_channel("שחר חוג כדורסל", kind="club", applies_to="Shachar")
    repo.add(MemoryItem(
        raw_text="[21/10/2026, 18:30] שחר חוג כדורסל / המאמן: מחר מתאמנים בדווקא בבוקר",
        occurred_at=datetime(2026, 10, 21, 15, 30, tzinfo=UTC),
    ))
    search = RecordingSearch([])
    service = _service(memory, repo, search)

    for question in (
        "מה צריך לאיתמר מחר?",       # single prefix ל
        "ואיתמר, מה הוא צריך?",       # single prefix ו
        "האם שלאיתמר יש מחר משהו?",   # prefix combo של
    ):
        service.ask(question, now=datetime(2026, 10, 22, 15, 0, tzinfo=UTC))
        assert search.seen_extra[-1], f"'{question}' must fire the kid leg"

    service.ask("מה חדש עם השחרור הזה?", now=datetime(2026, 10, 22, 15, 0, tzinfo=UTC))
    assert not search.seen_extra[-1], "prefixes must not resurrect the שחרור false positive"

    service.ask("מה עם שחר בחוג מחר?", now=datetime(2026, 10, 22, 15, 0, tzinfo=UTC))
    assert search.seen_extra[-1], "the bare alias as a whole word still fires"


def test_by_text_any_excludes_dropped_children(clean_items, memory_db) -> None:
    """Repo contract (design item 19): dropped extracted children are never
    recalled; ordinary captures, proposed and kept children are."""
    memory, repo = _world()
    _seed_itamar_world(memory, repo)
    for state in [None, "proposed", "kept", "dropped"]:
        repo.add(MemoryItem(
            raw_text="[21/10/2026, 19:00] ג' שקד - הורים / המחנכת: טיול שנתי בטבעון",
            occurred_at=datetime(2026, 10, 21, 16, 0, tzinfo=UTC),
            child_state=state,
        ))

    hits = repo.by_text_any(["טיול שנתי בטבעון"], limit=16)

    assert len(hits) == 3, "the dropped child must not be recalled"
    assert all((hit.child_state or None) != "dropped" for hit in hits)


# --- WA39 acceptance run 1 (24 Oct): the newest-N flood -----------------------

_TEACHER = "ב' ברקת - הורים"


def _whatsapp_item(
    repo: ItemsRepository,
    channel: str,
    day: int,
    hour: int,
    text: str,
    *,
    captured: int = 23,
) -> MemoryItem:
    """A WhatsApp conversation item in the REAL capture shape: the channel is
    in the raw_text header AND in meta.channel (services/whatsapp.py
    render_conversation). ``captured`` pins created_at (the capture day) so
    the newest-first pool order is deterministic — same-transaction inserts
    would otherwise share one now()."""
    return repo.add(
        MemoryItem(
            source="whatsapp",
            raw_text=f"[2{day}/10/2026, {hour}:00] {channel} / מעין: {text}",
            occurred_at=datetime(2026, 10, day, hour - 2, 0, tzinfo=UTC),
            created_at=datetime(2026, 10, captured, 12, 0, tzinfo=UTC),
            meta={
                "kind": "whatsapp_conversation",
                "channel": channel,
                "count": 1,
                "messages": [
                    {
                        "sender": f"{channel} / מעין",
                        "at": f"2026-10-{day}T{hour - 2}:00:00+00:00",
                        "at_raw": f"2{day}/10/2026, {hour}:00",
                    }
                ],
            },
        )
    )


def test_farm_conversation_recalled_despite_newer_flood(clean_items, memory_db) -> None:
    """WA39 run-1 miss, red-first: a question that names a kid AND resolves a
    date ('Wednesday 21.10') must recall the kid's-channel conversation from
    THAT day even when 20 newer conversations in the same channels already
    fill the newest-16 pool. The kid leg is bounded by the question's
    resolved time window when one resolves (the temporal leg's own
    resolution), else falls back to the newest-N cap — never all history."""
    memory, repo = _world()
    _seed_itamar_world(memory, repo)
    # the target: the teacher-channel conversation ABOUT the farm day —
    # no kid name anywhere in its text (the semantic leg cannot find it)
    farm = _whatsapp_item(
        repo, _TEACHER, 21, 10, "מחר הכיתה בחווה החקלאית — צריך כובע ומים", captured=21
    )
    # the flood: 20 NEWER conversations in the kid's own channels (captured
    # later, so the current newest-first cap serves them, not the farm item)
    for n in range(20):
        _whatsapp_item(
            repo, _TEACHER, 23, 8 + n % 12, f"הודעה שנייה־עשרים מספר {n}"
        )
    rona_item = _whatsapp_item(
        repo, "רונה כדורגל", 21, 9, "אימון הכדורגל של רונה מחר בשעה 15:15"
    )
    search = RecordingSearch([])  # semantic leg finds nothing (the failure mode)
    service = _service(memory, repo, search)

    result = service.ask(
        "Where was Itamar's class on Wednesday 21.10?",
        now=datetime(2026, 10, 24, 9, 0, tzinfo=UTC),
    )

    recalled = [str(hit.item.id) for hit in result.sources]
    assert str(farm.id) in recalled, (
        "the kid's-channel conversation inside the question's resolved window "
        "must join the recall pool even when newer items fill the newest-N cap"
    )
    assert str(rona_item.id) not in recalled, (
        "another kid's channel must never be recalled for this kid's question"
    )


def test_kid_channel_recall_without_resolved_window_is_recent_n(clean_items, memory_db) -> None:
    """No date in the question -> the kid leg falls back to the newest-N cap
    (never all history) and still respects the overall pool cap."""
    memory, repo = _world()
    _seed_itamar_world(memory, repo)
    old = _whatsapp_item(repo, _TEACHER, 9, 10, "שיחה ישנה מתחילת החודש")
    for n in range(20):
        _whatsapp_item(repo, _TEACHER, 23, 8 + n % 12, f"הודעה חדשה מספר {n}")
    search = RecordingSearch([])
    service = _service(memory, repo, search, kid_recall_limit=16)

    service.ask("מה חדש אצל איתמר?", now=datetime(2026, 10, 24, 9, 0, tzinfo=UTC))

    extra_ids = [str(item.id) for item in search.seen_extra[0]]
    assert len(extra_ids) == 16, "the pool cap holds"
    assert str(old.id) not in extra_ids, "no window -> recent N, never all history"


def test_no_channel_facts_keeps_the_kid_leg_text_only(clean_items, memory_db) -> None:
    """No confirmed applies_to fact -> the structured channel leg adds
    nothing (it must not guess); with the fact present the same question
    recalls the channel item. The channel name here deliberately does NOT
    contain the kid's class string, so the class text needle cannot
    accidentally recall the item."""
    memory, repo = _world()
    _seed_itamar_world(memory, repo)
    memory.set_channel("קשר עם המחנך", kind="teacher", applies_to="Itamar")
    item = _whatsapp_item(repo, "קשר עם המחנך", 21, 10, "מחר הכיתה בחווה החקלאית", captured=21)
    search = RecordingSearch([])
    service = _service(memory, repo, search)

    result = service.ask(
        "Where was Itamar's class on Wednesday 21.10?",
        now=datetime(2026, 10, 24, 9, 0, tzinfo=UTC),
    )
    assert str(item.id) in [str(hit.item.id) for hit in result.sources], (
        "the confirmed channel fact must recall the channel's conversation"
    )

    # the fact is withdrawn: the channel leg must stop guessing
    for entity in memory.overview(status="confirmed", q=None):
        if entity.get("kind") != "channel":
            continue
        for fact in entity.get("facts", []):
            if fact.get("predicate") == "applies_to":
                memory.delete_fact(fact["id"])
    search2 = RecordingSearch([])
    service2 = _service(memory, repo, search2)
    service2.ask(
        "Where was Itamar's class on Wednesday 21.10?",
        now=datetime(2026, 10, 24, 9, 0, tzinfo=UTC),
    )
    assert all(str(item.id) != str(i.id) for i in search2.seen_extra[0]), (
        "without confirmed channel facts the channel leg must not guess"
    )


def test_by_channels_hides_confirmed_cancellation_until_restored(clean_items) -> None:
    """Follow-up (3): by_channels routes through default_hidden_conditions —
    a confirmed-cancelled channel conversation is NOT recalled; the restore
    path (ReconciliationService.restore_cancellation) brings it back. One
    hiding predicate everywhere (B3), no by_channels-specific holes."""
    from brain.db.models import ItemRelation
    from brain.services.extraction.reconciliation import ReconciliationService

    _wipe_items()
    session = SessionLocal()
    _LEAKED.append(session)
    repo = ItemsRepository(session)
    normal = repo.add(
        MemoryItem(
            raw_text="[21/10/2026, 18:20] ב' ברקת - הורים / מעין: מחר ספרייה",
            source="whatsapp",
            meta={"channel": "ב' ברקת - הורים"},
            occurred_at=datetime(2026, 10, 21, 15, 20, tzinfo=UTC),
        )
    )
    cancelled = repo.add(
        MemoryItem(
            raw_text="[21/10/2026, 19:00] ב' ברקת - הורים / מעין: בוטל הטיול",
            source="whatsapp",
            meta={"channel": "ב' ברקת - הורים"},
            occurred_at=datetime(2026, 10, 21, 16, 0, tzinfo=UTC),
        )
    )
    session.commit()
    # the cancellation is CONFIRMED the way the pipeline stamps it: a
    # cancels relation + the cancelled dict citing it
    rel = ItemRelation(
        kind="cancels", from_id=normal.id, to_id=cancelled.id,
        provenance={"state": "applied"},
    )
    session.add(rel)
    session.commit()
    stamped = session.get(MemoryItem, cancelled.id)
    stamped.extraction_meta = {
        **(stamped.extraction_meta or {}),
        "cancelled": {"relation_id": str(rel.id)},
    }
    session.commit()

    def _ids() -> set[str]:
        return {
            str(item.id)
            for item in repo.by_channels(["ב' ברקת - הורים"], limit=10)
        }

    # RED: today by_channels still returns the confirmed-cancelled row
    assert str(normal.id) in _ids()
    assert str(cancelled.id) not in _ids()

    # the real restore path re-shows it
    service = ReconciliationService(session, repo, tz_name="Asia/Jerusalem")
    service.restore_cancellation(rel.id)
    assert str(cancelled.id) in _ids()

def test_announcement_sent_before_the_window_is_recalled_via_lookback(
    clean_items, memory_db
) -> None:
    """WA39 run-3 miss, red-first: the answer to a question about day D is
    often an announcement SENT on D-1 ('tomorrow the class is at the farm').
    The kid-channel leg bounds by occurred_at = the SENT time, so a strict
    [window_start, window_end) bound drops the announcement that arrived the
    evening before. With a resolved window the leg must widen its start by
    KID_RECALL_LOOKBACK_DAYS (default 3) — and slot priority is in-window >
    lookback > newest flood."""
    memory, repo = _world()
    _seed_itamar_world(memory, repo)
    # the announcement: sent Tue 20.10 evening (18:00 UTC = 21:00 Jerusalem),
    # one hour BEFORE the 'Wednesday 21.10' window opens at 20.10 21:00 UTC —
    # no kid name in its text (the semantic leg cannot find it)
    announce = _whatsapp_item(
        repo, _TEACHER, 20, 18, "מחר (יום ד') הכיתה בחווה החקלאית — צריך כובע ומים",
        captured=20,
    )
    # the flood: 16 newer same-channel conversations fill the whole cap
    for n in range(16):
        _whatsapp_item(repo, _TEACHER, 23, 8 + n % 12, f"הודעה מספר {n}")
    search = RecordingSearch([])
    service = _service(memory, repo, search)

    result = service.ask(
        "Where was Itamar's class on Wednesday 21.10?",
        now=datetime(2026, 10, 24, 9, 0, tzinfo=UTC),
    )

    recalled = [str(hit.item.id) for hit in result.sources]
    assert str(announce.id) in recalled, (
        "a kid's-channel announcement sent just before the resolved window "
        "(lookback days) must be recalled for the question about day D"
    )


def test_lookback_respects_its_days_bound(clean_items, memory_db) -> None:
    """The lookback is bounded: a conversation sent more than
    KID_RECALL_LOOKBACK_DAYS before the window opens stays out of the pool
    (never all history)."""
    memory, repo = _world()
    _seed_itamar_world(memory, repo)
    old = _whatsapp_item(
        repo, _TEACHER, 16, 10, "שיעור בחווה החקלאית ביום ד' הקרוב", captured=16
    )
    # flood to fill the cap so only the lookback/structured legs could add it
    for n in range(16):
        _whatsapp_item(repo, _TEACHER, 23, 8 + n % 12, f"הודעה מספר {n}")
    search = RecordingSearch([])
    service = _service(memory, repo, search)

    result = service.ask(
        "Where was Itamar's class on Wednesday 21.10?",
        now=datetime(2026, 10, 24, 9, 0, tzinfo=UTC),
    )

    recalled = [str(hit.item.id) for hit in result.sources]
    assert str(old.id) not in recalled, (
        "beyond the lookback days bound the kid-channel leg must not recall"
    )
