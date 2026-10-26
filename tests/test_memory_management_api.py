"""Memory Management screen — part A endpoints (red-first).

Covers the four part-A gaps:
1. Aliases on an EXISTING entity (add/remove + ops marker) — the product had
   no path; triage and A2 match kids through aliases.
2. Entity archive/restore: removing a WhatsApp group from the allowlist must
   be soft (Undo restores EXACTLY — facts and aliases never leave), while the
   existing hard DELETE (fact-free only, 409 otherwise) stays unchanged.
3. Fact restore: DELETE /facts is soft (status=rejected, valid_to set);
   Undo reopens the SAME row (id, status, supersedes chain) — never a
   re-create with a new id. The prior status is captured at delete time.
4. GET /memory/bridge-groups: read-only listing of bridge-visible WhatsApp
   groups not yet allowlisted — SELECT-only against the bridge store.
5. GET /memory/family-summary: deterministic, stored-facts only (no LLM),
   this week's items linked by id, inferred lines labelled.

Binding/auth: every new route registers on the SAME /memory router inside
brain.main:app, so it inherits the production binding (uvicorn --host
127.0.0.1, scripts/run-server.sh) — no new exposure.
"""

import asyncio
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime
from zoneinfo import ZoneInfo

import httpx
from fastapi.testclient import TestClient
from sqlalchemy import text

from brain.config import get_settings
from brain.db.session import engine
from brain.main import create_app

_ENDPOINT = "/memory"


def _add_entity(client: TestClient, name: str, kind: str = "person", **overrides) -> dict:
    payload: dict = {"kind": kind, "name": name}
    payload.update(overrides)
    response = client.post(f"{_ENDPOINT}/entities", json=payload)
    assert response.status_code == 201, response.text
    return response.json()


def _add_fact(client: TestClient, subject_id: str, **overrides) -> dict:
    payload: dict = {"subject_id": subject_id, "predicate": "grade", "value": "5"}
    payload.update(overrides)
    response = client.post(f"{_ENDPOINT}/facts", json=payload)
    assert response.status_code == 201, response.text
    return response.json()


# --- 1. aliases on an existing entity -----------------------------------------


def test_add_alias_to_existing_entity(client: TestClient, clean_memory: None) -> None:
    entity = _add_entity(client, name="Rona")

    response = client.post(f"{_ENDPOINT}/entities/{entity['id']}/aliases", json={"alias": "רוני"})

    assert response.status_code == 201, response.text
    assert "רוני" in response.json()["aliases"]
    overview = client.get(_ENDPOINT).json()["entities"]
    mine = next(e for e in overview if e["id"] == entity["id"])
    assert "רוני" in mine["aliases"]


def test_add_alias_is_idempotent(client: TestClient, clean_memory: None) -> None:
    entity = _add_entity(client, name="Rona")
    first = client.post(f"{_ENDPOINT}/entities/{entity['id']}/aliases", json={"alias": "רוני"})
    second = client.post(f"{_ENDPOINT}/entities/{entity['id']}/aliases", json={"alias": "רוני"})

    assert first.status_code == 201
    assert second.status_code == 201
    assert second.json()["aliases"].count("רוני") == 1


def test_add_alias_blank_rejected(client: TestClient, clean_memory: None) -> None:
    entity = _add_entity(client, name="Rona")
    response = client.post(f"{_ENDPOINT}/entities/{entity['id']}/aliases", json={"alias": "   "})
    assert response.status_code == 422


def test_add_alias_unknown_entity_404(client: TestClient, clean_memory: None) -> None:
    response = client.post(f"{_ENDPOINT}/entities/{uuid.uuid4()}/aliases", json={"alias": "x"})
    assert response.status_code == 404


def test_delete_alias_removes_it(client: TestClient, clean_memory: None) -> None:
    entity = _add_entity(client, name="Rona", aliases=["רוני"])
    response = client.delete(f"{_ENDPOINT}/entities/{entity['id']}/aliases/רוני")

    assert response.status_code == 204
    assert (
        client.delete(f"{_ENDPOINT}/entities/{entity['id']}/aliases/רוני").status_code == 404
    )  # gone — second delete is 404, not 204


def test_alias_writes_leave_an_ops_marker(client: TestClient, clean_memory: None) -> None:
    entity = _add_entity(client, name="Rona")
    client.post(f"{_ENDPOINT}/entities/{entity['id']}/aliases", json={"alias": "רוני"})
    client.delete(f"{_ENDPOINT}/entities/{entity['id']}/aliases/רוני")

    with engine.begin() as conn:
        rows = conn.execute(
            text("SELECT kind FROM ops_events WHERE component = 'memory' ORDER BY id")
        ).fetchall()
    kinds = [r[0] for r in rows]
    assert "alias_add" in kinds
    assert "alias_remove" in kinds


# --- 2. entity archive/restore (the allowlist removal) ------------------------


def test_archive_entity_hides_it_and_keeps_facts(client: TestClient, clean_memory: None) -> None:
    """Removing a WhatsApp group archives the channel: out of the list AND
    out of the poller's allowlist, facts intact, Undo exact."""
    entity = _add_entity(client, name="צופים רונה", kind="channel")
    client.post(
        f"{_ENDPOINT}/facts",
        json={
            "subject_id": entity["id"],
            "predicate": "applies_to",
            "value": "Rona",
            "status": "confirmed",
        },
    )

    response = client.post(f"{_ENDPOINT}/entities/{entity['id']}/archive")

    assert response.status_code == 200, response.text
    assert response.json()["archived"] is True
    # gone from the overview and from the channel->kid mapping (the poller
    # reads the same list_entities)
    assert client.get(_ENDPOINT).json() == {"entities": []}
    from brain.db.session import SessionLocal
    from brain.repositories.memory import MemoryRepository
    from brain.services.memory import MemoryService

    session = SessionLocal()
    try:
        service = MemoryService(MemoryRepository(session))
        assert service.channel_names_for_kid("Rona") == []
        # facts survive the archive
        facts = service._repo.current_facts(subject_ids=[entity_as_uuid(entity["id"])])
        assert len(facts) == 1
    finally:
        session.close()


def entity_as_uuid(raw: str) -> uuid.UUID:
    return uuid.UUID(raw)


