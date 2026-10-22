"""Tests for the /memory endpoints (need the docker-compose Postgres).

Covers every route plus the versioning semantics of facts:
PATCH closes the old version and opens a new one (history kept), DELETE is
soft (status=rejected, valid_to set), confirm only moves proposed->confirmed.
"""

import uuid

from fastapi.testclient import TestClient

_ENDPOINT = "/memory"

KINDS = ["person", "group", "place", "org"]


def _add_entity(client: TestClient, name: str = "Rona", kind: str = "person", **overrides) -> dict:
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


# --- GET /memory --------------------------------------------------------------


def test_get_memory_empty(client: TestClient, clean_memory: None) -> None:
    response = client.get(_ENDPOINT)

    assert response.status_code == 200
    assert response.json() == {"entities": []}


def test_get_memory_groups_facts_by_entity(client: TestClient, clean_memory: None) -> None:
    rona = _add_entity(client, name="Rona")
    school = _add_entity(client, name="ה' ספיר", kind="group")
    _add_fact(client, rona["id"], predicate="grade", value="5")
    _add_fact(client, rona["id"], predicate="gender", value="girl")
    _add_fact(client, school["id"], predicate="teacher", value="Rinat")

    body = client.get(_ENDPOINT).json()

    by_name = {entity["name"]: entity for entity in body["entities"]}
    assert set(by_name) == {"Rona", "ה' ספיר"}
    assert {(f["predicate"], f["value"]) for f in by_name["Rona"]["facts"]} == {
        ("grade", "5"),
        ("gender", "girl"),
    }
    assert by_name["ה' ספיר"]["facts"][0]["predicate"] == "teacher"


def test_get_memory_status_filter_proposed_excludes_confirmed(
    client: TestClient, clean_memory: None
) -> None:
    rona = _add_entity(client, name="Rona")
    itamar = _add_entity(client, name="Itamar")
    _add_fact(client, rona["id"])  # default status: confirmed
    _add_fact(client, itamar["id"], status="proposed")

    proposed = client.get(_ENDPOINT, params={"status": "proposed"}).json()
    confirmed = client.get(_ENDPOINT).json()

    assert [e["name"] for e in proposed["entities"]] == ["Itamar"]  # no empty groups
    # the default view still lists every entity; Itamar carries no confirmed facts
    assert {e["name"] for e in confirmed["entities"]} == {"Rona", "Itamar"}
    itamar_view = next(e for e in confirmed["entities"] if e["name"] == "Itamar")
    assert itamar_view["facts"] == []


def test_get_memory_search_q_filters(client: TestClient, clean_memory: None) -> None:
    _add_entity(client, name="Rona")
    _add_entity(client, name="Itamar")

    body = client.get(_ENDPOINT, params={"q": "rona"}).json()

    assert [entity["name"] for entity in body["entities"]] == ["Rona"]


def test_get_memory_rejects_bad_status(client: TestClient, clean_memory: None) -> None:
    assert client.get(_ENDPOINT, params={"status": "rejected"}).status_code == 422
    assert client.get(_ENDPOINT, params={"status": "all"}).status_code == 422


# --- POST/PATCH/DELETE /memory/entities ----------------------------------------


def test_create_entity_returns_201_with_payload(client: TestClient, clean_memory: None) -> None:
    response = client.post(
        f"{_ENDPOINT}/entities",
        json={"kind": "person", "name": "Rona", "aliases": ["רונה"]},
    )

    assert response.status_code == 201
    body = response.json()
    assert body["name"] == "Rona"
    assert body["kind"] == "person"
    assert body["aliases"] == ["רונה"]
    assert uuid.UUID(body["id"])


def test_create_entity_rejects_invalid(client: TestClient, clean_memory: None) -> None:
    cases = [
        {"kind": "person", "name": ""},  # empty name
        {"kind": "person"},  # name missing
        {"kind": "alien", "name": "x"},  # not an entity kind
        {"name": "x"},  # kind missing
        {"kind": "person", "name": "x", "aliases": [""]},  # empty alias
    ]
    for payload in cases:
        response = client.post(f"{_ENDPOINT}/entities", json=payload)
        assert response.status_code == 422, f"{payload} -> {response.status_code}"


