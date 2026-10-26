"""WhatsApp groups on a person's profile (B1+B3) — API contract, tests first.

GET /memory/entities/{id}/groups projects a KID's own groups: per channel,
the newest 5 RELEVANT conversations (triage_label == 'relevant' — noise AND
untriaged excluded; superseded/cancelled/dropped too, via the shared hidden
predicate), each with a preview (first 280 chars, whitespace-collapsed) and
kept_fact_id when already kept on this person. Family channels
belong to every kid (the existing applies_to semantics).

POST /memory/entities/{id}/groups/keep {item_id} saves a confirmed 'note'
fact on THAT profile (the one being viewed), provenance {how: 'group-keep',
item_id, channel, chat_jid}; 409 on a double keep and on a conversation
outside this person's groups' relevant set. Undo = the existing soft DELETE
+ /restore.

by_channels keeps its default (untriaged pass — the /recent semantics);
relevant_only=True is opt-in. No migration.
"""

import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from tests.conftest import assert_test_database

from brain.config import get_settings
from brain.db.models import MemoryItem
from brain.db.session import SessionLocal, engine
from brain.main import create_app
from brain.repositories.items import ItemsRepository
from brain.repositories.memory import MemoryRepository
from brain.services.memory import MemoryService
from brain.services.triage import TriageResult

_ENDPOINT = "/memory"
_NOA_CHANNEL = "ה' ספיר - הורים"
_ITAMAR_CHANNEL = "ב' ברקת - הורים"
_FAMILY_CHANNEL = "כל המשפחה"


@pytest.fixture(name="groups_client")
def groups_client_fixture(
    monkeypatch: pytest.MonkeyPatch, clean_memory: None,
) -> Iterator[TestClient]:
    monkeypatch.setenv("JEV_API_KEY", "")
    get_settings.cache_clear()
    # these tests seed the SHARED *_test database directly (items + entities),
    # so wipe all memory-layer rows BEFORE too — items are not covered by
    # clean_memory, and same-named channels from a previous test would leak
    # into this test's newest-5 projection.
    assert_test_database(str(engine.url))
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM memory_items"))
        conn.execute(text("DELETE FROM memory_facts"))
        conn.execute(text("DELETE FROM entity_aliases"))
        conn.execute(text("DELETE FROM entities"))
    yield TestClient(create_app())
    get_settings.cache_clear()


@contextmanager
def _seed_scope():
    """A real (memory, items) repo pair over the test DB for seeding.

    Commits on clean exit: the API under test runs its own session, so a
    flush alone (service/repo internals) would leave the rows invisible."""
    with SessionLocal() as db:
        yield MemoryService(MemoryRepository(db)), ItemsRepository(db)
        db.commit()


def _conversation(items_repo: ItemsRepository, channel: str, day: int, hour: int,
                  text: str) -> MemoryItem:
    """A WhatsApp conversation in the REAL capture shape (services/whatsapp.py
    render_conversation writes meta.channel + chat_jid + the raw header).
    Distinct occurred_at keeps newest-first deterministic."""
    return items_repo.add(
        MemoryItem(
            source="whatsapp",
            raw_text=f"[2{day}/10/2026, {hour}:00] {channel} / מעין: {text}",
            occurred_at=datetime(2026, 10, day, hour, 0, tzinfo=UTC),
            created_at=datetime(2026, 10, 23, 12, 0, tzinfo=UTC),
            meta={
                "kind": "whatsapp_conversation",
                "channel": channel,
                "chat_jid": "1204@g.us",
                "count": 1,
                "messages": [{"sender": "מעין", "at": f"2026-10-2{day}T{hour}:00:00+00:00"}],
            },
        )
    )


def _relevant(items_repo: ItemsRepository, item: MemoryItem) -> None:
    items_repo.apply_triage(
        item, TriageResult(label="relevant", confidence=1.0, method="jev", version="t1")
    )


def _entity_id(client: TestClient, name: str) -> str:
    for entity in client.get(_ENDPOINT).json()["entities"]:
        if entity["name"] == name:
            return entity["id"]
    raise AssertionError(f"no entity named {name!r}")


def _groups(client: TestClient, entity_id: str) -> dict:
    response = client.get(f"{_ENDPOINT}/entities/{entity_id}/groups")
    assert response.status_code == 200, response.text
    return response.json()



