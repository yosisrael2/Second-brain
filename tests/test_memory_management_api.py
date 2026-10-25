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

import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime
from zoneinfo import ZoneInfo

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


def test_family_summary_week_starts_local_monday_midnight(
    clean_memory: None, monkeypatch
) -> None:
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


def test_family_summary_hides_noise_and_superseded(
    client: TestClient, clean_memory: None
) -> None:
    """Superseded and triage-noise items in the week are absent — the same
    default-hidden conditions every other listing uses (#98)."""
    noa = _add_entity(client, name="Noa")
    _add_fact(client, noa["id"], predicate="grade", value="ה")
    _visible = _seed_item_raw(raw_text="visible item")  # kept
    replacement = _seed_item_raw(raw_text="current wording")
    superseded = _seed_item_raw(
        raw_text="old wording", superseded_by=str(replacement)
    )
    noise = _seed_item_raw(
        raw_text="triage noise", extraction_meta='{"cancelled": true}'
    )

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