def test_patch_entity_renames(client: TestClient, clean_memory: None) -> None:
    entity = _add_entity(client, name="Rona")

    response = client.patch(f"{_ENDPOINT}/entities/{entity['id']}", json={"name": "Rona C."})

    assert response.status_code == 200
    assert response.json()["name"] == "Rona C."


def test_patch_entity_not_found(client: TestClient, clean_memory: None) -> None:
    response = client.patch(
        f"{_ENDPOINT}/entities/{uuid.uuid4()}", json={"name": "nobody"}
    )

    assert response.status_code == 404


def test_delete_entity_cascades(client: TestClient, clean_memory: None) -> None:
    entity = _add_entity(client, name="Rona")
    _add_fact(client, entity["id"])

    response = client.delete(f"{_ENDPOINT}/entities/{entity['id']}")

    assert response.status_code == 204
    assert client.get(_ENDPOINT).json() == {"entities": []}


def test_delete_entity_not_found(client: TestClient, clean_memory: None) -> None:
    assert client.delete(f"{_ENDPOINT}/entities/{uuid.uuid4()}").status_code == 404


# --- POST /memory/facts ---------------------------------------------------------


def test_create_fact_returns_201(client: TestClient, clean_memory: None) -> None:
    rona = _add_entity(client, name="Rona")

    response = client.post(
        f"{_ENDPOINT}/facts",
        json={"subject_id": rona["id"], "predicate": "class", "value": "ה' ספיר"},
    )

    assert response.status_code == 201
    body = response.json()
    assert body["predicate"] == "class"
    assert body["value"] == "ה' ספיר"
    assert body["status"] == "confirmed"  # default
    assert body["valid_to"] is None  # current version
    assert body["supersedes_id"] is None
    assert uuid.UUID(body["id"])


def test_create_fact_rejects_empty_object_and_value(client: TestClient, clean_memory: None) -> None:
    rona = _add_entity(client, name="Rona")

    response = client.post(
        f"{_ENDPOINT}/facts",
        json={"subject_id": rona["id"], "predicate": "grade"},
    )

    assert response.status_code == 422  # DB CHECK: object or value required


def test_create_fact_unknown_subject_404(client: TestClient, clean_memory: None) -> None:
    response = client.post(
        f"{_ENDPOINT}/facts",
        json={"subject_id": str(uuid.uuid4()), "predicate": "grade", "value": "5"},
    )

    assert response.status_code == 404


# --- PATCH /memory/facts/{id}: versioning semantics ------------------------------


def test_patch_fact_closes_old_and_opens_new(client: TestClient, clean_memory: None) -> None:
    rona = _add_entity(client, name="Rona")
    fact = _add_fact(client, rona["id"], value="5")

    response = client.patch(f"{_ENDPOINT}/facts/{fact['id']}", json={"value": "6"})

    assert response.status_code == 200
    new_version = response.json()
    assert new_version["id"] != fact["id"]  # a NEW row, never in-place
    assert new_version["value"] == "6"
    assert new_version["supersedes_id"] == fact["id"]
    assert new_version["valid_to"] is None  # the current version
    # the overview shows the NEW version only; the old one lives in history
    overview = client.get(_ENDPOINT).json()
    facts = {f["id"]: f for e in overview["entities"] for f in e["facts"]}
    assert fact["id"] not in facts  # closed fact hidden from the overview
    assert new_version["id"] in facts
    chain = client.get(f"{_ENDPOINT}/facts/{fact['id']}/history").json()
    assert [f["id"] for f in chain] == [fact["id"], new_version["id"]]
    assert chain[0]["valid_to"] is not None  # old version closed


def test_patch_fact_absent_fields_inherited(client: TestClient, clean_memory: None) -> None:
    rona = _add_entity(client, name="Rona")
    fact = _add_fact(client, rona["id"], predicate="grade", value="5")

    response = client.patch(f"{_ENDPOINT}/facts/{fact['id']}", json={"value": "6"})

    assert response.json()["predicate"] == "grade"  # unchanged