def _conversations(client: TestClient, kid: str) -> list[dict]:
    payload = _groups(client, _entity_id(client, kid))
    return [c for g in payload["groups"] for c in g["conversations"]]


# --- the projection (B1) -------------------------------------------------------


def test_groups_list_relevant_conversations_newest_first(
    groups_client: TestClient,
) -> None:
    """5 relevant + 1 noise + 1 untriaged + 1 superseded: exactly the 5
    relevant ones, newest first; per-group cap; no noise/untriaged/sha."""
    with _seed_scope() as (memory, items):
        memory.set_kid("Noa", gender="girl", school="בראשית", grade="5", class_="ה' ספיר")
        memory.set_channel(_NOA_CHANNEL, kind="parents", applies_to="Noa")
        for i, (day, hour) in enumerate([(21, 18), (22, 17), (23, 16), (24, 15), (25, 14)]):
            item = _conversation(items, _NOA_CHANNEL, day, hour, f"הודעה {i + 1}")
            _relevant(items, item)
        noise = _conversation(items, _NOA_CHANNEL, 20, 5, "ספאם")
        items.apply_triage(
            noise, TriageResult(label="noise", confidence=1.0, method="jev", version="t1")
        )
        _conversation(items, _NOA_CHANNEL, 20, 4, "עוד לא מסווג")  # untriaged: excluded
        superseded = _conversation(items, _NOA_CHANNEL, 20, 3, "העתק ישן")
        _relevant(items, superseded)
        newer = _conversation(items, _NOA_CHANNEL, 26, 10, "העתק חדש")
        _relevant(items, newer)
        superseded.superseded_by = newer.id
        items._db.flush()

    payload = _groups(groups_client, _entity_id(groups_client, "Noa"))
    assert len(payload["groups"]) == 1
    group = payload["groups"][0]
    assert group["channel"] == _NOA_CHANNEL
    convs = group["conversations"]
    assert len(convs) == 5, convs
    occurred = [c["occurred_at"] for c in convs]
    assert occurred == sorted(occurred, reverse=True), occurred
    joined = " ".join(c["preview"] for c in convs)
    assert "ספאם" not in joined and "עוד לא מסווג" not in joined and "העתק ישן" not in joined
    for c in convs:
        assert len(c["preview"]) <= 280
        assert c["kept_fact_id"] is None
        assert c["item_id"]


def test_preview_is_whitespace_collapsed(groups_client: TestClient) -> None:
    with _seed_scope() as (memory, items):
        memory.set_kid("Noa", gender="girl", school="בראשית", grade="5")
        memory.set_channel(_NOA_CHANNEL, kind="parents", applies_to="Noa")
        item = _conversation(items, _NOA_CHANNEL, 21, 10, "שורה 1\n\n   שורה 2   סוף")
        _relevant(items, item)
    conv = _conversations(groups_client, "Noa")[0]
    assert "\n" not in conv["preview"] and "  " not in conv["preview"]
    assert "שורה 1 שורה 2 סוף" in conv["preview"]


def test_second_kid_does_not_see_the_first_kids_channel_and_family_is_on_both(
    groups_client: TestClient,
) -> None:
    with _seed_scope() as (memory, items):
        memory.set_kid("Noa", gender="girl", school="בראשית", grade="5")
        memory.set_kid("Itamar", gender="boy", school="בראשית", grade="2")
        memory.set_channel(_NOA_CHANNEL, kind="parents", applies_to="Noa")
        memory.set_channel(_ITAMAR_CHANNEL, kind="parents", applies_to="Itamar")
        memory.set_channel(_FAMILY_CHANNEL, kind="committee", applies_to="family")
        for channel, day in [(_NOA_CHANNEL, 21), (_ITAMAR_CHANNEL, 22), (_FAMILY_CHANNEL, 23)]:
            item = _conversation(items, channel, day, 10, "הודעה להורים")
            _relevant(items, item)

    noa = _groups(groups_client, _entity_id(groups_client, "Noa"))
    itamar = _groups(groups_client, _entity_id(groups_client, "Itamar"))
    # sorted() orders by codepoint: ה (U+05D4) < כ (U+05DB); ב < כ
    assert sorted(g["channel"] for g in noa["groups"]) == [_NOA_CHANNEL, _FAMILY_CHANNEL]
    assert sorted(g["channel"] for g in itamar["groups"]) == [_ITAMAR_CHANNEL, _FAMILY_CHANNEL]
    applies = {g["channel"]: g["applies_to"] for g in noa["groups"]}
    assert applies[_NOA_CHANNEL] == "Noa"
    assert applies[_FAMILY_CHANNEL] == "family"


