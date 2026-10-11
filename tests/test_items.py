"""Tests for the /items endpoints (need the docker-compose Postgres)."""

import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from brain.db.session import engine

_ENDPOINT = "/items"


def _create_item(client: TestClient, **overrides: object) -> dict:
    payload: dict = {"raw_text": "Send the weekly report to Alice"}
    payload.update(overrides)
    response = client.post(_ENDPOINT, json=payload)
    assert response.status_code == 201, response.text
    return response.json()


def test_create_item_returns_201_with_payload(client: TestClient, clean_items: None) -> None:
    response = client.post(
        _ENDPOINT,
        json={"raw_text": "Idea: voice capture for notes", "type": "idea", "importance": 4},
    )

    assert response.status_code == 201
    body = response.json()
    assert body["raw_text"] == "Idea: voice capture for notes"
    assert body["type"] == "idea"
    assert body["importance"] == 4
    assert uuid.UUID(body["id"])
    assert body["status"] == "inbox"
    assert body["created_at"] == body["updated_at"]


def test_create_item_applies_defaults(client: TestClient, clean_items: None) -> None:
    body = _create_item(client)

    assert body["source"] == "manual"
    assert body["status"] == "inbox"
    assert body["type"] is None
    assert body["project"] is None
    assert body["importance"] is None
    assert body["actionability"] is None
    assert body["confidence"] is None


def test_create_item_rejects_invalid_fields(client: TestClient, clean_items: None) -> None:
    cases = [
        {"raw_text": ""},  # empty text
        {"raw_text": "x", "importance": 6},  # out of range
        {"raw_text": "x", "importance": 0},
        {"raw_text": "x", "confidence": 1.5},  # out of range
        {"raw_text": "x", "confidence": -0.1},
        {"raw_text": "x", "type": "junk"},  # not an ItemType
        {"raw_text": "x", "status": "done"},  # not an ItemStatus
        {"raw_text": "x", "actionability": "whenever"},  # not an Actionability
    ]
    for payload in cases:
        response = client.post(_ENDPOINT, json=payload)
        assert response.status_code == 422, f"{payload} -> {response.status_code}"


def test_get_item_by_id(client: TestClient, clean_items: None) -> None:
    created = _create_item(client)

    response = client.get(f"{_ENDPOINT}/{created['id']}")

    assert response.status_code == 200
    assert response.json()["id"] == created["id"]


def test_get_item_not_found(client: TestClient, clean_items: None) -> None:
    response = client.get(f"{_ENDPOINT}/{uuid.uuid4()}")

    assert response.status_code == 404
    assert "not found" in response.json()["detail"]


def test_get_item_rejects_non_uuid(client: TestClient, clean_items: None) -> None:
    response = client.get(f"{_ENDPOINT}/not-a-uuid")

    assert response.status_code == 422


def test_list_items_filters_by_type_project_status(client: TestClient, clean_items: None) -> None:
    _create_item(client, raw_text="task one", type="task", project="alpha")
    _create_item(client, raw_text="idea two", type="idea", project="beta", status="filed")
    _create_item(client, raw_text="task three", type="task", project="beta")

    by_type = client.get(_ENDPOINT, params={"type": "task"}).json()
    by_project = client.get(_ENDPOINT, params={"project": "beta"}).json()
    by_status = client.get(_ENDPOINT, params={"status": "filed"}).json()
    combined = client.get(_ENDPOINT, params={"type": "task", "project": "beta"}).json()

    assert [item["raw_text"] for item in by_type] == ["task three", "task one"]  # newest first
    assert {item["raw_text"] for item in by_project} == {"idea two", "task three"}
    assert [item["raw_text"] for item in by_status] == ["idea two"]
    assert [item["raw_text"] for item in combined] == ["task three"]


def test_list_items_rejects_bad_filter_values(client: TestClient, clean_items: None) -> None:
    response = client.get(_ENDPOINT, params={"type": "junk"})

    assert response.status_code == 422


