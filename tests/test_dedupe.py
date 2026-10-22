"""Exact-duplicate guard at ingest (content fingerprint).

Fingerprint = sha256(normalized text + occurred_at UTC minute + source kind).
Normalization: NFKC, RTL-mark strip, whitespace collapse, casefold. A match
returns the EXISTING item with ``deduplicated=True`` — no new row, no
classification, no indexing. Recurring events (same title, different dates)
stay separate because occurred_at differs.
"""

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient
from qdrant_client import QdrantClient
from tests.fakes import FULL_CLASSIFICATION, FakeRepository, StubClassifier

from brain import schemas
from brain.ai.embedder import FakeEmbedder
from brain.ai.vectorstore import QdrantVectorStore
from brain.config import get_settings
from brain.main import create_app
from brain.services.dedupe import content_fingerprint, normalize_text
from brain.services.indexing import QdrantItemIndexer
from brain.services.ingest import IngestService
from brain.services.paste import parse as parse_paste

_ICS = (
    "BEGIN:VEVENT\n"
    "SUMMARY:צופים רונה\n"
    "DTSTART;TZID=Asia/Jerusalem:20261029T170000\n"
    "DTEND;TZID=Asia/Jerusalem:20261029T181500\n"
    "END:VEVENT"
)

_ICS_2 = (
    "BEGIN:VEVENT\n"
    "SUMMARY:צופים רונה\n"
    "DTSTART;TZID=Asia/Jerusalem:20261105T170000\n"
    "DTEND;TZID=Asia/Jerusalem:20261105T181500\n"
    "END:VEVENT"
)


def make_service(repo: FakeRepository | None = None) -> IngestService:
    store = QdrantVectorStore(client=QdrantClient(":memory:"))
    indexer = QdrantItemIndexer(FakeEmbedder(dim=8), store)
    return IngestService(repo or FakeRepository(), StubClassifier(FULL_CLASSIFICATION), indexer)


# ---- fingerprint primitives ---------------------------------------------------


def test_normalize_text_strips_rtl_marks_whitespace_and_case():
    raw = "\u200fHello\u200e   World\u202a\n\t X  \u202c"
    assert normalize_text(raw) == "hello world x"


def test_normalize_text_is_nfkc_and_casefold():
    assert normalize_text("ﬁngeR") == normalize_text("finger")  # NFKC folds ﬁ
    assert normalize_text("ABC") == normalize_text("abc")  # casefold


def test_fingerprint_ignores_rtl_marks_whitespace_runs_and_case():
    when = datetime(2026, 10, 29, 15, 0, tzinfo=UTC)
    base = content_fingerprint("begin: vevent", when, "calendar")
    assert base == content_fingerprint("BEGIN:\u200f\t VEVENT  ", when, "calendar")
    assert base == content_fingerprint("begin:  vevent", when, "calendar")


def test_fingerprint_separates_on_occurred_at_minute_and_source():
    when = datetime(2026, 10, 29, 15, 0, tzinfo=UTC)
    fp = content_fingerprint("same text", when, "calendar")
    assert fp != content_fingerprint("same text", when + timedelta(minutes=1), "calendar")
    assert fp != content_fingerprint("same text", when, "note")
    assert fp != content_fingerprint("same text", None, "calendar")


def test_fingerprint_uses_utc_minute_regardless_of_offset():
    when_utc = datetime(2026, 10, 29, 15, 0, tzinfo=UTC)
    # Jerusalem is UTC+2 on Oct 29 2026 (DST ended Oct 25)
    same_instant = datetime(2026, 10, 29, 17, 0, tzinfo=ZoneInfo("Asia/Jerusalem"))
    assert content_fingerprint("t", when_utc, "calendar") == content_fingerprint(
        "t", same_instant, "calendar"
    )


# ---- guard behaviour (unit, FakeRepository) ------------------------------------


def test_same_block_twice_creates_one_item_and_returns_existing():
    repo = FakeRepository()
    service = make_service(repo)

    first = service.capture_parsed(parse_paste(_ICS, kind_hint="calendar"))
    second = service.capture_parsed(parse_paste(_ICS, kind_hint="calendar"))

    assert len(repo.items) == 1  # no new row
    assert first[0].id == second[0].id  # the EXISTING item is returned
    assert second[0].deduplicated is True
    assert first[0].deduplicated is False


def test_recurring_same_title_different_dates_stays_separate():
    repo = FakeRepository()
    service = make_service(repo)

    service.capture_parsed(parse_paste(_ICS, kind_hint="calendar"))
    service.capture_parsed(parse_paste(_ICS_2, kind_hint="calendar"))

    assert len(repo.items) == 2


def test_whitespace_rtl_case_variants_dedupe_to_existing():
    repo = FakeRepository()
    service = make_service(repo)
    # plain text: whitespace runs, RTL marks, and case are copy artifacts and
    # fold away. (ICS text is different: whitespace is unfolding syntax, so a
    # re-indented ICS is legitimately another document.)
    base = "פגישת צוות ב־10:00"
    variant = "\u200f  פגישת\t צוות\n ב־10:00  \u202a"

    service.create(schemas.ItemCreate(raw_text=base, source="note", occurred_at=None))
    again = service.create(
        schemas.ItemCreate(raw_text=variant, source="note", occurred_at=None)
    )

    assert len(repo.items) == 1
    assert again.deduplicated is True


