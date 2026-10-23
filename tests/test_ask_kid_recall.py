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
                raw_text=f"[2{n // 10}/10/2026, 1{n % 10}:00] ב' ברקת - הורים / מעין: הודעה מספר {n}"
            )
        )
    search = RecordingSearch([])
    service = _service(memory, repo, search, kid_recall_limit=16)

    service.ask("מה חדש אצל איתמר?", now=datetime(2026, 10, 22, 15, 0, tzinfo=UTC))

    assert len(search.seen_extra[0]) == 16, "the extra leg must respect its cap"
