"""ACCEPTANCE EVAL: calendar retrieval quality (dogfood regression guard).

The calendar dogfood graded real questions 5/10. This eval pins RETRIEVAL
(no LLM judgment): a fake LLM cites every context id it receives, so the
assertions check exactly which items the retrieval pipeline put into the
context for each question.

Fixture: the 18 calendar events from the dogfood (the user's own captured
data, kept local to this file) and the 10 graded questions. Fixed 'now' =
2026-10-22 18:30 Israel so relative dates are deterministic.

Model choice (measured against the old embedder, top-8, pure semantic):
    BAAI/bge-small-en-v1.5 (old default)          6/11
    sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2    7/11
    intfloat/multilingual-e5-large                8/11
    minishlab/potion-multilingual-128M (chosen)  10/11
The remaining miss (one of Tuesday's four events) is exactly what temporal
retrieval covers.
"""

import re
import uuid
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest
from qdrant_client import QdrantClient
from tests.fakes import FakeRepository
from tests.test_search_service import StubReranker

from brain.ai.ask import FakeLLMClient
from brain.ai.embedder import FastEmbedEmbedder
from brain.ai.vectorstore import QdrantVectorStore
from brain.db.models import MemoryItem
from brain.services.ask import AskService
from brain.services.search import SearchService

JLM = ZoneInfo("Asia/Jerusalem")
NOW = datetime(2026, 10, 22, 18, 30, tzinfo=JLM)
EVAL_MODEL = "minishlab/potion-multilingual-128M"

# These tests load the REAL fastembed model (EVAL_MODEL above): a cold-cache
# HF fetch can take minutes (incident 25 Oct — two 60s per-test timeouts on
# CI in one day). The 600s budget is scoped to THIS MODULE ONLY — the global
# --timeout=60 stays for the rest of the suite (a hung test must still fail).
pytestmark = pytest.mark.timeout(600)

# --- fixture: the 18 dogfood events (verbatim VEVENT blocks) ------------------