def test_deduped_capture_skips_classification_and_indexing():
    classifier = StubClassifier(FULL_CLASSIFICATION)
    repo = FakeRepository()
    store = QdrantVectorStore(client=QdrantClient(":memory:"))
    service = IngestService(repo, classifier, QdrantItemIndexer(FakeEmbedder(dim=8), store))
    service.capture_parsed(parse_paste(_ICS, kind_hint="calendar"))
    classifier.calls.clear()

    service.capture_parsed(parse_paste(_ICS, kind_hint="calendar"))

    assert classifier.calls == []  # classification not re-run


def test_different_source_is_not_a_duplicate():
    repo = FakeRepository()
    service = make_service(repo)
    service.create(
        schemas.ItemCreate(raw_text="same text", source="calendar", occurred_at=None)
    )
    service.create(schemas.ItemCreate(raw_text="same text", source="note", occurred_at=None))
    assert len(repo.items) == 2


def test_different_occurred_at_is_not_a_duplicate():
    repo = FakeRepository()
    service = make_service(repo)
    when = datetime(2026, 10, 29, 15, 0, tzinfo=UTC)
    service.create(
        schemas.ItemCreate(raw_text="same text", source="note", occurred_at=when)
    )
    service.create(
        schemas.ItemCreate(
            raw_text="same text", source="note", occurred_at=when + timedelta(days=7)
        )
    )
    assert len(repo.items) == 2


# ---- PATCH keeps the fingerprint fresh -----------------------------------------


def test_patch_raw_text_recomputes_fingerprint():
    from brain.services.items import ItemsService

    repo = FakeRepository()
    service = make_service(repo)
    item = service.create(schemas.ItemCreate(raw_text="original text", source="note"))

    ItemsService(repo).update(item.id, schemas.ItemUpdate(raw_text="edited text"))

    stored = repo.items[item.id]
    assert stored.raw_text == "edited text"
    assert stored.fingerprint == content_fingerprint(
        "edited text", stored.occurred_at, "note"
    )


def test_patch_source_change_recomputes_fingerprint():
    from brain.services.items import ItemsService

    repo = FakeRepository()
    service = make_service(repo)
    item = service.create(schemas.ItemCreate(raw_text="same text", source="note"))
    before = item.fingerprint

    ItemsService(repo).update(item.id, schemas.ItemUpdate(source="calendar"))

    stored = repo.items[item.id]
    assert stored.fingerprint != before
    assert stored.fingerprint == content_fingerprint(
        "same text", stored.occurred_at, "calendar"
    )


def test_edited_item_guards_new_text_not_old():
    from brain.services.items import ItemsService

    repo = FakeRepository()
    ingest = make_service(repo)
    item = ingest.create(schemas.ItemCreate(raw_text="original text", source="note"))
    ItemsService(repo).update(item.id, schemas.ItemUpdate(raw_text="edited text"))

    again = ingest.create(schemas.ItemCreate(raw_text="edited text", source="note"))
    assert again.id == item.id  # new text is now guarded by the edited item
    assert again.deduplicated is True

    old = ingest.create(schemas.ItemCreate(raw_text="original text", source="note"))
    assert old.id != item.id  # the stale fingerprint no longer matches
    assert len(repo.items) == 2


# ---- e2e over the real repository (needs Postgres) ------------------------------


@pytest.fixture(name="jerusalem_client")
def jerusalem_client_fixture(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    """A client whose TIMEZONE is pinned (zone-less pastes -> aware UTC)."""
    monkeypatch.setenv("TIMEZONE", "Asia/Jerusalem")
    get_settings.cache_clear()
    yield TestClient(create_app())
    get_settings.cache_clear()


@pytest.mark.usefixtures("clean_items")
def test_capture_same_ics_twice_returns_existing_item(jerusalem_client: TestClient) -> None:
    first = jerusalem_client.post("/capture", json={"text": _ICS, "kind_hint": "auto"})
    assert first.status_code == 201
    second = jerusalem_client.post("/capture", json={"text": _ICS, "kind_hint": "auto"})
    assert second.status_code == 201

    items_after = jerusalem_client.get("/items?limit=200").json()
    assert len(items_after) == 1  # exactly one item despite two captures
    body = second.json()
    assert body["items"][0]["deduplicated"] is True
    assert body["items"][0]["id"] == first.json()["items"][0]["id"]


@pytest.mark.usefixtures("clean_items")
def test_capture_recurring_same_title_two_dates_creates_two_items(
    jerusalem_client: TestClient,
) -> None:
    for ics in (_ICS, _ICS_2):
        response = jerusalem_client.post("/capture", json={"text": ics, "kind_hint": "auto"})
        assert response.status_code == 201
    assert len(jerusalem_client.get("/items?limit=200").json()) == 2


@pytest.mark.usefixtures("clean_items")
def test_patch_then_capture_of_new_text_dedupes_to_edited_item(
    jerusalem_client: TestClient,
) -> None:
    created = jerusalem_client.post(
        "/items", json={"raw_text": "original text", "source": "note"}
    )
    assert created.status_code == 201
    patched = jerusalem_client.patch(
        f"/items/{created.json()['id']}", json={"raw_text": "edited text"}
    )
    assert patched.status_code == 200

    capture = jerusalem_client.post("/capture", json={"text": "edited text"})
    assert capture.status_code == 201
    body = capture.json()
    assert len(jerusalem_client.get("/items?limit=200").json()) == 1
    assert body["items"][0]["id"] == created.json()["id"]
    assert body["items"][0]["deduplicated"] is True