def test_patch_fact_not_found(client: TestClient, clean_memory: None) -> None:
    response = client.patch(f"{_ENDPOINT}/facts/{uuid.uuid4()}", json={"value": "x"})

    assert response.status_code == 404


# --- DELETE /memory/facts/{id}: soft delete --------------------------------------


def test_delete_fact_is_soft_history_kept(client: TestClient, clean_memory: None) -> None:
    rona = _add_entity(client, name="Rona")
    fact = _add_fact(client, rona["id"])

    response = client.delete(f"{_ENDPOINT}/facts/{fact['id']}")

    assert response.status_code == 204
    # the row is NOT hard-deleted: the history chain keeps it, marked rejected
    chain = client.get(f"{_ENDPOINT}/facts/{fact['id']}/history").json()
    assert [f["id"] for f in chain] == [fact["id"]]
    assert chain[0]["status"] == "rejected"
    assert chain[0]["valid_to"] is not None
    # and it disappears from the confirmed overview (only history keeps it)
    confirmed = client.get(_ENDPOINT).json()
    assert all(f["id"] != fact["id"] for e in confirmed["entities"] for f in e["facts"])


def test_delete_fact_twice_conflicts(client: TestClient, clean_memory: None) -> None:
    rona = _add_entity(client, name="Rona")
    fact = _add_fact(client, rona["id"])
    assert client.delete(f"{_ENDPOINT}/facts/{fact['id']}").status_code == 204

    response = client.delete(f"{_ENDPOINT}/facts/{fact['id']}")

    assert response.status_code == 409


def test_delete_fact_not_found(client: TestClient, clean_memory: None) -> None:
    assert client.delete(f"{_ENDPOINT}/facts/{uuid.uuid4()}").status_code == 404


# --- POST /memory/facts/{id}/confirm ---------------------------------------------


def test_confirm_fact_proposed_to_confirmed(client: TestClient, clean_memory: None) -> None:
    rona = _add_entity(client, name="Rona")
    fact = _add_fact(client, rona["id"], status="proposed")

    response = client.post(f"{_ENDPOINT}/facts/{fact['id']}/confirm")

    assert response.status_code == 200
    assert response.json()["status"] == "confirmed"
    assert response.json()["id"] == fact["id"]  # in-place status flip, same row


def test_confirm_fact_already_confirmed_conflicts(client: TestClient, clean_memory: None) -> None:
    rona = _add_entity(client, name="Rona")
    fact = _add_fact(client, rona["id"])  # confirmed by default

    response = client.post(f"{_ENDPOINT}/facts/{fact['id']}/confirm")

    assert response.status_code == 409


def test_confirm_fact_not_found(client: TestClient, clean_memory: None) -> None:
    assert client.post(f"{_ENDPOINT}/facts/{uuid.uuid4()}/confirm").status_code == 404


# --- GET /memory/facts/{id}/history ----------------------------------------------


def test_fact_history_walks_the_version_chain(client: TestClient, clean_memory: None) -> None:
    rona = _add_entity(client, name="Rona")
    v1 = _add_fact(client, rona["id"], value="5")
    v2 = client.patch(f"{_ENDPOINT}/facts/{v1['id']}", json={"value": "6"}).json()
    v3 = client.patch(f"{_ENDPOINT}/facts/{v2['id']}", json={"value": "7"}).json()

    response = client.get(f"{_ENDPOINT}/facts/{v2['id']}/history")

    assert response.status_code == 200
    chain = response.json()
    assert [fact["id"] for fact in chain] == [v1["id"], v2["id"], v3["id"]]
    assert [fact["value"] for fact in chain] == ["5", "6", "7"]


def test_fact_history_single_version_fact(client: TestClient, clean_memory: None) -> None:
    rona = _add_entity(client, name="Rona")
    fact = _add_fact(client, rona["id"])

    chain = client.get(f"{_ENDPOINT}/facts/{fact['id']}/history").json()

    assert [f["id"] for f in chain] == [fact["id"]]


def test_fact_history_not_found(client: TestClient, clean_memory: None) -> None:
    assert client.get(f"{_ENDPOINT}/facts/{uuid.uuid4()}/history").status_code == 404