_EVENTS = """\
BEGIN:VEVENT
SUMMARY:צופים רונה
DTSTART;TZID=Asia/Jerusalem:20261023T160000
DTEND;TZID=Asia/Jerusalem:20261023T173000
END:VEVENT
===
BEGIN:VEVENT
SUMMARY:ארוחת שישי עם סתיו ודניאל
DTSTART;TZID=Asia/Jerusalem:20261023T190000
DTEND;TZID=Asia/Jerusalem:20261023T220000
END:VEVENT
===
BEGIN:VEVENT
SUMMARY:ארוחת צהריים עם אור
DTSTART;TZID=Asia/Jerusalem:20261024T110000
DTEND;TZID=Asia/Jerusalem:20261024T140000
END:VEVENT
===
BEGIN:VEVENT
SUMMARY:כדורגל איתמר 16:15-17:20
DTSTART;TZID=Asia/Jerusalem:20261025T161500
DTEND;TZID=Asia/Jerusalem:20261025T171500
END:VEVENT
===
BEGIN:VEVENT
SUMMARY:כדורגל לרונה
DTSTART;TZID=Asia/Jerusalem:20261025T184500
DTEND;TZID=Asia/Jerusalem:20261025T194500
END:VEVENT
===
BEGIN:VEVENT
SUMMARY:מפגש קבוצתי איתמר - מרכז דקל( מתחילים ב07:45)
DTSTART;TZID=Asia/Jerusalem:20261026T074500
DTEND;TZID=Asia/Jerusalem:20261026T084500
END:VEVENT
===
BEGIN:VEVENT
SUMMARY:הוראה מתקנת - רונה
DTSTART;TZID=Asia/Jerusalem:20261026T161500
DTEND;TZID=Asia/Jerusalem:20261026T170000
END:VEVENT
===
BEGIN:VEVENT
SUMMARY:מדורה+ קמפינג של שכבה ב
DTSTART;TZID=Asia/Jerusalem:20261026T180000
DTEND;TZID=Asia/Jerusalem:20261026T220000
END:VEVENT
===
BEGIN:VEVENT
SUMMARY:זהר באירוע מהעבודה
DTSTART;TZID=Asia/Jerusalem:20261026T183000
DTEND;TZID=Asia/Jerusalem:20261026T210000
END:VEVENT
===
BEGIN:VEVENT
SUMMARY:טיפול זוגי שבועי 19:00
DTSTART;TZID=Asia/Jerusalem:20261026T184500
DTEND;TZID=Asia/Jerusalem:20261026T203000
END:VEVENT
===
BEGIN:VEVENT
SUMMARY:שחיה יציאה מוקדמת 06:40
DTSTART;TZID=Asia/Jerusalem:20261027T080000
DTEND;TZID=Asia/Jerusalem:20261027T090000
END:VEVENT
===
BEGIN:VEVENT
SUMMARY:כדורסל לאיתמר (איילת אוספת)
DTSTART;TZID=Asia/Jerusalem:20261027T130000
DTEND;TZID=Asia/Jerusalem:20261027T140000
END:VEVENT
===
BEGIN:VEVENT
SUMMARY:צופים לרונה
DTSTART;TZID=Asia/Jerusalem:20261027T160000
DTEND;TZID=Asia/Jerusalem:20261027T173000
END:VEVENT
===
BEGIN:VEVENT
SUMMARY:כדורגל איתמר 16:15-17:20
DTSTART;TZID=Asia/Jerusalem:20261027T161500
DTEND;TZID=Asia/Jerusalem:20261027T171500
END:VEVENT
===
BEGIN:VEVENT
SUMMARY:כדורגל לרונה
DTSTART;TZID=Asia/Jerusalem:20261028T150000
DTEND;TZID=Asia/Jerusalem:20261028T163000
END:VEVENT
===
BEGIN:VEVENT
SUMMARY:שחיה יציאה מוקדמת 06:40
DTSTART;TZID=Asia/Jerusalem:20261029T080000
DTEND;TZID=Asia/Jerusalem:20261029T090000
END:VEVENT
===
BEGIN:VEVENT
SUMMARY:סיכום אבחון רונה (זום)
DTSTART;TZID=Asia/Jerusalem:20261029T100000
DTEND;TZID=Asia/Jerusalem:20261029T110000
END:VEVENT
===
BEGIN:VEVENT
SUMMARY:הוראה מתקנת - רונה (השדה 10)
DTSTART;TZID=Asia/Jerusalem:20261029T170000
DTEND;TZID=Asia/Jerusalem:20261029T173000
END:VEVENT"""

# --- the 10 graded questions ---------------------------------------------------

QUESTIONS = [
    ("Q1", "What do the kids have on Tuesday October 27?"),
    ("Q2", "When is Rona's diagnosis summary meeting, and is it on Zoom?"),
    ("Q3", "Who picks Itamar up from basketball?"),
    ("Q4", "What am I doing on Friday evening?"),
    ("Q5", "What time do I need to leave for swimming on Tuesday?"),
    ("Q6", "What is happening on Monday evening, October 26? Are there any conflicts?"),
    ("Q7", "How many times does Itamar have football this week, and when?"),
    ("Q8", "מתי ארוחת הצהריים עם אור?"),
    ("Q9", "What time is Itamar's football on Sunday October 25?"),
    ("Q10", "When is my dentist appointment?"),
]

# (summary, local date) pairs that MUST be in the context per question.
EXPECTED: dict[str, set[tuple[str, str]]] = {
    "Q1": {
        ("כדורסל לאיתמר (איילת אוספת)", "2026-10-27"),
        ("צופים לרונה", "2026-10-27"),
        ("כדורגל איתמר 16:15-17:20", "2026-10-27"),
        ("שחיה יציאה מוקדמת 06:40", "2026-10-27"),
    },
    "Q3": {("כדורסל לאיתמר (איילת אוספת)", "2026-10-27")},
    "Q4": {("ארוחת שישי עם סתיו ודניאל", "2026-10-23")},
    "Q6": {
        ("מדורה+ קמפינג של שכבה ב", "2026-10-26"),
        ("זהר באירוע מהעבודה", "2026-10-26"),
        ("טיפול זוגי שבועי 19:00", "2026-10-26"),
    },
    "Q8": {("ארוחת צהריים עם אור", "2026-10-24")},
    "Q9": {("כדורגל איתמר 16:15-17:20", "2026-10-25")},
}


