"""Tests for the /capture endpoint (paste-aware capture; needs Postgres).

``occurred_at`` is an aware UTC instant: zone-less pasted times are
interpreted in the ``TIMEZONE`` setting (pinned to Asia/Jerusalem here so the
tests are machine-independent); explicit offsets are honored as given.
"""

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from brain.config import Settings, get_settings
from brain.main import create_app

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

_OUTLOOK_EMAIL = (
    "From: Vendor <vendor@corp.com>\n"
    "To: me@example.com\n"
    "Subject: DPA signature\n"
    "Date: Wed, 22 Oct 2026 09:30:00 +0300\n"
    "\n"
    "Please sign the attached DPA."
)


@pytest.fixture(name="jerusalem_client")
def jerusalem_client_fixture(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    """A client whose TIMEZONE is pinned (zone-less pastes -> aware UTC)."""
    monkeypatch.setenv("TIMEZONE", "Asia/Jerusalem")
    get_settings.cache_clear()
    yield TestClient(create_app())
    get_settings.cache_clear()


def _capture(client: TestClient, payload: dict) -> dict:
    response = client.post(_ENDPOINT, json=payload)
    assert response.status_code == 201, response.text
    return response.json()


def _instant(iso: str) -> datetime:
    """Parse an ISO-8601 timestamp ('Z' or offset form) for instant compares."""
    return datetime.fromisoformat(iso)


def test_capture_whatsapp_joins_thread_by_default(
    jerusalem_client: TestClient, clean_items: None
) -> None:
    body = _capture(jerusalem_client, {"text": _WHATSAPP_THREAD})

    assert body["kind"] == "whatsapp"
    assert body["extracted"]["count"] == 2
    assert body["extracted"]["senders"] == ["Dana", "Rami"]
    assert len(body["items"]) == 1
    item = body["items"][0]
    assert item["source"] == "whatsapp"
    # 09:00 Israel (IDT, +03:00) == 06:00Z
    assert _instant(item["occurred_at"]) == datetime(2026, 10, 21, 6, 0, tzinfo=UTC)
    assert item["meta"]["count"] == 2
    assert item["meta"]["messages"][0]["sender"] == "Dana"
    assert item["meta"]["messages"][0]["at_raw"] == "21/10/2026, 09:00:00"


def test_capture_persists_occurred_at_and_meta(
    jerusalem_client: TestClient, clean_items: None
) -> None:
    body = _capture(jerusalem_client, {"text": _WHATSAPP_THREAD})

    fetched = jerusalem_client.get(f"/items/{body['items'][0]['id']}").json()

    assert _instant(fetched["occurred_at"]) == datetime(2026, 10, 21, 6, 0, tzinfo=UTC)
    assert fetched["meta"]["messages"][1]["sender"] == "Rami"
    assert fetched["meta"]["messages"][1]["at_raw"] == "21/10/2026, 09:01:00"


def test_capture_zone_less_time_round_trips_to_correct_utc_instant(
    jerusalem_client: TestClient, clean_items: None
) -> None:
    """Zone-less WhatsApp time -> stored as the correct aware UTC instant."""
    body = _capture(jerusalem_client, {"text": "[21/10/2026, 09:00:00] Dana: hi"})

    stored = jerusalem_client.get(f"/items/{body['items'][0]['id']}").json()

    assert _instant(stored["occurred_at"]) == datetime(2026, 10, 21, 6, 0, tzinfo=UTC)


def test_capture_explicit_offset_round_trips_to_correct_utc_instant(
    jerusalem_client: TestClient, clean_items: None
) -> None:
    """Outlook +0300 is honored as given (not re-interpreted in TIMEZONE)."""
    body = _capture(jerusalem_client, {"text": _OUTLOOK_EMAIL})

    stored = jerusalem_client.get(f"/items/{body['items'][0]['id']}").json()

    assert _instant(stored["occurred_at"]) == datetime(2026, 10, 22, 6, 30, tzinfo=UTC)
    assert stored["meta"]["date"] == "Wed, 22 Oct 2026 09:30:00 +0300"  # raw, audit


def test_capture_timezone_setting_changes_interpretation(clean_items: None) -> None:
    """The same zone-less paste maps to different UTC instants per TIMEZONE."""
    instants: dict[str, datetime] = {}
    for name, zone in (("jerusalem", "Asia/Jerusalem"), ("new_york", "America/New_York")):
        body = _capture_with_timezone(zone, {"text": "[21/10/2026, 09:00:00] Dana: hi"})
        instants[name] = _instant(body["items"][0]["occurred_at"])

    # 09:00 IDT (+03:00) vs 09:00 EDT (-04:00): 7 hours apart
    assert instants["jerusalem"] == datetime(2026, 10, 21, 6, 0, tzinfo=UTC)
    assert instants["new_york"] == datetime(2026, 10, 21, 13, 0, tzinfo=UTC)


def _capture_with_timezone(zone: str, payload: dict | None = None) -> dict:
    """POST /capture with TIMEZONE temporarily set (own client, own settings)."""
    import os

    previous = os.environ.get("TIMEZONE")
    os.environ["TIMEZONE"] = zone
    get_settings.cache_clear()
    try:
        client = TestClient(create_app())
        response = client.post(_ENDPOINT, json=payload or {"text": "[21/10/2026, 09:00:00] D: x"})
        assert response.status_code == 201, response.text
        return response.json()
    finally:
        if previous is None:
            os.environ.pop("TIMEZONE", None)
        else:
            os.environ["TIMEZONE"] = previous
        get_settings.cache_clear()


def test_capture_email_keeps_full_text_and_headers(
    jerusalem_client: TestClient, clean_items: None
) -> None:
    body = _capture(jerusalem_client, {"text": _GMAIL_FORWARD})

    assert body["kind"] == "email"
    assert body["extracted"]["subject"] == "Quarterly OKR draft"
    assert body["extracted"]["from"] == "Ada Levi <ada@example.com>"
    item = body["items"][0]
    # "10:14 AM" in TIMEZONE (Israel, IDT) -> 07:14Z
    assert _instant(item["occurred_at"]) == datetime(2026, 10, 21, 7, 14, tzinfo=UTC)
    assert item["raw_text"].startswith("---------- Forwarded message")


def test_capture_split_yields_one_item_per_message(
    jerusalem_client: TestClient, clean_items: None
) -> None:
    body = _capture(jerusalem_client, {"text": _WHATSAPP_THREAD, "split": True})

    assert [item["meta"]["messages"][0]["sender"] for item in body["items"]] == [
        "Dana",
        "Rami",
    ]
    assert _instant(body["items"][0]["occurred_at"]) == datetime(2026, 10, 21, 6, 0, tzinfo=UTC)
    assert _instant(body["items"][1]["occurred_at"]) == datetime(2026, 10, 21, 6, 1, tzinfo=UTC)


def test_capture_calendar_text(jerusalem_client: TestClient, clean_items: None) -> None:
    body = _capture(
        jerusalem_client,
        {"text": "Team sync\nTuesday, October 21 · 10:00 – 11:00am\nMeeting room 2"},
    )

    assert body["kind"] == "calendar"
    assert body["extracted"]["title"] == "Team sync"
    assert body["extracted"]["location"] == "Meeting room 2"
    assert _instant(body["items"][0]["occurred_at"]).hour == 7  # 10:00 IDT -> 07:00Z


def test_capture_note_falls_back_to_plain_item(
    jerusalem_client: TestClient, clean_items: None
) -> None:
    body = _capture(jerusalem_client, {"text": "Read the RAG paper and take notes"})

    assert body["kind"] == "note"
    assert body["extracted"] == {}
    assert body["items"][0]["meta"] == {}
    assert body["items"][0]["occurred_at"] is None
    assert body["items"][0]["status"] == "inbox"


def test_capture_invalid_kind_hint_is_422(
    jerusalem_client: TestClient, clean_items: None
) -> None:
    response = jerusalem_client.post(_ENDPOINT, json={"text": "hi", "kind_hint": "tweet"})

    assert response.status_code == 422


def test_capture_empty_text_is_422(jerusalem_client: TestClient, clean_items: None) -> None:
    response = jerusalem_client.post(_ENDPOINT, json={"text": ""})

    assert response.status_code == 422


class TestTimezoneSetting:
    def test_default_resolves_to_a_valid_iana_zone(self):
        settings = Settings()

        assert ZoneInfo(settings.timezone) is not None  # does not raise
        assert "/" in settings.timezone or settings.timezone == "UTC"

    def test_invalid_zone_is_rejected(self):
        with pytest.raises(ValidationError, match="IANA"):
            Settings(timezone="Mars/Olympus_Mons")


def expected_local(zone: ZoneInfo) -> datetime:  # pragma: no cover - helper
    """Aware-UTC instant of 2026-10-21 10:00 wall time in the given zone."""
    return datetime(2026, 10, 21, 10, 0).replace(tzinfo=zone).astimezone(UTC)
