"""Tests for the /capture endpoint (paste-aware capture; needs Postgres)."""

from datetime import datetime

from fastapi.testclient import TestClient

_ENDPOINT = "/capture"

_WHATSAPP_THREAD = "[21/10/2026, 09:00:00] Dana: one\n[21/10/2026, 09:01:00] Rami: two"

_GMAIL_FORWARD = (
    "---------- Forwarded message ---------\n"
    "From: Ada Levi <ada@example.com>\n"
    "Date: Tue, Oct 21, 2026 at 10:14 AM\n"
    "Subject: Quarterly OKR draft\n"
    "To: <team@example.com>\n"
    "\n"
    "Hi team,\n"
    "Please review the draft before Thursday.\n"
    "— Ada"
)


def test_capture_whatsapp_joins_thread_by_default(
    client: TestClient, clean_items: None
) -> None:
    body = _capture(client, {"text": _WHATSAPP_THREAD})

    assert body["kind"] == "whatsapp"
    assert body["extracted"]["count"] == 2
    assert body["extracted"]["senders"] == ["Dana", "Rami"]
    assert len(body["items"]) == 1
    item = body["items"][0]
    assert item["source"] == "whatsapp"
    assert item["occurred_at"] == "2026-10-21T09:00:00"
    assert item["meta"]["count"] == 2
    assert item["meta"]["messages"][0] == {"sender": "Dana", "at": "2026-10-21T09:00:00"}


def test_capture_persists_occurred_at_and_meta(client: TestClient, clean_items: None) -> None:
    body = _capture(client, {"text": _WHATSAPP_THREAD})

    fetched = client.get(f"/items/{body['items'][0]['id']}").json()

    assert fetched["occurred_at"] == "2026-10-21T09:00:00"
    assert fetched["meta"]["messages"][1] == {"sender": "Rami", "at": "2026-10-21T09:01:00"}


def test_capture_email_keeps_full_text_and_headers(
    client: TestClient, clean_items: None
) -> None:
    body = _capture(client, {"text": _GMAIL_FORWARD})

    assert body["kind"] == "email"
    assert body["extracted"]["subject"] == "Quarterly OKR draft"
    assert body["extracted"]["from"] == "Ada Levi <ada@example.com>"
    item = body["items"][0]
    assert item["occurred_at"] == "2026-10-21T10:14:00"
    assert item["raw_text"].startswith("---------- Forwarded message")


def test_capture_split_yields_one_item_per_message(
    client: TestClient, clean_items: None
) -> None:
    body = _capture(client, {"text": _WHATSAPP_THREAD, "split": True})

    assert [item["meta"]["messages"][0]["sender"] for item in body["items"]] == [
        "Dana",
        "Rami",
    ]
    assert body["items"][0]["occurred_at"] == "2026-10-21T09:00:00"
    assert body["items"][1]["occurred_at"] == "2026-10-21T09:01:00"


def test_capture_calendar_text(client: TestClient, clean_items: None) -> None:
    body = _capture(
        client,
        {"text": "Team sync\nTuesday, October 21 · 10:00 – 11:00am\nMeeting room 2"},
    )

    assert body["kind"] == "calendar"
    assert body["extracted"]["title"] == "Team sync"
    assert body["extracted"]["location"] == "Meeting room 2"
    assert datetime.fromisoformat(body["items"][0]["occurred_at"]).hour == 10


def test_capture_note_falls_back_to_plain_item(
    client: TestClient, clean_items: None
) -> None:
    body = _capture(client, {"text": "Read the RAG paper and take notes"})

    assert body["kind"] == "note"
    assert body["extracted"] == {}
    assert body["items"][0]["meta"] == {}
    assert body["items"][0]["occurred_at"] is None
    assert body["items"][0]["status"] == "inbox"


def test_capture_timezone_aware_email_date(client: TestClient, clean_items: None) -> None:
    body = _capture(
        client,
        {
            "text": (
                "From: Vendor <vendor@corp.com>\n"
                "To: me@example.com\n"
                "Subject: DPA signature\n"
                "Date: Wed, 22 Oct 2026 09:30:00 +0300\n"
                "\n"
                "Please sign the attached DPA."
            )
        },
    )

    assert body["items"][0]["occurred_at"] == "2026-10-22T09:30:00"


def test_capture_invalid_kind_hint_is_422(client: TestClient, clean_items: None) -> None:
    response = client.post(_ENDPOINT, json={"text": "hi", "kind_hint": "tweet"})

    assert response.status_code == 422


def test_capture_empty_text_is_422(client: TestClient, clean_items: None) -> None:
    response = client.post(_ENDPOINT, json={"text": ""})

    assert response.status_code == 422


def _capture(client: TestClient, payload: dict) -> dict:
    response = client.post(_ENDPOINT, json=payload)
    assert response.status_code == 201, response.text
    return response.json()