def test_restore_entity_brings_everything_back(client: TestClient, clean_memory: None) -> None:
    entity = _add_entity(client, name="גן ניצן", kind="channel")
    client.post(
        f"{_ENDPOINT}/facts",
        json={"subject_id": entity["id"], "predicate": "channel_kind", "value": "parents"},
    )
    assert client.post(f"{_ENDPOINT}/entities/{entity['id']}/archive").status_code == 200

    response = client.post(f"{_ENDPOINT}/entities/{entity['id']}/restore")

    assert response.status_code == 200, response.text
    assert response.json()["archived"] is False
    overview = client.get(_ENDPOINT).json()["entities"]
    mine = next(e for e in overview if e["id"] == entity["id"])
    assert mine["name"] == "גן ניצן"
    assert mine["facts"][0]["predicate"] == "channel_kind"


def test_readd_archived_entity_unarchives_same_row(client: TestClient, clean_memory: None) -> None:
    """Add-from-bridge after a removal restores the SAME entity (exact Undo
    chain), it does not silently create a second channel."""
    entity = _add_entity(client, name="מרכז דקל", kind="channel")
    client.post(f"{_ENDPOINT}/entities/{entity['id']}/archive")

    response = client.post(f"{_ENDPOINT}/entities", json={"kind": "channel", "name": "מרכז דקל"})

    assert response.status_code == 201, response.text
    assert response.json()["id"] == entity["id"]
    assert response.json()["archived"] is False


def test_archive_unknown_entity_404(client: TestClient, clean_memory: None) -> None:
    assert client.post(f"{_ENDPOINT}/entities/{uuid.uuid4()}/archive").status_code == 404


def test_archive_is_idempotent(client: TestClient, clean_memory: None) -> None:
    entity = _add_entity(client, name="Dana")
    assert client.post(f"{_ENDPOINT}/entities/{entity['id']}/archive").status_code == 200
    assert client.post(f"{_ENDPOINT}/entities/{entity['id']}/archive").status_code == 200


def test_archived_entity_hidden_but_hard_delete_still_guarded(
    client: TestClient, clean_memory: None
) -> None:
    """The existing contract holds: hard DELETE still refuses while current
    facts exist — even archived ones (archive is reversible, delete is not)."""
    entity = _add_entity(client, name="Rona")
    client.post(
        f"{_ENDPOINT}/facts",
        json={"subject_id": entity["id"], "predicate": "grade", "value": "5"},
    )
    client.post(f"{_ENDPOINT}/entities/{entity['id']}/archive")

    response = client.delete(f"{_ENDPOINT}/entities/{entity['id']}")
    assert response.status_code == 409


# --- 3. fact restore (Undo for a deleted memory) ------------------------------


def test_delete_fact_captures_prior_status_in_provenance(
    client: TestClient, clean_memory: None
) -> None:
    entity = _add_entity(client, name="Rona")
    fact = _add_fact(client, entity["id"])  # confirmed by default

    assert client.delete(f"{_ENDPOINT}/facts/{fact['id']}").status_code == 204

    with engine.begin() as conn:
        row = conn.execute(
            text("SELECT provenance FROM memory_facts WHERE id = :id"),
            {"id": fact["id"]},
        ).fetchone()
    assert row is not None
    assert row[0].get("deleted_from") == "confirmed"


def test_restore_fact_reopens_same_row(client: TestClient, clean_memory: None) -> None:
    """Undo is a reopen: same fact id, same status, visible again — never a
    re-create with a new id."""
    entity = _add_entity(client, name="Rona")
    fact = _add_fact(client, entity["id"], predicate="note", value="Rona swims Tuesdays")
    client.delete(f"{_ENDPOINT}/facts/{fact['id']}")
    body = client.get(_ENDPOINT).json()["entities"]
    assert next(e for e in body if e["id"] == entity["id"])["facts"] == []

    response = client.post(f"{_ENDPOINT}/facts/{fact['id']}/restore")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["id"] == fact["id"]
    assert body["status"] == "confirmed"
    assert body["valid_to"] is None
    overview = client.get(_ENDPOINT).json()["entities"]
    mine = next(e for e in overview if e["id"] == entity["id"])
    assert mine["facts"][0]["value"] == "Rona swims Tuesdays"


def test_restore_proposed_fact_stays_proposed(client: TestClient, clean_memory: None) -> None:
    entity = _add_entity(client, name="Rona")
    fact = _add_fact(client, entity["id"], status="proposed")
    client.delete(f"{_ENDPOINT}/facts/{fact['id']}")

    response = client.post(f"{_ENDPOINT}/facts/{fact['id']}/restore")

    assert response.status_code == 200
    assert response.json()["status"] == "proposed"


def test_restore_open_fact_conflicts(client: TestClient, clean_memory: None) -> None:
    """Only a soft-DELETED fact can be restored — an open one has nothing to
    undo (and a PATCH-closed version must not resurrect either)."""
    entity = _add_entity(client, name="Rona")
    fact = _add_fact(client, entity["id"])

    response = client.post(f"{_ENDPOINT}/facts/{fact['id']}/restore")
    assert response.status_code == 409

    client.patch(
        f"{_ENDPOINT}/facts/{fact['id']}", json={"value": "updated"}
    )  # PATCH closes the old version (supersedes) and opens a new one
    closed = client.get(f"{_ENDPOINT}/facts/{fact['id']}/history").json()
    old_version = next(f for f in closed if f["valid_to"] is not None)
    assert client.post(f"{_ENDPOINT}/facts/{old_version['id']}/restore").status_code == 409


def test_restore_fact_not_found(client: TestClient, clean_memory: None) -> None:
    assert client.post(f"{_ENDPOINT}/facts/{uuid.uuid4()}/restore").status_code == 404


# --- 4. bridge groups (read-only options for "+ Add group") -------------------


def _make_bridge_store(tmp_path, chats: dict[str, str]) -> str:

    path = tmp_path / "messages.db"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE chats (jid TEXT PRIMARY KEY, name TEXT)")
    for jid, name in chats.items():
        conn.execute("INSERT INTO chats (jid, name) VALUES (?, ?)", (jid, name))
    conn.commit()
    conn.close()
    return str(path)