def test_list_items_pagination(client: TestClient, clean_items: None) -> None:
    for i in range(3):
        _create_item(client, raw_text=f"note {i}")

    page = client.get(_ENDPOINT, params={"limit": 2, "offset": 1}).json()

    assert [item["raw_text"] for item in page] == ["note 1", "note 0"]
    assert client.get(_ENDPOINT, params={"limit": 0}).status_code == 422
    assert client.get(_ENDPOINT, params={"offset": -1}).status_code == 422


def test_patch_item_updates_only_given_fields(client: TestClient, clean_items: None) -> None:
    created = _create_item(client, raw_text="Call Bob", project="alpha", type="task")

    response = client.patch(
        f"{_ENDPOINT}/{created['id']}", json={"status": "filed", "importance": 5}
    )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "filed"
    assert body["importance"] == 5
    assert body["project"] == "alpha"  # unchanged
    assert body["type"] == "task"  # unchanged
    assert body["updated_at"] >= body["created_at"]


def test_patch_item_can_clear_nullable_fields(client: TestClient, clean_items: None) -> None:
    created = _create_item(client, raw_text="Call Bob", project="alpha")

    response = client.patch(f"{_ENDPOINT}/{created['id']}", json={"project": None})

    assert response.status_code == 200
    assert response.json()["project"] is None


def test_patch_item_rejects_invalid_values(client: TestClient, clean_items: None) -> None:
    created = _create_item(client)

    response = client.patch(f"{_ENDPOINT}/{created['id']}", json={"importance": 9})

    assert response.status_code == 422


def test_patch_item_not_found(client: TestClient, clean_items: None) -> None:
    response = client.patch(f"{_ENDPOINT}/{uuid.uuid4()}", json={"status": "filed"})

    assert response.status_code == 404


def test_delete_item(client: TestClient, clean_items: None) -> None:
    created = _create_item(client)

    response = client.delete(f"{_ENDPOINT}/{created['id']}")

    assert response.status_code == 204
    assert client.get(f"{_ENDPOINT}/{created['id']}").status_code == 404


def test_delete_item_not_found(client: TestClient, clean_items: None) -> None:
    response = client.delete(f"{_ENDPOINT}/{uuid.uuid4()}")

    assert response.status_code == 404


def test_search_matches_keywords(client: TestClient, clean_items: None) -> None:
    _create_item(client, raw_text="The quarterly budget review is scheduled")
    _create_item(client, raw_text="Buy groceries for dinner")

    response = client.get(f"{_ENDPOINT}/search", params={"q": "budget review"})

    assert response.status_code == 200
    results = response.json()
    assert len(results) == 1
    assert results[0]["raw_text"] == "The quarterly budget review is scheduled"


def test_search_no_matches_returns_empty_list(client: TestClient, clean_items: None) -> None:
    _create_item(client, raw_text="The quarterly budget review is scheduled")

    response = client.get(f"{_ENDPOINT}/search", params={"q": "xylophone"})

    assert response.status_code == 200
    assert response.json() == []


def test_search_requires_query(client: TestClient, clean_items: None) -> None:
    assert client.get(f"{_ENDPOINT}/search").status_code == 422
    assert client.get(f"{_ENDPOINT}/search", params={"q": ""}).status_code == 422



def test_search_matches_words_with_search_operators(client: TestClient, clean_items: None) -> None:
    _create_item(client, raw_text="Plan the offsite agenda")
    _create_item(client, raw_text="Plan the budget")

    response = client.get(f"{_ENDPOINT}/search", params={"q": "offsite OR budget"})

    assert response.status_code == 200
    assert {item["raw_text"] for item in response.json()} == {
        "Plan the offsite agenda",
        "Plan the budget",
    }


def test_db_check_constraints_backstop(clean_items: None) -> None:
    """Invalid enum values are rejected by the DB even bypassing pydantic."""
    with pytest.raises(IntegrityError):
        with engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO memory_items (id, raw_text, source, status) "
                    "VALUES (:id, :raw_text, 'manual', :status)"
                ),
                {"id": uuid.uuid4(), "raw_text": "bad status", "status": "done"},
            )