def test_non_kid_person_gets_empty_groups(groups_client: TestClient) -> None:
    groups_client.post(f"{_ENDPOINT}/entities", json={"kind": "person", "name": "אבא"})
    assert _groups(groups_client, _entity_id(groups_client, "אבא")) == {"groups": []}


def test_unknown_entity_404_and_non_person_409(groups_client: TestClient) -> None:
    assert groups_client.get(f"{_ENDPOINT}/entities/{uuid.uuid4()}/groups").status_code == 404
    groups_client.post(f"{_ENDPOINT}/entities", json={"kind": "channel", "name": "בית ספר"})
    assert groups_client.get(
        f"{_ENDPOINT}/entities/{_entity_id(groups_client, 'בית ספר')}/groups"
    ).status_code == 409


# --- keep (B3) -----------------------------------------------------------------


def _seed_one_relevant(groups_client: TestClient) -> str:
    """Kid Noa + her channel + one relevant conversation; returns the item id."""
    with _seed_scope() as (memory, items):
        memory.set_kid("Noa", gender="girl", school="בראשית", grade="5")
        memory.set_channel(_NOA_CHANNEL, kind="parents", applies_to="Noa")
        item = _conversation(items, _NOA_CHANNEL, 21, 10, "מחר פעילות מים — להביא מגבת")
        _relevant(items, item)
        return str(item.id)


def test_keep_writes_a_confirmed_note_with_provenance(groups_client: TestClient) -> None:
    item_id = _seed_one_relevant(groups_client)
    entity_id = _entity_id(groups_client, "Noa")
    response = groups_client.post(f"{_ENDPOINT}/entities/{entity_id}/groups/keep",
                                  json={"item_id": item_id})
    assert response.status_code == 201, response.text
    fact = response.json()
    assert fact["predicate"] == "note" and fact["status"] == "confirmed"
    assert "להביא מגבת" in fact["value"]
    assert fact["provenance"]["how"] == "group-keep"
    assert fact["provenance"]["item_id"] == item_id
    assert fact["provenance"]["channel"] == _NOA_CHANNEL
    assert fact["provenance"]["chat_jid"] == "1204@g.us"
    # the projection marks exactly that row as kept
    assert [c["kept_fact_id"] for c in _conversations(groups_client, "Noa")] == [fact["id"]]


def test_double_keep_is_409(groups_client: TestClient) -> None:
    item_id = _seed_one_relevant(groups_client)
    entity_id = _entity_id(groups_client, "Noa")
    first = groups_client.post(f"{_ENDPOINT}/entities/{entity_id}/groups/keep",
                               json={"item_id": item_id})
    assert first.status_code == 201, first.text
    second = groups_client.post(f"{_ENDPOINT}/entities/{entity_id}/groups/keep",
                                json={"item_id": item_id})
    assert second.status_code == 409, second.text