def test_bridge_groups_lists_unallowlisted_groups(
    client: TestClient, clean_memory: None, tmp_path, monkeypatch
) -> None:
    store = _make_bridge_store(
        tmp_path,
        {
            "120363@g.us": "צופים רונה",  # already allowlisted below
            "120364@g.us": "מרכז דקל - עדכונים ✨",  # not allowlisted -> offered
            "120365@g.us": "הורים כיתה ה",  # not allowlisted -> offered
            "97250@s.whatsapp.net": "אמא",  # not a group -> excluded
        },
    )
    _add_entity(client, name="צופים רונה", kind="channel")
    monkeypatch.setenv("WHATSAPP_BRIDGE_DB", store)
    get_settings.cache_clear()
    fresh = TestClient(create_app())
    try:
        response = fresh.get(f"{_ENDPOINT}/bridge-groups")
        assert response.status_code == 200, response.text
        names = [g["name"] for g in response.json()["groups"]]
        assert names == sorted(names)
        assert "מרכז דקל - עדכונים ✨" in names
        assert "הורים כיתה ה" in names
        assert "צופים רונה" not in names  # allowlisted channels are not options
        assert "אמא" not in names  # direct chats are not group options
    finally:
        get_settings.cache_clear()


def test_bridge_groups_never_writes(
    client: TestClient, clean_memory: None, tmp_path, monkeypatch
) -> None:
    """The endpoint may only READ the bridge: every statement executed
    against the store must be a SELECT/PRAGMA — no INSERT/UPDATE/DELETE, and
    the bridge's REST /api/send is never touched."""
    store = _make_bridge_store(tmp_path, {"120364@g.us": "מרכז דקל - עדכונים ✨"})
    monkeypatch.setenv("WHATSAPP_BRIDGE_DB", store)
    get_settings.cache_clear()

    executed: list[str] = []
    import sqlite3 as _sqlite3

    from brain.services import whatsapp as wa

    real_connect = _sqlite3.connect

    class _SpyConn:
        """Read-only guard: record every statement the endpoint executes."""

        def __init__(self, conn: _sqlite3.Connection) -> None:
            self._conn = conn

        def execute(self, sql, *a, **k):
            executed.append(" ".join(str(sql).split()).upper())
            return self._conn.execute(sql, *a, **k)

        def close(self):
            self._conn.close()

    def spy_connect(*args, **kwargs):
        return _SpyConn(real_connect(*args, **kwargs))

    monkeypatch.setattr(wa.sqlite3, "connect", spy_connect)
    fresh = TestClient(create_app())
    try:
        assert fresh.get(f"{_ENDPOINT}/bridge-groups").status_code == 200
    finally:
        get_settings.cache_clear()
    assert executed, "the endpoint must read the bridge store"
    assert all(s.startswith("SELECT") or s.startswith("PRAGMA") for s in executed), (
        f"write statement reached the bridge store: {executed}"
    )


# --- 5. family summary (deterministic; no LLM) --------------------------------


def _seed_item(**overrides) -> uuid.UUID:
    item_id = uuid.uuid4()
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO memory_items (id, raw_text, source, status, occurred_at, applies_to) "
                "VALUES (:id, :raw_text, :source, :status, :occurred_at, :applies_to)"
            ),
            {
                "id": str(item_id),
                "raw_text": overrides.get("raw_text", "Noa has a swimming test Friday"),
                "source": overrides.get("source", "whatsapp"),
                "status": "inbox",
                "occurred_at": overrides.get(
                    "occurred_at", datetime(2025, 10, 22, 15, 0, tzinfo=ZoneInfo("UTC"))
                ),
                "applies_to": overrides.get("applies_to", '["Noa"]'),
            },
        )
    return item_id


def test_family_summary_members_from_stored_facts(client: TestClient, clean_memory: None) -> None:
    noa = _add_entity(client, name="Noa")
    _add_fact(client, noa["id"], predicate="grade", value="ה")
    _add_fact(client, noa["id"], predicate="class", value="ספיר")
    _add_fact(client, noa["id"], predicate="teacher", value="דנה")

    response = client.get(f"{_ENDPOINT}/family-summary?on=2025-10-22")

    assert response.status_code == 200, response.text
    body = response.json()
    member = next(m for m in body["members"] if m["name"] == "Noa")
    assert member["grade"] == "ה"
    assert member["class"] == "ספיר"
    assert member["teacher"] == "דנה"
    assert "inferred" in member and member["inferred"] == []


def test_family_summary_week_items_linked(client: TestClient, clean_memory: None) -> None:
    noa = _add_entity(client, name="Noa")
    _add_fact(client, noa["id"], predicate="grade", value="ה")
    in_week = _seed_item(occurred_at=datetime(2025, 10, 22, 15, 0, tzinfo=ZoneInfo("UTC")))
    out_week = _seed_item(occurred_at=datetime(2025, 9, 10, 15, 0, tzinfo=ZoneInfo("UTC")))

    response = client.get(f"{_ENDPOINT}/family-summary?on=2025-10-22")

    member = next(m for m in response.json()["members"] if m["name"] == "Noa")
    ids = [i["item_id"] for i in member["week_items"]]
    assert str(in_week) in ids
    assert str(out_week) not in ids


def test_family_summary_channels_leg(client: TestClient, clean_memory: None) -> None:
    noa = _add_entity(client, name="Noa")
    _add_fact(client, noa["id"], predicate="grade", value="ה")
    _add_entity(client, name="הורים נועה", kind="channel")
    client.post(
        f"{_ENDPOINT}/facts",
        json={
            "subject_id": _entity_id(client, "הורים נועה"),
            "predicate": "applies_to",
            "value": "Noa",
            "status": "confirmed",
        },
    )

    response = client.get(f"{_ENDPOINT}/family-summary?on=2025-10-22")
    member = next(m for m in response.json()["members"] if m["name"] == "Noa")
    assert "הורים נועה" in member["channels"]


def _entity_id(client: TestClient, name: str) -> str:
    for e in client.get(_ENDPOINT).json()["entities"]:
        if e["name"] == name:
            return e["id"]
    raise AssertionError(name)


def test_family_summary_proposed_facts_are_the_open_leg(
    client: TestClient, clean_memory: None
) -> None:
    """Open (proposed, unconfirmed) facts are the screen's 'conflicts' leg —
    surfaced per member, never invented."""
    noa = _add_entity(client, name="Noa")
    _add_fact(client, noa["id"], predicate="grade", value="ה")
    proposed = _add_fact(
        client, noa["id"], predicate="note", value="maybe chess club", status="proposed"
    )

    response = client.get(f"{_ENDPOINT}/family-summary?on=2025-10-22")
    member = next(m for m in response.json()["members"] if m["name"] == "Noa")
    assert proposed["id"] in [f["id"] for f in member["proposed_facts"]]