def _summary_dates(hits) -> set[tuple[str, str]]:
    """(summary, local date) for each hit — the eval's matching key."""
    out: set[tuple[str, str]] = set()
    for hit in hits:
        summary = (hit.item.meta or {}).get("summary", "")
        out.add((summary, f"{hit.item.occurred_at.astimezone(JLM):%Y-%m-%d}"))
    return out


def cite_every_context(prompt: str) -> str:
    """LLM stand-in: cite every context id (retrieval-only assertions)."""
    ids = re.findall(r"- \[([0-9a-f-]{36})\]", prompt)
    return "Sources: " + " ".join(f"[{item_id}]" for item_id in ids)


def _seed_repo() -> FakeRepository:
    repo = FakeRepository()
    for block in _EVENTS.split("==="):
        summary = re.search(r"SUMMARY:(.+)", block).group(1).strip()
        dtstart = re.search(r"DTSTART;TZID=Asia/Jerusalem:(\d{8}T\d{6})", block).group(1)
        dtend = re.search(r"DTEND;TZID=Asia/Jerusalem:(\d{8}T\d{6})", block).group(1)
        start = datetime.strptime(dtstart, "%Y%m%dT%H%M%S").replace(tzinfo=JLM).astimezone(UTC)
        end = datetime.strptime(dtend, "%Y%m%dT%H%M%S").replace(tzinfo=JLM).astimezone(UTC)
        repo.add(
            MemoryItem(
                id=uuid.uuid4(),
                raw_text=block,
                source="calendar",
                occurred_at=start,
                meta={"start": start.isoformat(), "end": end.isoformat(), "summary": summary},
            )
        )
    return repo


@pytest.fixture(name="eval_service")
def eval_service_fixture() -> AskService:
    """The real retrieval pipeline over the 18 events (real embedder + qdrant)."""
    repo = _seed_repo()
    embedder = FastEmbedEmbedder(EVAL_MODEL)
    store = QdrantVectorStore(client=QdrantClient(":memory:"))
    store.ensure_collection(embedder.dim)
    items = list(repo.items.values())
    vectors = embedder.embed_batch([item.raw_text for item in items])
    store.upsert_many(
        [
            (str(item.id), vector, {"id": str(item.id), "text": item.raw_text})
            for item, vector in zip(items, vectors, strict=True)
        ]
    )
    search = SearchService(repo=repo, embedder=embedder, store=store, reranker=StubReranker())
    return AskService(search, FakeLLMClient(reply=cite_every_context), tz=JLM)


def _context(service: AskService, question: str) -> set[tuple[str, str]]:
    """(summary, local date) pairs the pipeline puts into the ask context."""
    result = service.ask(question, now=NOW)
    return _summary_dates(result.sources)


# --- the eval -------------------------------------------------------------------


@pytest.mark.parametrize(
    ("qid", "question"),
    [(qid, q) for qid, q in QUESTIONS if qid in EXPECTED],
    ids=[qid for qid, _ in QUESTIONS if qid in EXPECTED],
)
def test_retrieval_finds_the_expected_events(
    eval_service: AskService, qid: str, question: str
) -> None:
    context = _context(eval_service, question)
    missing = EXPECTED[qid] - context
    assert not missing, f"{qid}: retrieval missed {sorted(missing)}"


def test_ask_prompt_carries_now_and_when(eval_service: AskService) -> None:
    """The prompt grounds the LLM: current local time + per-source When."""
    from brain.ai.ask import build_prompt

    result = eval_service.ask("What am I doing on Friday evening?", now=NOW)
    prompt = build_prompt(
        "What am I doing on Friday evening?",
        [
            {"id": str(hit.item.id), "text": hit.item.raw_text, "when": "Fri 23 Oct 19:00-22:00"}
            for hit in result.sources
        ],
        now=NOW,
        tz=JLM,
    )
    assert "Current date/time: Thu 22 Oct 2026 18:30 (Asia/Jerusalem)" in prompt
    assert "(When: Fri 23 Oct 19:00-22:00)" in prompt