def test_keep_refuses_a_foreign_or_hidden_or_nonwhatsapp_item(
    groups_client: TestClient,
) -> None:
    """A Keep cannot be pointed at an arbitrary item: another kid's channel,
    a superseded copy of the kid's own channel, and a non-WhatsApp item that
    carries the same meta.channel all refuse."""
    with _seed_scope() as (memory, items):
        memory.set_kid("Noa", gender="girl", school="בראשית", grade="5")
        memory.set_kid("Itamar", gender="boy", school="בראשית", grade="2")
        memory.set_channel(_NOA_CHANNEL, kind="parents", applies_to="Noa")
        memory.set_channel(_ITAMAR_CHANNEL, kind="parents", applies_to="Itamar")
        foreign = _conversation(items, _ITAMAR_CHANNEL, 22, 10, "של איתמר")
        _relevant(items, foreign)
        hidden = _conversation(items, _NOA_CHANNEL, 23, 10, "העתק ישן")
        _relevant(items, hidden)
        newer = _conversation(items, _NOA_CHANNEL, 24, 10, "העתק חדש")
        _relevant(items, newer)
        hidden.superseded_by = newer.id
        items._db.flush()
        not_whatsapp = items.add(MemoryItem(
            source="email", raw_text="daily digest",
            occurred_at=datetime(2026, 10, 23, 9, 0, tzinfo=UTC),
            meta={"channel": _NOA_CHANNEL},
        ))
        _relevant(items, not_whatsapp)

    noa_id = _entity_id(groups_client, "Noa")
    for bad_id in [str(foreign.id), str(hidden.id), str(not_whatsapp.id)]:
        response = groups_client.post(f"{_ENDPOINT}/entities/{noa_id}/groups/keep",
                                      json={"item_id": bad_id})
        assert response.status_code == 409, (bad_id, response.text)
    # the kid's own live conversation still keeps
    with _seed_scope() as (_memory, items):
        live = items.by_channels([_NOA_CHANNEL], limit=5, relevant_only=True)
    assert len(live) == 1
    ok = groups_client.post(f"{_ENDPOINT}/entities/{noa_id}/groups/keep",
                            json={"item_id": str(live[0].id)})
    assert ok.status_code == 201, ok.text


def test_keep_undo_delete_and_restore(groups_client: TestClient) -> None:
    item_id = _seed_one_relevant(groups_client)
    entity_id = _entity_id(groups_client, "Noa")
    made = groups_client.post(f"{_ENDPOINT}/entities/{entity_id}/groups/keep",
                              json={"item_id": item_id}).json()
    deleted = groups_client.delete(f"{_ENDPOINT}/facts/{made['id']}")
    assert deleted.status_code == 204, deleted.text
    assert _conversations(groups_client, "Noa")[0]["kept_fact_id"] is None
    restored = groups_client.post(f"{_ENDPOINT}/facts/{made['id']}/restore")
    assert restored.status_code == 200, restored.text
    assert _conversations(groups_client, "Noa")[0]["kept_fact_id"] == made["id"]


def test_keep_snippet_is_capped_and_collapsed(groups_client: TestClient) -> None:
    with _seed_scope() as (memory, items):
        memory.set_kid("Noa", gender="girl", school="בראשית", grade="5")
        memory.set_channel(_NOA_CHANNEL, kind="parents", applies_to="Noa")
        item = _conversation(items, _NOA_CHANNEL, 21, 10, "מילה " * 200)
        _relevant(items, item)
    entity_id = _entity_id(groups_client, "Noa")
    item_id = _conversations(groups_client, "Noa")[0]["item_id"]
    fact = groups_client.post(f"{_ENDPOINT}/entities/{entity_id}/groups/keep",
                              json={"item_id": item_id}).json()
    assert fact["value"].endswith("…")
    assert len(fact["value"]) <= 601
    assert "\n" not in fact["value"] and "  " not in fact["value"]


# --- the by_channels contract (the one repo change) -----------------------------


def test_by_channels_relevant_only_is_opt_in(groups_client: TestClient) -> None:
    """Default: untriaged items pass (the /recent semantics — existing callers
    unchanged). relevant_only=True: strictly triage_label == 'relevant'."""
    with _seed_scope() as (memory, items):
        memory.set_kid("Noa", gender="girl", school="בראשית", grade="5")
        memory.set_channel(_NOA_CHANNEL, kind="parents", applies_to="Noa")
        relevant = _conversation(items, _NOA_CHANNEL, 21, 10, "רלוונטי")
        _relevant(items, relevant)
        untriaged = _conversation(items, _NOA_CHANNEL, 22, 10, "לא מסווג")
        noise = _conversation(items, _NOA_CHANNEL, 23, 10, "ספאם")
        items.apply_triage(
            noise, TriageResult(label="noise", confidence=1.0, method="jev", version="t1")
        )

        default_ids = {str(i.id) for i in items.by_channels([_NOA_CHANNEL], limit=10)}
        strict = items.by_channels([_NOA_CHANNEL], limit=10, relevant_only=True)
        strict_ids = {str(i.id) for i in strict}
    assert str(relevant.id) in default_ids and str(untriaged.id) in default_ids
    assert str(noise.id) not in default_ids
    assert strict_ids == {str(relevant.id)}