def test_family_summary_touches_no_llm(client: TestClient, clean_memory: None, monkeypatch) -> None:
    """The summary must be computable with NO ai deps at all: any LLM client
    construction raises — the endpoint still answers."""
    from brain.api import deps

    def boom(*a, **k):
        raise AssertionError("family-summary must not build an LLM client")

    monkeypatch.setattr(deps, "get_llm_client", boom)
    noa = _add_entity(client, name="Noa")
    _add_fact(client, noa["id"], predicate="grade", value="ה")

    response = client.get(f"{_ENDPOINT}/family-summary?on=2025-10-22")
    assert response.status_code == 200


# --- review round 1: entity unarchive consistency (item 2) --------------------


def test_seed_paths_unarchive_the_same_row(client: TestClient, clean_memory: None) -> None:
    """set_channel on an ARCHIVED group name must not create a second
    entity with the same name — the archived row is unarchived in place."""
    entity = _add_entity(client, name="דמו צופים", kind="channel")
    client.post(f"{_ENDPOINT}/entities/{entity['id']}/archive")
    archived_id = entity["id"]

    response = client.post(f"{_ENDPOINT}/entities", json={"kind": "channel", "name": "דמו צופים"})
    assert response.json()["id"] == archived_id
    assert response.json()["archived"] is False

    # the seed path goes through the service, not the route: a duplicate
    # must not appear under any listing
    names = [e["name"] for e in client.get(_ENDPOINT).json()["entities"]]
    assert names.count("דמו צופים") == 1


# --- review round 1: restore vs a newer current fact (item 3) -----------------


def test_restore_fact_refused_when_a_current_fact_exists(
    client: TestClient, clean_memory: None
) -> None:
    """Delete 'grade 5', add 'grade 6', then Undo: restoring would put two
    current values next to each other — refused with 409."""
    entity = _add_entity(client, name="Noa")
    old = _add_fact(client, entity["id"], predicate="grade", value="5")
    client.delete(f"{_ENDPOINT}/facts/{old['id']}")
    _add_fact(client, entity["id"], predicate="grade", value="6")

    response = client.post(f"{_ENDPOINT}/facts/{old['id']}/restore")

    assert response.status_code == 409, response.text
    body = response.json()
    assert "current" in body["detail"]
    # the refusal leaves state exactly as it was: one current grade
    facts = client.get(_ENDPOINT).json()["entities"][0]["facts"]
    assert [f["value"] for f in facts] == ["6"]


# --- review round 1: family summary week bounds follow the local clock (4) ----


def test_family_summary_week_starts_local_monday_midnight(clean_memory: None, monkeypatch) -> None:
    """An item at local Monday 00:30 (Jerusalem) belongs to THIS week even
    though its UTC instant falls on Sunday evening. The timezone is pinned
    (CI hosts run UTC — the bound must come from settings, not the host)."""
    monkeypatch.setenv("TIMEZONE", "Asia/Jerusalem")
    get_settings.cache_clear()
    fresh = TestClient(create_app())
    try:
        noa = _add_entity(fresh, name="Noa")
        _add_fact(fresh, noa["id"], predicate="grade", value="ה")
        # 2025-10-20 is a Monday; Jerusalem is UTC+3 (IDT) until late October,
        # so local Mon 00:30 = Sun 21:30Z — inside THIS week by local clock.
        item_id = _seed_item(occurred_at=datetime(2025, 10, 19, 21, 30, tzinfo=ZoneInfo("UTC")))

        response = fresh.get(f"{_ENDPOINT}/family-summary?on=2025-10-20")

        member = next(m for m in response.json()["members"] if m["name"] == "Noa")
        assert str(item_id) in [i["item_id"] for i in member["week_items"]]
    finally:
        get_settings.cache_clear()


# --- review round 1: the summary never lists hidden items (5) -----------------


def _seed_item_raw(**overrides) -> uuid.UUID:
    item_id = uuid.uuid4()
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO memory_items (id, raw_text, source, status, occurred_at, "
                "applies_to, superseded_by, extraction_meta) VALUES "
                "(:id, :raw_text, :source, :status, :occurred_at, :applies_to, "
                ":superseded_by, :extraction_meta)"
            ),
            {
                "id": str(item_id),
                "raw_text": overrides.get("raw_text", "Noa (demo) week item"),
                "source": overrides.get("source", "whatsapp"),
                "status": "inbox",
                "occurred_at": overrides.get(
                    "occurred_at", datetime(2025, 10, 22, 15, 0, tzinfo=ZoneInfo("UTC"))
                ),
                "applies_to": overrides.get("applies_to", '["Noa"]'),
                "superseded_by": overrides.get("superseded_by"),
                "extraction_meta": overrides.get("extraction_meta", "{}"),
            },
        )
    return item_id


def test_family_summary_hides_noise_and_superseded(client: TestClient, clean_memory: None) -> None:
    """Superseded and triage-noise items in the week are absent — the same
    default-hidden conditions every other listing uses (#98)."""
    noa = _add_entity(client, name="Noa")
    _add_fact(client, noa["id"], predicate="grade", value="ה")
    _visible = _seed_item_raw(raw_text="visible item")  # kept
    replacement = _seed_item_raw(raw_text="current wording")
    superseded = _seed_item_raw(raw_text="old wording", superseded_by=str(replacement))
    noise = _seed_item_raw(raw_text="triage noise", extraction_meta='{"cancelled": true}')

    response = client.get(f"{_ENDPOINT}/family-summary?on=2025-10-22")

    member = next(m for m in response.json()["members"] if m["name"] == "Noa")
    ids = [i["item_id"] for i in member["week_items"]]
    assert superseded not in ids
    assert noise not in ids
    assert str(_visible) in ids


# --- review round 1: archiving a person pins the kids() consequence (7) -------


def test_archived_kid_leaves_kids_and_returns_on_restore(
    client: TestClient, clean_memory: None
) -> None:
    """An archived person leaves kids() (triage + A2 stop seeing them) and
    comes back on restore — pinned so the UI can spell the consequence out."""
    entity = _add_entity(client, name="Noa")
    _add_fact(client, entity["id"], predicate="grade", value="ה")

    with _memory_service_for_tests() as service:
        assert [k["name"] for k in service.kids()] == ["Noa"]
        client.post(f"{_ENDPOINT}/entities/{entity['id']}/archive")
        assert service.kids() == []
        client.post(f"{_ENDPOINT}/entities/{entity['id']}/restore")
        assert [k["name"] for k in service.kids()] == ["Noa"]


@contextmanager
def _memory_service_for_tests():
    """A MemoryService over its own session, CLOSED afterwards — a leaked
    session here stays idle-in-transaction on `entities` and blocks the
    migration test's DROP COLUMN (CI timeout, round 2)."""
    from brain.db.session import SessionLocal
    from brain.repositories.memory import MemoryRepository
    from brain.services.memory import MemoryService

    session = SessionLocal()
    try:
        yield MemoryService(MemoryRepository(session))
    finally:
        session.rollback()
        session.close()


def test_set_channel_on_archived_name_unarchives_same_row(
    client: TestClient, clean_memory: None
) -> None:
    """The SERVICE seed path (set_channel — what the poller/seed use) on an
    archived name must unarchive the same row, never create a duplicate."""
    entity = _add_entity(client, name="דמו הורים קבוצה", kind="channel")
    client.post(f"{_ENDPOINT}/entities/{entity['id']}/archive")

    with _memory_service_for_tests() as service:
        seeded = service.set_channel("דמו הורים קבוצה", kind="teacher", applies_to="me")

        assert seeded["id"] == entity["id"]
        assert seeded["archived"] is False
        names = [e["name"] for e in service.overview()]
        assert names.count("דמו הורים קבוצה") == 1


def test_restore_note_fact_allowed_next_to_other_notes(
    client: TestClient, clean_memory: None
) -> None:
    """'note' facts are additive free-text memories (one card each): their
    Undo must NOT hit the single-value restore guard — a person carries
    many current notes at once."""
    entity = _add_entity(client, name="Noa")
    _add_fact(client, entity["id"], predicate="note", value="swims Fridays")
    deleted = _add_fact(client, entity["id"], predicate="note", value="chess club maybe")
    client.delete(f"{_ENDPOINT}/facts/{deleted['id']}")

    response = client.post(f"{_ENDPOINT}/facts/{deleted['id']}/restore")

    assert response.status_code == 200, response.text
    values = sorted(f["value"] for f in client.get(_ENDPOINT).json()["entities"][0]["facts"])
    assert values == ["chess club maybe", "swims Fridays"]


# --- 12. single-valued kid predicates supersede (one current value) -----------


def test_add_fact_supersedes_current_grade_fact(client: TestClient, clean_memory: None) -> None:
    """Posting a second current value for a single-valued kid predicate
    (the set_kid field set) must leave ONE current fact: the old row is
    closed and chained via supersedes_id, exactly as set_kid does. The
    old behavior piled up parallel current facts (8 grade facts on one
    person after repeated adds), which kids() then resolved arbitrarily
    — the wrong-child axis."""
    entity = _add_entity(client, name="Noa")
    first = _add_fact(client, entity["id"], predicate="grade", value="ה")
    second = _add_fact(client, entity["id"], predicate="grade", value="ד")

    assert second["supersedes_id"] == first["id"]
    grades = [
        fact
        for fact in client.get(_ENDPOINT).json()["entities"][0]["facts"]
        if fact["predicate"] == "grade"
    ]
    assert len(grades) == 1
    assert grades[0]["value"] == "ד"


def test_add_fact_same_single_valued_value_is_idempotent(
    client: TestClient, clean_memory: None
) -> None:
    """Re-posting the value a single-valued predicate already carries is a
    no-op (set_kid: an unchanged attribute keeps its row — no history
    spam), returning the existing fact."""
    entity = _add_entity(client, name="Noa")
    first = _add_fact(client, entity["id"], predicate="grade", value="ה")

    response = client.post(
        f"{_ENDPOINT}/facts",
        json={
            "subject_id": entity["id"],
            "predicate": "grade",
            "value": "ה",
        },
    )

    # 201 with the EXISTING row — the API's established idempotent style
    # (add_entity returns 201 for a re-add of the same row too).
    assert response.status_code == 201, response.text
    assert response.json()["id"] == first["id"]
    grades = [
        fact
        for fact in client.get(_ENDPOINT).json()["entities"][0]["facts"]
        if fact["predicate"] == "grade"
    ]
    assert len(grades) == 1


def test_note_facts_stay_additive(client: TestClient, clean_memory: None) -> None:
    """'note' is not in the single-valued set: many current notes per
    person are the norm (one card each)."""
    entity = _add_entity(client, name="Noa")
    _add_fact(client, entity["id"], predicate="note", value="swims Fridays")
    _add_fact(client, entity["id"], predicate="note", value="chess club maybe")

    notes = [
        fact
        for fact in client.get(_ENDPOINT).json()["entities"][0]["facts"]
        if fact["predicate"] == "note"
    ]
    assert sorted(fact["value"] for fact in notes) == ["chess club maybe", "swims Fridays"]


def test_single_valued_set_matches_set_kid_signature(
    client: TestClient, clean_memory: None
) -> None:
    """The single-valued set must stay IDENTICAL to the profile fields
    set_kid writes — one source of truth, pinned here via the signature
    so a new set_kid field cannot silently miss the supersede behavior."""
    import inspect

    from brain.services.memory import MemoryService

    # set_kid(name, *, gender, school, grade, class_, teacher): drop the
    # positional 'name' and 'self'; class_ is the fact predicate 'class'.
    params = set(inspect.signature(MemoryService.set_kid).parameters) - {"name", "self"}
    params = {"class" if field == "class_" else field for field in params}
    from brain.services.memory import SINGLE_VALUED_KID_PREDICATES

    assert set(SINGLE_VALUED_KID_PREDICATES) == params


# --- 13. Undo of delete: multi-valued classes stay restorable -----------------


def test_restore_connection_to_other_child_allowed(client: TestClient, clean_memory: None) -> None:
    """connection:* facts are multi-valued (one per related person, keyed by
    object_id): deleting the link to Itamar must be Undo-able even though a
    connection:child to Rona still exists. The single-value guard must
    compare object_id for connections, not just the predicate."""
    entity = _add_entity(client, name="Noa")
    rona = _add_entity(client, name="Rona")
    itamar = _add_entity(client, name="Itamar")
    _add_fact(
        client,
        entity["id"],
        predicate="connection:child",
        object_id=rona["id"],
        value=None,
    )
    to_itamar = _add_fact(
        client,
        entity["id"],
        predicate="connection:child",
        object_id=itamar["id"],
        value=None,
    )
    client.delete(f"{_ENDPOINT}/facts/{to_itamar['id']}")

    response = client.post(f"{_ENDPOINT}/facts/{to_itamar['id']}/restore")

    assert response.status_code == 200, response.text
    links = sorted(
        f["object_id"]
        for f in client.get(_ENDPOINT).json()["entities"][0]["facts"]
        if f["predicate"] == "connection:child"
    )
    assert links == sorted([rona["id"], itamar["id"]])


def test_restore_same_connection_to_same_child_still_refused(
    client: TestClient, clean_memory: None
) -> None:
    """The object_id comparison still guards the REAL duplicate: restoring a
    connection:child to Rona while another current connection:child to Rona
    exists would put two identical links on the profile. add_fact refuses
    such duplicates upfront, so the deleted-duplicate state is built through
    the repo directly (the guard stays as defense-in-depth for restores)."""
    import uuid as _uuid
    from datetime import datetime
    from zoneinfo import ZoneInfo

    from brain.db.models import MemoryFact

    entity = _add_entity(client, name="Noa")
    rona = _add_entity(client, name="Rona")
    _add_fact(client, entity["id"], predicate="connection:child", object_id=rona["id"])
    with _memory_service_for_tests() as service:
        duplicate = service._repo.add_fact(
            MemoryFact(
                subject_id=_uuid.UUID(entity["id"]),
                predicate="connection:child",
                object_id=_uuid.UUID(rona["id"]),
                value=None,
                status="confirmed",
                valid_from=datetime.now(ZoneInfo("UTC")),
                supersedes_id=None,
                provenance={"source": "user"},
            )
        )
        service._repo.commit()
        duplicate_id = str(duplicate.id)
    client.delete(f"{_ENDPOINT}/facts/{duplicate_id}")

    response = client.post(f"{_ENDPOINT}/facts/{duplicate_id}/restore")

    assert response.status_code == 409


def test_restore_memory_category_facts_stay_additive(
    client: TestClient, clean_memory: None
) -> None:
    """routine/preference/date are free-form memories like notes: many
    current ones per person are the norm, so their Undo must never 409."""
    entity = _add_entity(client, name="Noa")
    _add_fact(client, entity["id"], predicate="routine", value="swims Fridays")
    deleted = _add_fact(client, entity["id"], predicate="routine", value="library Tuesdays")
    client.delete(f"{_ENDPOINT}/facts/{deleted['id']}")

    response = client.post(f"{_ENDPOINT}/facts/{deleted['id']}/restore")

    assert response.status_code == 200, response.text
    values = sorted(
        f["value"]
        for f in client.get(_ENDPOINT).json()["entities"][0]["facts"]
        if f["predicate"] == "routine"
    )
    assert values == ["library Tuesdays", "swims Fridays"]


# --- 14. review-round follow-ups (PR #136): confirm supersede, one set, rename --


def test_confirm_proposed_grade_supersedes_current(client: TestClient, clean_memory: None) -> None:
    """Confirming a PROPOSED single-valued fact next to a current one must
    supersede, not pile a second current value: the new value is confirmed
    with supersedes_id chained to the closed one."""
    entity = _add_entity(client, name="Noa")
    old = _add_fact(client, entity["id"], predicate="grade", value="2")
    proposed = _add_fact(
        client,
        entity["id"],
        predicate="grade",
        value="3",
        status="proposed",
    )

    response = client.post(f"{_ENDPOINT}/facts/{proposed['id']}/confirm")

    assert response.status_code == 200, response.text
    saved = response.json()
    assert saved["id"] == proposed["id"]
    assert saved["supersedes_id"] == old["id"], saved
    current = [
        f for f in client.get(_ENDPOINT).json()["entities"][0]["facts"] if f["predicate"] == "grade"
    ]
    assert [f["value"] for f in current] == ["3"], current


def test_confirm_proposed_equal_value_adds_no_duplicate(
    client: TestClient, clean_memory: None
) -> None:
    """Confirming a proposal whose value equals the current one is a no-op:
    one current fact survives (the existing row), the proposal leaves the
    inbox, and the response names the surviving row."""
    entity = _add_entity(client, name="Noa")
    current = _add_fact(client, entity["id"], predicate="grade", value="2")
    proposed = _add_fact(
        client,
        entity["id"],
        predicate="grade",
        value="2",
        status="proposed",
    )

    response = client.post(f"{_ENDPOINT}/facts/{proposed['id']}/confirm")

    assert response.status_code == 200, response.text
    assert response.json()["id"] == current["id"], response.text
    grades = [
        f for f in client.get(_ENDPOINT).json()["entities"][0]["facts"] if f["predicate"] == "grade"
    ]
    assert [f["value"] for f in grades] == ["2"], grades
    inbox = client.get(f"{_ENDPOINT}?status=proposed").json()["entities"]
    assert inbox == [], inbox


def test_confirm_proposed_note_stays_additive(client: TestClient, clean_memory: None) -> None:
    """Notes are free-text memories: confirming one never closes another."""
    entity = _add_entity(client, name="Noa")
    _add_fact(client, entity["id"], predicate="note", value="likes libraries")
    proposed = _add_fact(
        client,
        entity["id"],
        predicate="note",
        value="swims Fridays",
        status="proposed",
    )

    response = client.post(f"{_ENDPOINT}/facts/{proposed['id']}/confirm")

    assert response.status_code == 200, response.text
    notes = [
        f["value"]
        for f in client.get(_ENDPOINT).json()["entities"][0]["facts"]
        if f["predicate"] == "note"
    ]
    assert sorted(notes) == ["likes libraries", "swims Fridays"], notes


def test_restore_phone_with_another_current_phone_is_additive(
    client: TestClient, clean_memory: None
) -> None:
    """phone/email/work/relation are multi-valued: deleting one of two
    current phones must be Undo-able (the pre-#136 guard 409'd everything
    not note/memory-category/connection)."""
    entity = _add_entity(client, name="Noa")
    home = _add_fact(client, entity["id"], predicate="phone", value="050-1")
    work = _add_fact(client, entity["id"], predicate="phone", value="050-2")
    client.delete(f"{_ENDPOINT}/facts/{home['id']}")

    response = client.post(f"{_ENDPOINT}/facts/{home['id']}/restore")

    assert response.status_code == 200, response.text
    phones = sorted(
        f["value"]
        for f in client.get(_ENDPOINT).json()["entities"][0]["facts"]
        if f["predicate"] == "phone"
    )
    assert phones == ["050-1", "050-2"], phones
    assert work["id"]


def test_restore_birthday_with_current_birthday_refused(
    client: TestClient, clean_memory: None
) -> None:
    """birthday/anniversary are single-valued like the kid fields: a deleted
    birthday may not be restored next to a current one (state built through
    the repo — add_fact supersedes, so the API cannot create the pair)."""
    import uuid as _uuid

    from brain.db.models import MemoryFact

    entity = _add_entity(client, name="Noa")
    with _memory_service_for_tests() as service:
        for value in ("1.1.2010", "2.2.2011"):
            service._repo.add_fact(
                MemoryFact(
                    subject_id=_uuid.UUID(entity["id"]),
                    predicate="birthday",
                    object_id=None,
                    value=value,
                    status="confirmed",
                    valid_from=datetime.now(ZoneInfo("UTC")),
                    supersedes_id=None,
                    provenance={"source": "user"},
                )
            )
        service._repo.commit()
    duo = [
        f
        for f in client.get(_ENDPOINT).json()["entities"][0]["facts"]
        if f["predicate"] == "birthday"
    ]
    assert len(duo) == 2, duo
    victim = duo[0]
    client.delete(f"{_ENDPOINT}/facts/{victim['id']}")

    response = client.post(f"{_ENDPOINT}/facts/{victim['id']}/restore")

    assert response.status_code == 409, response.text


def test_add_second_birthday_supersedes_and_chains(client: TestClient, clean_memory: None) -> None:
    """Posting a second birthday closes the old one and chains the new row
    via supersedes_id — one current value, history queryable."""
    entity = _add_entity(client, name="Noa")
    old = _add_fact(client, entity["id"], predicate="birthday", value="1.1.2010")

    response = client.post(
        f"{_ENDPOINT}/facts",
        json={
            "subject_id": entity["id"],
            "predicate": "birthday",
            "value": "2.2.2011",
        },
    )

    assert response.status_code == 201, response.text
    saved = response.json()
    assert saved["supersedes_id"] == old["id"], saved
    birthdays = [
        f
        for f in client.get(_ENDPOINT).json()["entities"][0]["facts"]
        if f["predicate"] == "birthday"
    ]
    assert [f["value"] for f in birthdays] == ["2.2.2011"], birthdays


def test_superseded_birthday_restores_after_the_replacement_is_deleted(
    client: TestClient, clean_memory: None
) -> None:
    """The undo of a superseding add must WORK on the real server: the
    displaced row is soft-deleted (deleted_from captured), so after the
    replacement is deleted the old value restores (200) and is current
    again — not the 409 'only a deleted fact can be restored'."""
    entity = _add_entity(client, name="Noa")
    old = _add_fact(client, entity["id"], predicate="birthday", value="1.1.2010")
    made = client.post(
        f"{_ENDPOINT}/facts",
        json={
            "subject_id": entity["id"],
            "predicate": "birthday",
            "value": "2.2.2011",
        },
    )
    assert made.status_code == 201, made.text
    new_fact = made.json()
    assert [r["id"] for r in new_fact["replaced"]] == [old["id"]], new_fact

    deleted = client.delete(f"{_ENDPOINT}/facts/{new_fact['id']}")
    assert deleted.status_code == 204, deleted.text

    restored = client.post(f"{_ENDPOINT}/facts/{old['id']}/restore")

    assert restored.status_code == 200, restored.text
    birthdays = [
        f["value"]
        for f in client.get(_ENDPOINT).json()["entities"][0]["facts"]
        if f["predicate"] == "birthday"
    ]
    assert birthdays == ["1.1.2010"], birthdays


def test_restoring_superseded_birthday_while_replacement_is_current_refused(
    client: TestClient, clean_memory: None
) -> None:
    """The restore guard holds for displaced rows too: while the replacement
    is still current, restoring the old birthday would create a second
    current value — refused with 409 (the client's undo deletes the
    replacement FIRST, then restores)."""
    entity = _add_entity(client, name="Noa")
    old = _add_fact(client, entity["id"], predicate="birthday", value="1.1.2010")
    made = client.post(
        f"{_ENDPOINT}/facts",
        json={
            "subject_id": entity["id"],
            "predicate": "birthday",
            "value": "2.2.2011",
        },
    )
    assert made.status_code == 201, made.text

    response = client.post(f"{_ENDPOINT}/facts/{old['id']}/restore")

    assert response.status_code == 409, response.text


def test_rename_kid_migrates_applies_to_values(client: TestClient, clean_memory: None) -> None:
    """applies_to facts store the kid NAME, so renaming a kid must migrate
    that entity's applies_to values in the same transaction — otherwise the
    ask kid-recall leg silently loses every channel."""
    kid = _add_entity(client, name="Noa")
    _add_fact(client, kid["id"], predicate="grade", value="4")
    channel = _add_entity(client, name="דמו: קבוצה א׳", kind="channel")
    _add_fact(
        client,
        channel["id"],
        predicate="applies_to",
        value="Noa",
    )

    response = client.patch(
        f"{_ENDPOINT}/entities/{kid['id']}",
        json={"name": "Noa Levi"},
    )

    assert response.status_code == 200, response.text
    links = [
        f["value"]
        for f in client.get(_ENDPOINT).json()["entities"]
        if f["id"] == channel["id"]
        for f in f["facts"]
        if f["predicate"] == "applies_to"
    ]
    assert links == ["Noa Levi"], links
    with _memory_service_for_tests() as service:
        assert "דמו: קבוצה א׳" in service.channel_names_for_kid("Noa Levi")


def test_rename_refuses_an_existing_name(client: TestClient, clean_memory: None) -> None:
    """A rename to a name another entity already carries is refused (409):
    the screen's duplicate-open rule relies on names staying unique."""
    noa = _add_entity(client, name="Noa")
    rona = _add_entity(client, name="Rona")

    response = client.patch(
        f"{_ENDPOINT}/entities/{noa['id']}",
        json={"name": "Rona"},
    )

    assert response.status_code == 409, response.text
    assert rona["id"]


def test_add_memory_with_a_title_stores_it_in_provenance(
    client: TestClient, clean_memory: None
) -> None:
    """Minor a (PR #136 review): the add-memory form gains an optional Title.
    memory_facts has NO title column — the smallest no-migration storage is
    provenance['title'] (a JSONB dict every row already carries, and which
    PATCH copies forward, so edits keep the title)."""
    entity = _add_entity(client, name="Noa")

    response = client.post(
        f"{_ENDPOINT}/facts",
        json={
            "subject_id": entity["id"],
            "predicate": "note",
            "value": "She danced all evening",
            "title": "Dance recital",
        },
    )

    assert response.status_code == 201, response.text
    saved = response.json()
    assert saved["provenance"].get("title") == "Dance recital", saved

    edited = client.patch(
        f"{_ENDPOINT}/facts/{saved['id']}",
        json={"value": "She danced twice"},
    )
    assert edited.status_code == 200, edited.text
    assert edited.json()["provenance"].get("title") == "Dance recital", edited.json()


def test_alias_containing_a_slash_can_be_removed(client: TestClient, clean_memory: None) -> None:
    """An alias like 'a/b' must be removable: the path segment arrives
    percent-encoded and the route must decode it (it 404'd before)."""
    entity = _add_entity(client, name="Noa")
    response = client.post(f"{_ENDPOINT}/entities/{entity['id']}/aliases", json={"alias": "a/b"})
    assert response.status_code == 201, response.text
    stored = client.get(_ENDPOINT).json()["entities"][0]["aliases"]
    assert "a/b" in stored, f"alias not stored: {stored}"

    removed = client.delete(f"{_ENDPOINT}/entities/{entity['id']}/aliases/a%2Fb")

    assert removed.status_code == 204, removed.text
    names = client.get(_ENDPOINT).json()["entities"][0]["aliases"]
    assert "a/b" not in names


def test_alias_with_a_percent_sequence_is_not_double_decoded(
    client: TestClient, clean_memory: None
) -> None:
    """A literal '%' in an alias must survive the round trip ('R%41' must
    not arrive as 'RA'). The route's extra unquote was one offender; the
    other is TestClient itself, which unquotes scope['path'] a second time
    (httpx's url.path is already decoded). uvicorn delivers scope['path']
    decoded EXACTLY once (ASGI spec), so the honest boundary is
    httpx.ASGITransport, which passes url.path through untouched."""
    entity = _add_entity(client, name="Noa")
    added = client.post(
        f"{_ENDPOINT}/entities/{entity['id']}/aliases",
        json={"alias": "R%41"},
    )
    assert added.status_code == 201, added.text
    stored = client.get(_ENDPOINT).json()["entities"][0]["aliases"]
    assert "R%41" in stored, f"alias not stored: {stored}"

    async def remove() -> httpx.Response:
        transport = httpx.ASGITransport(app=create_app())
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://test",
        ) as asgi:
            return await asgi.delete(
                f"{_ENDPOINT}/entities/{entity['id']}/aliases/R%2541",
            )

    removed = asyncio.run(remove())

    assert removed.status_code == 204, removed.text
    names = client.get(_ENDPOINT).json()["entities"][0]["aliases"]
    assert "R%41" not in names, names


def test_alias_ending_in_a_slash_is_removable(client: TestClient, clean_memory: None) -> None:
    """The route's rstrip('/') corrupted aliases that genuinely end in a
    slash ('kids/' removed 'kids' or 404'd) — a trailing slash is part of
    the name, not noise."""
    entity = _add_entity(client, name="Noa")
    added = client.post(
        f"{_ENDPOINT}/entities/{entity['id']}/aliases",
        json={"alias": "kids/"},
    )
    assert added.status_code == 201, added.text
    stored = client.get(_ENDPOINT).json()["entities"][0]["aliases"]
    assert "kids/" in stored, f"alias not stored: {stored}"

    removed = client.delete(f"{_ENDPOINT}/entities/{entity['id']}/aliases/kids%2F")

    assert removed.status_code == 204, removed.text
    names = client.get(_ENDPOINT).json()["entities"][0]["aliases"]
    assert "kids/" not in names, names


def test_add_fact_refuses_a_duplicate_connection(client: TestClient, clean_memory: None) -> None:
    """Two identical connection:child links to the same person are the same
    fact twice — the second must be refused (409), not silently doubled."""
    entity = _add_entity(client, name="Noa")
    rona = _add_entity(client, name="Rona")
    _add_fact(client, entity["id"], predicate="connection:child", object_id=rona["id"])
    response = client.post(
        f"{_ENDPOINT}/facts",
        json={
            "subject_id": entity["id"],
            "predicate": "connection:child",
            "object_id": rona["id"],
        },
    )

    assert response.status_code == 409, response.text
    links = [
        f
        for f in client.get(_ENDPOINT).json()["entities"][0]["facts"]
        if f["predicate"] == "connection:child" and f["object_id"] == rona["id"]
    ]
    assert len(links) == 1


def test_patch_fact_refuses_a_duplicate_connection_link(
    client: TestClient, clean_memory: None
) -> None:
    """PATCHing a link's object_id onto a person already linked by the same
    predicate creates the same fact twice — refused (409), like a second
    POST would be."""
    entity = _add_entity(client, name="Noa")
    rona = _add_entity(client, name="Rona")
    dan = _add_entity(client, name="Dan")
    _add_fact(client, entity["id"], predicate="connection:child", object_id=rona["id"])
    to_dan = _add_fact(
        client,
        entity["id"],
        predicate="connection:child",
        object_id=dan["id"],
    )

    response = client.patch(
        f"{_ENDPOINT}/facts/{to_dan['id']}",
        json={"object_id": rona["id"]},
    )

    assert response.status_code == 409, response.text
    rona_links = [
        f
        for f in client.get(_ENDPOINT).json()["entities"][0]["facts"]
        if f["predicate"] == "connection:child" and f["object_id"] == rona["id"]
    ]
    assert len(rona_links) == 1, rona_links


def test_confirming_a_duplicate_connection_link_is_refused(
    client: TestClient, clean_memory: None
) -> None:
    """Confirming a proposed link that duplicates a current one would stack
    the same fact twice — refused (409); the proposal can be deleted instead."""
    entity = _add_entity(client, name="Noa")
    rona = _add_entity(client, name="Rona")
    _add_fact(client, entity["id"], predicate="connection:child", object_id=rona["id"])
    duplicate = _add_fact(
        client,
        entity["id"],
        predicate="connection:child",
        object_id=rona["id"],
        status="proposed",
    )

    response = client.post(f"{_ENDPOINT}/facts/{duplicate['id']}/confirm")

    assert response.status_code == 409, response.text
    links = [
        f
        for f in client.get(_ENDPOINT).json()["entities"][0]["facts"]
        if f["predicate"] == "connection:child" and f["object_id"] == rona["id"]
    ]
    assert len(links) == 1, links
