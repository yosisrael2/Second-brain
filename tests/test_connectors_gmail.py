"""C5: the Gmail port onto the connector framework.

The connector reuses the ai/gmail.py plumbing (RestGmailClient over an
injected fake service, the pure payload parsers) and the runner owns the
ingest semantics. Every test here is offline: the google service is a fake,
the DB is the worker's brain_w6_test.

Old->new test mapping (tests/services/test_email_poller.py moved 1:1):
  TestListByConstruction.test_every_list_call_carries_the_label
      -> TestQueryPath.test_first_fetch_lists_by_construction_with_the_label
  TestListByConstruction.test_label_config_missing_refuses_to_poll
      -> TestQueryPath.test_missing_label_config_is_a_terminal_error
  TestIngestPath.test_new_message_is_captured_via_paste_path
      -> TestRunnerSemantics.test_new_message_captures_through_the_paste_path
  TestIngestPath.test_seen_message_is_skipped
      -> TestRunnerSemantics.test_seeded_ledger_message_is_not_reingested
  TestIngestPath.test_same_message_twice_ingests_once
      -> TestRunnerSemantics.test_reset_resync_of_known_messages_is_idempotent
  TestIngestPath.test_message_without_rfc_id_uses_fingerprint_dedupe
      -> TestRunnerSemantics.test_same_body_without_rfc_id_dedupes_by_fingerprint
  TestIngestPath.test_attachment_references_stored_in_meta
      -> TestRecordFromMessage.test_record_from_the_committed_fixture (meta asserts)
  TestPollerStates.test_auth_error_sets_needs_re_auth_and_records_event
      -> TestErrorTranslation.test_refresh_error_maps_to_auth_error
  TestPollerStates.test_label_error_sets_label_missing
      -> TestQueryPath.test_invalid_label_is_terminal_not_a_reset_loop
  TestPollerStates.test_transient_error_sets_error_state
      -> TestErrorTranslation.test_http_503_maps_to_retryable
  TestBackoff.* -> covered by tests/test_connectors_runner.py (the runner owns
      backoff) + tests/test_email_cli.py (loop pause policy)
  TestDedupeId.test_rfc_message_id_wins / .fingerprint_is_deterministic...
      -> TestRunnerSemantics.test_message_id_redelivered_under_a_new_gmail_id_dedupes
      (the manager's delta-a: the Message-ID ledger is REPLACED by the ingest
      fingerprint + the gmail_id ledger; the fp backstop lives in the same
      fingerprint path)
  TestLabelMembershipDefence.test_message_without_the_label_is_skipped_and_not_ledgered
      -> TestRunnerSemantics.test_message_that_lost_the_label_is_not_captured_fresh
  TestLabelMembershipDefence.test_message_with_the_label_is_captured
      -> TestRunnerSemantics.test_new_message_captures_through_the_paste_path
  TestGmailIdFastPath.test_known_gmail_id_skips_get_entirely / .unknown... / .second_poll...
      -> TestQueryPath.test_known_gmail_ids_skip_the_message_get
      + TestRunnerSemantics.test_seeded_ledger_message_is_not_reingested (no-GET assert)

F3 (tests/services/test_attachments_email.py moved 1:1):
  test_attachment_bytes_captured_with_source_email
      -> TestRunnerSemantics.test_attachments_flow_through_the_pipeline
  test_too_large_reference_is_skipped_before_the_fetch -> covered by the runner
      (tests/test_connectors_runner.py attachment policy, MAX_FILE_BYTES)
  test_fetch_failure_recorded_never_silent -> the framework policy is STRICTER:
      a failed attachment fails the RECORD (cursor held, retried next sync)
      instead of dropping the bytes silently — tests/test_connectors_runner.py
  test_budget_via_drain -> the gmail-poll loop drains once per cycle (CLI); the
      budget cap itself lives in AttachmentService (tests/test_attachments.py)
"""

from __future__ import annotations

import base64
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from brain.connectors.base import (
    ConnectorAuthError,
    ConnectorError,
    ConnectorRetryableError,
)

FIXTURES = Path(__file__).parent / "fixtures" / "connectors" / "gmail"
LABEL = "Label_school"


def _fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


def _b64(text: str) -> str:
    return base64.urlsafe_b64encode(text.encode()).decode().rstrip("=")


def _msg_fixture(
    mid: str = "m1", rfc: str = "<a1b2c3@mail.gmail.com>", *, with_attachment: bool = False
) -> dict:
    """A users.messages.get response (Google's documented shape), text built
    from the committed fixture with the id/message-id swapped in."""
    raw = _fixture("message_full.json")
    raw["id"] = mid
    raw["payload"]["headers"] = [
        h if h["name"] != "Message-ID" else {"name": "Message-ID", "value": rfc}
        for h in raw["payload"]["headers"]
    ]
    if not with_attachment:
        raw["payload"]["parts"] = [
            p for p in raw["payload"]["parts"] if not p["body"].get("attachmentId")
        ]
    return raw


# --- fake google service (duck-typed; errors carry .resp.status) --------------


class _FakeRequest:
    def __init__(self, result=None, error=None):
        self._result = result
        self._error = error

    def execute(self):
        if self._error is not None:
            raise self._error
        return self._result


class _FakeHttpError(Exception):
    """Duck-typed googleapiclient.errors.HttpError."""

    def __init__(self, status: int, content: bytes = b""):
        super().__init__(f"http {status}")
        self.resp = type("Resp", (), {"status": status})()
        self.content = content


class _FakeMessages:
    def __init__(self, state):
        self._state = state

    def list(self, **kwargs):
        self._state.calls.append(("messages.list", kwargs))
        if self._state.list_error is not None:
            return _FakeRequest(error=self._state.list_error)
        return _FakeRequest(result=self._state.list_result)

    def get(self, userId, id, format="full"):  # noqa: N803, A002
        self._state.calls.append(("messages.get", {"id": id, "format": format}))
        err = self._state.get_errors.get(id)
        if err is not None:
            return _FakeRequest(error=err)
        msg = self._state.messages.get(id)
        if msg is None:
            return _FakeRequest(error=_FakeHttpError(404, b"Requested entity was not found"))
        return _FakeRequest(result=msg)

    def attachments(self):
        # Real API shape: users.messages.attachments.get
        return _FakeAttachments(self._state)


class _FakeAttachments:
    def __init__(self, state):
        self._state = state

    def get(self, userId, messageId, id):  # noqa: N803, A002
        self._state.calls.append(("attachments.get", {"messageId": messageId, "id": id}))
        if self._state.attachment_error is not None:
            return _FakeRequest(error=self._state.attachment_error)
        data = self._state.attachment_blobs.get(id)
        if data is None:
            return _FakeRequest(error=_FakeHttpError(404, b"not found"))
        return _FakeRequest(result={"data": data, "size": len(data)})


class _FakeHistory:
    def __init__(self, state):
        self._state = state

    def list(self, **kwargs):
        self._state.calls.append(("history.list", kwargs))
        if self._state.history_error is not None:
            return _FakeRequest(error=self._state.history_error)
        page_token = kwargs.get("pageToken")
        if page_token:
            page = self._state.history_pages.get(page_token)
            if page is None:
                return _FakeRequest(error=_FakeHttpError(400, b"Invalid pageToken"))
        else:
            page = self._state.history_pages.get("first")
        if page is None:  # nothing scripted: an empty history window
            page = {"history": [], "historyId": self._state.profile_history_id}
        return _FakeRequest(result=page)


class _FakeUsers:
    def __init__(self, state):
        self._state = state

    def messages(self):
        return _FakeMessages(self._state)

    def attachments(self):
        return _FakeAttachments(self._state)

    def history(self):
        return _FakeHistory(self._state)

    def getProfile(self, userId):  # noqa: N803
        self._state.calls.append(("getProfile", {"userId": userId}))
        return _FakeRequest(result={"historyId": self._state.profile_history_id})


class _FakeService:
    def __init__(self, state):
        self._state = state
        self.closed = False

    def users(self):
        return _FakeUsers(self._state)

    def close(self) -> None:
        self.closed = True
        self._state.calls.append(("service.close", {}))


class _ServiceState:
    """Everything the fake service can be scripted with."""

    def __init__(self):
        self.calls: list[tuple[str, dict]] = []
        self.messages: dict[str, dict] = {}
        self.get_errors: dict[str, Exception] = {}
        self.list_result: dict = {"messages": []}
        self.list_error: Exception | None = None
        self.history_pages: dict[str, dict] = {}
        self.history_error: Exception | None = None
        self.profile_history_id = "9002"
        self.attachment_blobs: dict[str, str] = {}
        self.attachment_error: Exception | None = None


def _connector(
    state: _ServiceState,
    *,
    label: str = LABEL,
    query: str = "",
    legacy_seed=None,
    is_known=None,
) -> object:
    from brain.config import Settings
    from brain.connectors.google.gmail import GmailConnector

    return GmailConnector(
        token_loader=lambda: "refresh-token",
        service_factory=lambda token, scopes, api_name, api_version: _FakeService(state),
        settings=Settings(
            gmail_min_call_interval_s=0.0,
            gmail_label_id=label, gmail_query=query, gmail_credentials_path="", _env_file=None
        ),
        legacy_seed=legacy_seed,
        is_known=is_known,
    )


# --- pure record mapping (fixtures) -------------------------------------------


class TestRecordFromMessage:
    def test_record_from_the_committed_fixture(self):
        from brain.ai.gmail import _message_from_payload
        from brain.connectors.google.gmail import record_from_message

        raw = _fixture("message_full.json")
        msg = _message_from_payload(raw)
        record = record_from_message(msg)
        assert record.source == "email"
        assert record.external_id == "m1"
        assert record.version is None  # Gmail messages are immutable
        assert record.title == "School trip tomorrow"
        assert record.url is None
        assert record.deleted is False
        # The body flows through the SAME paste parse as the old poller
        # (kind email: RTL stripped, leading headers kept).
        assert "The trip is tomorrow, please bring a hat." in record.text
        assert "בוקר טוב, הטיול מחר" in record.text
        assert record.occurred_at == datetime(2026, 10, 22, 10, 0, tzinfo=UTC)
        assert record.meta["gmail_id"] == "m1"
        assert record.meta["rfc_message_id"] == "<a1b2c3@mail.gmail.com>"
        assert record.meta["from"] == "teacher@example.com"
        assert record.meta["subject"] == "School trip tomorrow"
        assert record.meta["attachments"] == [
            {
                "attachment_id": "att1",
                "filename": "trip-form.pdf",
                "mime_type": "application/pdf",
                "size": 1234,
            }
        ]
        assert len(record.attachments) == 1
        ref = record.attachments[0]
        assert ref.ref_id == "att1" and ref.filename == "trip-form.pdf"
        assert ref.mime_type == "application/pdf" and ref.size == 1234

    def test_occurred_at_falls_back_to_internal_date(self):
        from brain.ai.gmail import _message_from_payload
        from brain.connectors.google.gmail import record_from_message

        raw = _fixture("message_full.json")
        raw["payload"]["headers"] = [
            h for h in raw["payload"]["headers"] if h["name"] != "Date"
        ]
        msg = _message_from_payload(raw)
        assert msg.internal_date is not None
        record = record_from_message(msg)
        assert record.occurred_at == msg.internal_date


# --- query path (first sync / reset fallback) ---------------------------------


class TestQueryPath:
    def test_first_fetch_lists_by_construction_with_the_label(self):
        state = _ServiceState()
        state.list_result = {"messages": [{"id": "m1", "threadId": "t1"}]}
        state.messages["m1"] = _msg_fixture("m1")
        connector = _connector(state)
        result = connector.fetch(None, limit=200)
        list_calls = [c for c in state.calls if c[0] == "messages.list"]
        assert len(list_calls) == 1
        assert list_calls[0][1]["labelIds"] == [LABEL]  # label present BY CONSTRUCTION
        assert [r.external_id for r in result.records] == ["m1"]
        assert result.reset is False
        assert result.next_cursor == "9002"  # the profile historyId

    def test_query_also_carries_the_configured_query_filter(self):
        state = _ServiceState()
        state.list_result = {"messages": []}
        connector = _connector(state, query="newer_than:30d")
        connector.fetch(None, limit=200)
        list_calls = [c for c in state.calls if c[0] == "messages.list"]
        assert list_calls[0][1].get("q") == "newer_than:30d"

    def test_known_gmail_ids_skip_the_message_get(self):
        state = _ServiceState()
        state.list_result = {"messages": [{"id": "m1", "threadId": "t1"}]}
        state.messages["m1"] = _msg_fixture("m1")
        connector = _connector(state, is_known=lambda gid: gid == "m1")
        connector.fetch(None, limit=200)
        gets = [c for c in state.calls if c[0] == "messages.get"]
        assert gets == []  # the ledger knows m1: no GET at all (old fast path)

    def test_unknown_gmail_ids_are_fetched(self):
        state = _ServiceState()
        state.list_result = {"messages": [{"id": "m1", "threadId": "t1"}]}
        state.messages["m1"] = _msg_fixture("m1")
        connector = _connector(state)
        result = connector.fetch(None, limit=200)
        assert [r.external_id for r in result.records] == ["m1"]

    def test_limit_caps_the_batch_and_drops_the_cursor(self):
        state = _ServiceState()
        state.list_result = {
            "messages": [{"id": "m1", "threadId": "t1"}, {"id": "m2", "threadId": "t2"}]
        }
        state.messages["m1"] = _msg_fixture("m1")
        state.messages["m2"] = _msg_fixture("m2")
        connector = _connector(state)
        result = connector.fetch(None, limit=1)
        assert len(result.records) == 1
        # More messages remain than the limit: no cursor — the next run
        # re-lists from the start (cheap: known ids never GET).
        assert result.next_cursor is None

    def test_missing_label_config_is_a_terminal_error(self):
        state = _ServiceState()
        connector = _connector(state, label="")
        with pytest.raises(ConnectorError):
            connector.fetch(None, limit=200)

    def test_invalid_label_is_terminal_not_a_reset_loop(self):
        state = _ServiceState()
        state.list_error = _FakeHttpError(400, b"Invalid label id: Label_gone")
        connector = _connector(state)
        with pytest.raises(ConnectorError) as excinfo:
            connector.fetch(None, limit=200)
        assert excinfo.value.status == 400


class _LateMailMessages(_FakeMessages):
    """messages.list that injects a late arrival AFTER its page was read:
    m2 lands in the mailbox and the mailbox historyId moves past the
    listing — the exact race the resume cursor must survive."""

    def list(self, **kwargs):
        req = super().list(**kwargs)
        state = self._state
        if not getattr(state, "arrived", False):
            state.arrived = True
            state.messages["m2"] = _msg_fixture("m2")
            state.profile_history_id = "9100"
        return req


class _LateMailUsers(_FakeUsers):
    def messages(self):
        return _LateMailMessages(self._state)


class _LateMailService(_FakeService):
    def users(self):
        return _LateMailUsers(self._state)


class TestResumeCursorOrder:
    """Finding 1: the resume historyId must be taken BEFORE the listing.

    Taken after (the old behavior), a message arriving between the last
    list page and getProfile is in neither the listing nor the future
    history walk (the saved cursor is past its labelsAdded) — silently
    never captured. Taken before, the next sync's history walk re-covers
    the window; the re-delivered m1 is skipped by the ledger.
    """

    def _connector(self, state: _ServiceState):
        from brain.config import Settings
        from brain.connectors.google.gmail import GmailConnector

        return GmailConnector(
            token_loader=lambda: "refresh-token",
            service_factory=lambda token, scopes, api, ver: _LateMailService(state),
            settings=Settings(
                gmail_min_call_interval_s=0.0,
                gmail_label_id=LABEL,
                gmail_query="",
                gmail_credentials_path="",
                _env_file=None,
            ),
        )

    def test_message_arriving_during_the_listing_is_recovered_by_the_next_sync(self):
        state = _ServiceState()
        state.arrived = False
        state.messages = {"m1": _msg_fixture("m1")}
        state.list_result = {"messages": [{"id": "m1"}]}  # single page, no token
        connector = self._connector(state)

        first = connector.fetch(None, limit=200)
        # m2 arrived (and the mailbox historyId moved to 9100) AFTER the
        # listing ran: the saved cursor must predate the arrival.
        assert first.next_cursor == "9002", (
            "resume cursor was taken after the listing — m2 is skipped forever"
        )

        # The next sync walks history from the saved cursor and captures m2.
        state.history_pages = {
            "first": {
                "history": [{"id": "h1", "messagesAdded": [{"message": {"id": "m2"}}]}],
                "historyId": "9100",
            }
        }
        second = connector.fetch(first.next_cursor, limit=200)
        assert "m2" in [r.external_id for r in second.records]


# --- history path (incremental sync) ------------------------------------------


def _history_state() -> _ServiceState:
    state = _ServiceState()
    state.profile_history_id = "9004"
    state.history_pages = {
        "first": _fixture("history_added_page1.json"),
        "tok-page2": _fixture("history_removed_page2.json"),
    }
    return state


class TestHistoryPath:
    def test_messages_added_entry_uses_the_wrapped_payload(self):
        """Gmail's History schema WRAPS every entry message in 'message'
        (labelsAdded/labelsRemoved AND messagesAdded/messagesDeleted/
        messagesTrashed). Reading m['id'] instead of m['message']['id']
        would GET the literal id 'None' -> 404 -> a spurious full reset on
        every real incremental delivery."""
        state = _ServiceState()
        state.profile_history_id = "9003"
        state.history_pages = {"first": _fixture("history_message_added_page1.json")}
        state.messages["m2"] = _msg_fixture("m2")
        connector = _connector(state)
        result = connector.fetch("9002", limit=200)
        assert result.reset is False
        assert [(r.external_id, r.deleted) for r in result.records] == [("m2", False)]

    def test_messages_trashed_entry_marks_the_record_deleted(self):
        state = _ServiceState()
        state.profile_history_id = "9004"
        state.history_pages = {"first": _fixture("history_message_trashed_page1.json")}
        state.messages["m2"] = _msg_fixture("m2")
        connector = _connector(state)
        result = connector.fetch("9003", limit=200)
        assert result.reset is False
        assert [(r.external_id, r.deleted) for r in result.records] == [("m2", True)]

    def test_incremental_fetch_uses_the_saved_cursor_and_the_label(self):
        state = _history_state()
        state.messages["m2"] = _msg_fixture("m2")
        connector = _connector(state)
        result = connector.fetch("9000", limit=200)
        calls = [c for c in state.calls if c[0] == "history.list"]
        assert calls and calls[0][1]["startHistoryId"] == "9000"
        assert calls[0][1]["labelId"] == LABEL  # the label filter is respected
        assert [r.external_id for r in result.records] == ["m2"]
        assert result.reset is False

    def test_cursor_advances_to_the_last_consumed_entry(self):
        state = _history_state()
        state.messages["m2"] = _msg_fixture("m2")
        connector = _connector(state)
        result = connector.fetch("9000", limit=200)
        # Page 1 added m2 (entry id 9001), page 2 removed it (entry 9003):
        # the LAST state wins -> deleted, cursor = last consumed entry.
        assert len(result.records) == 1
        assert result.records[0].deleted is True
        assert result.next_cursor == "9003"

    def test_limit_never_stops_mid_entry(self):
        """One coalesced Gmail entry can carry many messages (dragging a
        whole folder into the label). The limit must stop BETWEEN entries:
        stopping mid-entry would drop the tail of the entry AND advance the
        cursor past it — silent message loss. The batch may exceed the
        limit by one entry; the cursor only covers FULLY emitted entries."""
        state = _ServiceState()
        state.profile_history_id = "9002"
        state.history_pages = {
            "first": {
                "history": [
                    {
                        "id": "9001",
                        "labelsAdded": [
                            {"labelIds": [LABEL], "message": {"id": f"m{i}"}}
                            for i in (1, 2, 3)
                        ],
                    }
                ]
            }
        }
        for i in (1, 2, 3):
            state.messages[f"m{i}"] = _msg_fixture(f"m{i}")
        connector = _connector(state)
        result = connector.fetch("9000", limit=2)
        # The whole entry is emitted despite limit=2 — no silent tail loss.
        assert sorted(r.external_id for r in result.records) == ["m1", "m2", "m3"]
        # The entry was fully emitted, so the cursor may cover it.
        assert result.next_cursor == "9001"

    def test_label_removed_keeps_the_record_text(self):
        """A message leaving the label is DELETED for the source, but the
        record still carries the body so the runner preserves the item text
        and only marks source_deleted."""
        state = _history_state()
        state.messages["m2"] = _msg_fixture("m2")
        connector = _connector(state)
        result = connector.fetch("9000", limit=200)
        record = result.records[0]
        assert record.deleted is True
        assert "The trip is tomorrow" in record.text  # NOT an empty record

    def test_hard_deleted_message_yields_a_deleted_record_without_a_body(self):
        state = _ServiceState()
        state.history_pages = {
            "first": {
                "history": [
                    {"id": "9100", "messagesDeleted": [{"message": {"id": "m3"}}]}
                ],
                "historyId": "9101",
            }
        }
        state.get_errors["m3"] = _FakeHttpError(404, b"Requested entity was not found")
        connector = _connector(state)
        result = connector.fetch("9000", limit=200)
        assert len(result.records) == 1
        assert result.records[0].deleted is True
        assert result.next_cursor == "9100"

    def test_expired_history_id_404_resets_to_the_query_fallback(self):
        state = _ServiceState()
        state.history_error = _FakeHttpError(404, b"Requested entity was not found")
        state.list_result = {"messages": [{"id": "m1", "threadId": "t1"}]}
        state.messages["m1"] = _msg_fixture("m1")
        connector = _connector(state)
        result = connector.fetch("9000", limit=200)
        assert result.reset is True  # the cursor was invalidated
        assert [r.external_id for r in result.records] == ["m1"]
        assert result.next_cursor == "9002"  # a FRESH historyId

    def test_limit_caps_the_history_batch_at_the_entry_boundary(self):
        state = _ServiceState()
        state.profile_history_id = "9100"
        state.history_pages = {
            "first": {
                "history": [
                    {
                        "id": "9001",
                        "labelsAdded": [
                            {"labelIds": [LABEL], "message": {"id": "m1"}}
                        ],
                    },
                    {
                        "id": "9002",
                        "labelsAdded": [
                            {"labelIds": [LABEL], "message": {"id": "m2"}}
                        ],
                    },
                ],
                "historyId": "9003",
            }
        }
        state.messages["m1"] = _msg_fixture("m1")
        state.messages["m2"] = _msg_fixture("m2")
        connector = _connector(state)
        result = connector.fetch("9000", limit=1)
        assert [r.external_id for r in result.records] == ["m1"]
        assert result.next_cursor == "9001"  # resume AFTER the consumed entry


# --- error translation (through google.base, single mapping) -------------------


class TestErrorTranslation:
    def test_refresh_error_maps_to_auth_error(self):
        state = _ServiceState()
        state.list_error = type("RefreshError", (Exception,), {})("invalid_grant")
        connector = _connector(state)
        with pytest.raises(ConnectorAuthError):
            connector.fetch(None, limit=200)

    def test_http_503_maps_to_retryable(self):
        state = _ServiceState()
        state.list_error = _FakeHttpError(503)
        connector = _connector(state)
        with pytest.raises(ConnectorRetryableError):
            connector.fetch(None, limit=200)

    def test_http_429_maps_to_retryable(self):
        state = _ServiceState()
        state.list_error = _FakeHttpError(429)
        connector = _connector(state)
        with pytest.raises(ConnectorRetryableError):
            connector.fetch(None, limit=200)


# --- attachments ---------------------------------------------------------------


class TestRegistration:
    """The registry contract (found missing in review round 3): without
    @register + the alias, `brain connectors sync email` reports 'not
    available on this install' — the whole CLI path is dead, and every CLI
    test missed it because they monkeypatch _build_connector."""

    def test_connector_registers_as_email_with_the_gmail_alias(self):
        import brain.connectors as registry
        from brain.connectors.google.gmail import GmailConnector

        registry.ensure_registered()
        assert registry.get("email") is GmailConnector
        assert registry.get("gmail") is GmailConnector  # the alias resolves
        assert "email" in registry.all_names()


class TestAttachments:
    def test_fetch_attachment_returns_the_decoded_bytes(self):
        state = _ServiceState()
        _queue_message(state, "m1", with_attachment=True)
        blob = base64.urlsafe_b64encode(b"%PDF-fake").decode().rstrip("=")
        state.attachment_blobs["att1"] = blob
        connector = _connector(state)
        result = connector.fetch(None, limit=200)
        ref = result.records[0].attachments[0]
        assert connector.fetch_attachment(result.records[0], ref) == b"%PDF-fake"
        assert ("attachments.get", {"messageId": "m1", "id": "att1"}) in state.calls

    def test_build_declares_gmail_v1_to_the_service_factory(self):
        """Round-4 base contract: the adapter declares its API and the cached
        factory receives exactly (token, scopes, "gmail", "v1")."""
        from brain.ai.gmail import GMAIL_SCOPE
        from brain.config import Settings
        from brain.connectors.google.gmail import GmailConnector

        assert GmailConnector.API_NAME == "gmail"
        assert GmailConnector.API_VERSION == "v1"
        seen: list[tuple[str, list[str], str, str]] = []

        def factory(token: str, scopes: list[str], api_name: str, api_version: str) -> Any:
            seen.append((token, scopes, api_name, api_version))
            return _FakeService(_ServiceState())

        connector = GmailConnector(
            token_loader=lambda: "refresh-token",
            service_factory=factory,
            settings=Settings(
                gmail_min_call_interval_s=0.0,
                    gmail_label_id=LABEL, gmail_credentials_path="", _env_file=None
            ),
        )
        connector._service()
        assert seen == [("refresh-token", [GMAIL_SCOPE], "gmail", "v1")]

    def test_close_releases_the_cached_google_service(self):
        """Regression (found by mypy): the old close() called
        RestGmailClient.close(), which does not exist — a real poll cycle
        would crash in the finally. close() must release THE cached google
        service (duck-typed; one service per connector, no double-close)."""
        from brain.config import Settings
        from brain.connectors.google.gmail import GmailConnector

        state = _ServiceState()
        builds: list[_FakeService] = []

        def factory(token: str, scopes: list[str], api_name: str, api_version: str) -> Any:
            svc = _FakeService(state)
            builds.append(svc)
            return svc

        connector = GmailConnector(
            token_loader=lambda: "refresh-token",
            service_factory=factory,
            settings=Settings(
                gmail_min_call_interval_s=0.0,
                    gmail_label_id=LABEL, gmail_credentials_path="", _env_file=None
            ),
        )
        first_client = connector._gmail()
        connector.close()
        assert ("service.close", {}) in state.calls
        assert builds[0].closed is True
        # After close() NOTHING cached may still hold the closed service: the
        # next use must build a NEW client over a NEW service.
        second_client = connector._gmail()
        assert second_client is not first_client
        assert len(builds) == 2
        assert builds[1].closed is False
        connector.close()  # idempotent per generation: releases the new one
        assert state.calls.count(("service.close", {})) == 2

    def _capture_attachment_bytes(self, harness: Any, svc_env: Any) -> None:
        """Body shared by the DB-level capture test and the stray-row test:
        the REAL AttachmentService in the runner (the gmail-poll wiring), the
        REAL ingest (real memory_items parent), the runner capture+submit,
        then one drain — exactly one `brain gmail-poll` cycle."""
        from tests.test_attachments import FakeQuery, _make_service

        h = harness
        real = _make_service(svc_env, FakeQuery(reply="the attached note"))
        h.runner._attachments = real
        _queue_message(h._state_service, "m1", with_attachment=True)
        # Swap the fixture's pdf part for a png so the read goes through the
        # Claude reader (FakeQuery) — same shape the old F3 test used.
        part = h._state_service.messages["m1"]["payload"]["parts"][1]
        part["mimeType"] = "image/png"
        part["filename"] = "photo.png"
        blob = base64.urlsafe_b64encode(b"\x89PNG fake bytes").decode().rstrip("=")
        h._state_service.attachment_blobs["att1"] = blob
        summary = h.runner.run()
        assert summary.created == 1
        assert ("attachments.get", {"messageId": "m1", "id": "att1"}) in h._state_service.calls
        real.drain()  # the CLI cycle drains once per poll (_gmail_poll_cycle)

        from brain.db.models import Attachment, MemoryItem
        from brain.db.session import SessionLocal

        item = h.item_for("m1")
        assert item is not None
        with SessionLocal() as session:
            # Scoped to THIS test's email item (T1): a stray attachment row in
            # the shared *_test db from an interrupted session must not break it.
            row = session.query(Attachment).filter_by(parent_item_id=item.id).one()
            assert row.source == "email" and row.status == "done"
            assert "the attached note" in (row.text or "")
            assert row.filename == "photo.png" and row.mime == "image/png"
            parent = session.get(MemoryItem, row.parent_item_id)
            assert parent is not None, "the email item is the attachment's parent"
            assert parent.meta["gmail_id"] == "m1"

    def test_attachment_bytes_captured_with_source_email(self, svc_env, gmail_harness):
        """Old F3 (moved 1:1 from tests/services/test_attachments_email.py):
        attachment BYTES flow through the attachments core with the REAL
        service — captured under the email item, queued, drained, row done."""
        self._capture_attachment_bytes(gmail_harness, svc_env)

    def test_email_attachment_test_ignores_stray_rows(self, svc_env):
        """T1 (moved with the test): the DB assertions must be scoped to THIS
        test's own parent item. A stray attachment row left in the shared
        *_test database by an interrupted earlier session must not break the
        capture test — plant one (its own parent item, so FK holds) and run
        the same body verbatim."""
        from brain.db.models import Attachment, MemoryItem
        from brain.db.session import SessionLocal

        with SessionLocal() as session:
            stray_parent = MemoryItem(raw_text="stray from an earlier session", status="inbox")
            session.add(stray_parent)
            session.flush()
            session.add(
                Attachment(
                    item_id=stray_parent.id,
                    parent_item_id=stray_parent.id,
                    sha256="0" * 64,
                    mime="text/plain",
                    filename="stray.txt",
                    size_bytes=4,
                    source="email",
                    source_ref="stray-m1",
                    status="done",
                )
            )
            session.commit()

        harness = _Harness()  # no wipe: the stray row must survive
        try:
            self._capture_attachment_bytes(harness, svc_env)
        finally:
            harness.close()


# --- legacy ledger seed (approved dedupe decision) ----------------------------


class TestLegacySeed:
    @pytest.fixture(name="ledger_db")
    def ledger_db_fixture(self, database):
        """Migrated schema + the legacy email_messages table cleaned."""
        from sqlalchemy import text

        from brain.db.session import engine

        with engine.begin() as conn:
            for table in ("connector_records", "connector_state", "email_messages"):
                conn.execute(text(f"DELETE FROM {table}"))
            conn.execute(text("DELETE FROM memory_items"))
        yield
        with engine.begin() as conn:
            for table in ("connector_records", "connector_state", "email_messages"):
                conn.execute(text(f"DELETE FROM {table}"))
            conn.execute(text("DELETE FROM memory_items"))

    def _seed_legacy_rows(self, rows: list[tuple[str, str | None]]) -> None:
        """Insert old-poller seen rows: (gmail_id, item_id or None)."""

        from brain.db.models import EmailMessage, MemoryItem
        from brain.db.session import SessionLocal

        with SessionLocal() as session:
            for gmail_id, item_id in rows:
                real_item_id = None
                if item_id is None:
                    item = MemoryItem(raw_text=f"old item for {gmail_id}", source="email")
                    session.add(item)
                    session.flush()
                    real_item_id = item.id
                session.add(
                    EmailMessage(
                        dedupe_id=f"<{gmail_id}@x>",
                        gmail_id=gmail_id,
                        rfc_message_id=f"<{gmail_id}@x>",
                        item_id=real_item_id,
                    )
                )
            session.commit()

    def test_seed_on_first_fetch_maps_the_old_ledger(self, ledger_db):
        from sqlalchemy import text

        from brain.connectors.google.gmail import seed_connector_records_from_email_ledger
        from brain.db.session import SessionLocal

        self._seed_legacy_rows([("old1", None), ("old2", None)])
        with SessionLocal() as session:
            seeded = seed_connector_records_from_email_ledger(session)
        assert seeded == 2
        with SessionLocal() as session:
            rows = session.execute(
                text("SELECT source, external_id FROM connector_records ORDER BY external_id")
            ).all()
        assert rows == [("email", "old1"), ("email", "old2")]

    def test_second_seed_inserts_zero(self, ledger_db):
        from brain.connectors.google.gmail import seed_connector_records_from_email_ledger
        from brain.db.session import SessionLocal

        self._seed_legacy_rows([("old1", None)])
        with SessionLocal() as session:
            assert seed_connector_records_from_email_ledger(session) == 1
        with SessionLocal() as session:
            assert seed_connector_records_from_email_ledger(session) == 0  # idempotent

    def _connector_with_seed(self, state: _ServiceState, label_rows: list[tuple[str, None]]):
        """Connector + the seed closure the CLI wires (session-backed)."""
        from brain.connectors.google.gmail import seed_connector_records_from_email_ledger
        from brain.db.session import SessionLocal

        self._seed_legacy_rows(label_rows)
        seeds: list[int] = []

        def seed() -> int:
            with SessionLocal() as session:
                n = seed_connector_records_from_email_ledger(session)
            seeds.append(n)
            return n

        return _connector(state, legacy_seed=seed), seeds

    def test_seed_runs_again_on_reset(self, ledger_db):
        """cursor=None (first run) AND the reset fallback both seed; a second
        seed is a no-op (the manager's condition 1)."""
        state = _ServiceState()
        state.history_error = _FakeHttpError(404, b"Requested entity was not found")
        state.list_result = {"messages": []}
        connector, seeds = self._connector_with_seed(state, [("old1", None)])
        result = connector.fetch("9000", limit=200)  # reset path: 404 -> fallback
        assert result.reset is True
        assert seeds == [1]  # the reset seed inserted the legacy row

    def test_first_fetch_seeds_and_reset_reseeds_zero(self, ledger_db):
        state = _ServiceState()
        state.list_result = {"messages": []}
        connector, seeds = self._connector_with_seed(state, [("old1", None)])
        connector.fetch(None, limit=200)  # first run: cursor None -> seed
        assert seeds == [1]
        state.history_error = _FakeHttpError(404, b"Requested entity was not found")
        connector.fetch("9000", limit=200)  # reset fallback: re-seed -> 0
        assert seeds == [1, 0]


# --- runner-level semantics (real IngestService on the test DB) ---------------


class _Harness:
    """GmailConnector (fake service) + the REAL runner stack."""

    def __init__(self) -> None:
        from tests.fakes import StubClassifier
        from tests.test_connectors_runner import Harness as BaseHarness

        from brain.ai.classify import FALLBACK_CLASSIFICATION
        from brain.connectors.google.gmail import seed_connector_records_from_email_ledger
        from brain.connectors.runner import ConnectorRunner
        from brain.connectors.state import ConnectorRecordRepo, ConnectorStateRepo
        from brain.db.session import SessionLocal
        from brain.repositories.items import ItemsRepository
        from brain.services.ingest import IngestService

        self.base = BaseHarness.__new__(BaseHarness)  # reuse its inner fakes
        self.session = SessionLocal()
        self.repo = ItemsRepository(self.session)
        self.classifier = StubClassifier(dict(FALLBACK_CLASSIFICATION))
        from qdrant_client import QdrantClient

        from brain.ai.embedder import FakeEmbedder
        from brain.ai.vectorstore import QdrantVectorStore
        from brain.services.indexing import QdrantItemIndexer

        indexer = QdrantItemIndexer(
            FakeEmbedder(dim=8), QdrantVectorStore(client=QdrantClient(":memory:"))
        )
        self.ingest = IngestService(self.repo, self.classifier, indexer)
        self.state = ConnectorStateRepo(self.session)
        self.records = ConnectorRecordRepo(self.session)
        self._state_service = _ServiceState()



        def seed() -> int:
            return seed_connector_records_from_email_ledger(self.session)

        self.connector = _connector(
            self._state_service,
            legacy_seed=seed,
            is_known=lambda gid: self.records.get("email", gid) is not None,
        )
        self.runner = ConnectorRunner(
            self.connector,
            ingest=self.ingest,
            repo=self.repo,
            state=self.state,
            records=self.records,
        )

    def close(self) -> None:
        self.session.rollback()
        self.session.close()

    def item_for(self, gmail_id: str):
        row = self.records.get("email", gmail_id)
        return None if row is None else self.repo.get(row.item_id)


@pytest.fixture(name="gmail_harness")
def gmail_harness_fixture(database):
    from sqlalchemy import text

    from brain.db.session import engine

    with engine.begin() as conn:
        for table in ("connector_records", "connector_state", "email_messages"):
            conn.execute(text(f"DELETE FROM {table}"))
        conn.execute(text("DELETE FROM memory_items"))
    h = _Harness()
    yield h
    h.close()
    with engine.begin() as conn:
        for table in ("connector_records", "connector_state", "email_messages"):
            conn.execute(text(f"DELETE FROM {table}"))
        conn.execute(text("DELETE FROM memory_items"))


def _queue_message(
    state: _ServiceState,
    mid: str = "m1",
    rfc: str = "<a1b2c3@mail.gmail.com>",
    *,
    with_attachment: bool = False,
):
    state.list_result = {"messages": [{"id": mid, "threadId": "t1"}]}
    state.messages[mid] = _msg_fixture(mid, rfc, with_attachment=with_attachment)


class TestRunnerSemantics:
    """new / unchanged / changed / deleted for the email source, through the
    real runner — the connector only produces records."""

    def test_new_message_captures_through_the_paste_path(self, gmail_harness):
        h = gmail_harness
        _queue_message(h._state_service, "m1")
        summary = h.runner.run()
        assert summary.created == 1
        item = h.item_for("m1")
        assert item is not None
        assert item.source == "email"  # source stays 'email' for existing data
        assert "The trip is tomorrow" in item.raw_text
        assert item.meta["gmail_id"] == "m1"
        assert item.meta["rfc_message_id"] == "<a1b2c3@mail.gmail.com>"
        assert item.meta["connector"] == "email"
        assert item.meta["external_id"] == "m1"
        assert item.occurred_at == datetime(2026, 10, 22, 10, 0, tzinfo=UTC)

    def test_seeded_ledger_message_is_not_reingested(self, gmail_harness):
        """The manager's proof: old-poller rows in the seen table + seed ->
        the first connector run ingests NOTHING again — and never even GETs
        the known id (the old fast path, now via the ledger)."""
        from brain.db.models import EmailMessage, MemoryItem
        from brain.db.session import SessionLocal

        h = gmail_harness
        with SessionLocal() as s:
            item = MemoryItem(
                raw_text="old captured email", source="email", status="inbox"
            )
            s.add(item)
            s.flush()
            s.add(
                EmailMessage(
                    dedupe_id="<old@x>",
                    gmail_id="old1",
                    rfc_message_id="<old@x>",
                    item_id=item.id,
                )
            )
            s.commit()
        _queue_message(h._state_service, "old1")
        summary = h.runner.run()
        assert summary.created == 0  # seeded -> NOT re-ingested
        assert summary.fetched == 0  # the connector yielded nothing for it
        assert not any(
            c[0] == "messages.get" and c[1]["id"] == "old1" for c in h._state_service.calls
        ), "known ids must skip the GET entirely"

    def test_reset_resync_of_known_messages_is_idempotent(self, gmail_harness):
        """A full resync (expired history) re-lists everything the poll still
        sees; known ids are skipped — no duplicates, no updates."""
        h = gmail_harness
        _queue_message(h._state_service, "m1")
        first = h.runner.run()
        assert first.created == 1
        # Second poll: the history window is empty (nothing changed).
        second = h.runner.run()
        assert (second.created, second.updated, second.deleted) == (0, 0, 0)
        # Third poll: history expired -> reset -> the query lists m1 again.
        h._state_service.history_error = _FakeHttpError(404)
        third = h.runner.run()
        assert third.reset is True
        assert (third.created, third.updated, third.deleted) == (0, 0, 0)

    def test_message_id_redelivered_under_a_new_gmail_id_dedupes(self, gmail_harness):
        """Manager condition 2: the same Message-ID re-delivered under a NEW
        gmail id still dedupes at ingest by the content fingerprint — one
        item, and the new id lands in the ledger pointing at it. The
        re-delivery arrives through the REAL incremental path (a scripted
        messagesAdded history entry): an unscripted history window returns
        empty and the run would pass VACUOUSLY (found in the second-pass
        review work)."""
        h = gmail_harness
        _queue_message(h._state_service, "m1", rfc="<same@msg>")
        first = h.runner.run()
        assert first.created == 1
        _queue_message(h._state_service, "m2", rfc="<same@msg-id-different-id>")
        # m2: identical body text + occurred_at + source -> same fingerprint.
        h._state_service.messages["m2"]["payload"]["headers"] = [
            hh if hh["name"] != "Message-ID" else {"name": "Message-ID", "value": "<same@msg>"}
            for hh in h._state_service.messages["m2"]["payload"]["headers"]
        ]
        h._state_service.history_pages = {
            "first": _fixture("history_message_added_page1.json")
        }
        second = h.runner.run()
        assert second.reset is False
        # The fingerprint dedupe fires INSIDE ingest.create: the DB keeps ONE
        # item and the ledger points the new id at it. (The summary's created
        # counter counts create() CALLS — a dedupe hit returns the existing
        # item, so it reads 1 here; the DB truth below is what matters.)
        items = h.repo.list(item_type=None, project=None, status=None, limit=50, offset=0)
        assert len([i for i in items if i.source == "email"]) == 1
        ledger_m2 = h.records.get("email", "m2")
        assert ledger_m2 is not None
        assert str(ledger_m2.item_id) == str(h.item_for("m1").id)

    def test_same_rfc_new_gmail_id_different_minute_needs_the_backstop(
        self, gmail_harness
    ):
        """Second-pass review finding 2: the ingest fingerprint hashes the
        occurred_at MINUTE, so the same Message-ID re-delivered under a NEW
        gmail id with a slightly different internal_date escapes it. The
        Message-ID backstop (rfc_backstop_put) must catch the re-delivery
        BEFORE create: ledger put pointing at the existing item, zero new
        items, and no spurious reset."""
        from brain.connectors.google.gmail import rfc_backstop_put

        h = gmail_harness
        h.connector._rfc_backstop = lambda rfc, gid: rfc_backstop_put(
            h.session, rfc, gid
        )  # the production wiring (cli._build_connector)
        _queue_message(h._state_service, "m1", rfc="<same@msg>")
        first = h.runner.run()
        assert first.created == 1
        # Re-delivery: NEW gmail id, same Message-ID, internal_date 5 minutes
        # later, and a DATE-FREE body so occurred_at tracks internal_date
        # (the fixture body's own date phrase would pin occurred_at and mask
        # the scenario).
        _queue_message(h._state_service, "m2", rfc="<same@msg>")
        raw = h._state_service.messages["m2"]
        raw["internalDate"] = str(int(raw["internalDate"]) + 5 * 60 * 1000)
        raw["payload"]["parts"][0]["body"]["data"] = _b64(
            "Please label your child's jacket with their name."
        )
        h._state_service.history_pages = {
            "first": _fixture("history_message_added_page1.json")
        }
        second = h.runner.run()
        assert second.created == 0, "the Message-ID backstop must catch the re-delivery"
        assert second.reset is False  # a clean incremental delivery, no fallback
        items = h.repo.list(item_type=None, project=None, status=None, limit=50, offset=0)
        assert len([i for i in items if i.source == "email"]) == 1
        ledger_m2 = h.records.get("email", "m2")
        assert ledger_m2 is not None and ledger_m2.item_id is not None
        assert str(ledger_m2.item_id) == str(h.item_for("m1").id)

    def test_backstop_never_swallows_a_known_gmail_id(self, gmail_harness):
        """Review round 3, B2: the backstop matches its OWN item when the
        SAME gmail id returns — a label change re-delivers the identical
        message through the history path, and the pre-fix backstop reset the
        ledger row to version=None and DROPPED the record, staling the
        version and losing a legitimate update. The backstop must apply
        only when the gmail id has NO ledger row yet; a known id belongs to
        the runner (version update)."""
        from brain.connectors.google.gmail import rfc_backstop_put

        h = gmail_harness
        h.connector._rfc_backstop = lambda rfc, gid: rfc_backstop_put(
            h.session, rfc, gid
        )  # the production wiring (cli._build_connector)
        _queue_message(h._state_service, "m1", rfc="<same@msg>")
        first = h.runner.run()
        assert first.created == 1
        row = h.records.get("email", "m1")
        assert row is not None and row.version is None
        h.records.put("email", "m1", "v1", row.item_id)  # an already-ledgered VERSION
        # The SAME gmail id returns through the history path (label change).
        h._state_service.history_pages = {
            "first": {
                "history": [
                    {
                        "id": "9001",
                        "messagesAdded": [
                            {
                                "message": {
                                    "id": "m1",
                                    "labelIds": [LABEL],
                                    "historyId": "9001",
                                }
                            }
                        ],
                    }
                ]
            }
        }
        second = h.runner.run()
        assert second.fetched == 1, "the backstop must NOT swallow a known id"
        assert second.updated == 1
        assert h.records.get("email", "m1").version is None  # reconciled to the source
        items = h.repo.list(item_type=None, project=None, status=None, limit=50, offset=0)
        assert len([i for i in items if i.source == "email"]) == 1

    def test_same_body_without_rfc_id_dedupes_by_fingerprint(self, gmail_harness):
        h = gmail_harness
        _queue_message(h._state_service, "m1", rfc="")
        h.runner.run()
        _queue_message(h._state_service, "m2", rfc="")
        summary = h.runner.run()
        assert summary.created == 0  # same sender/subject/minute -> same fp

    def test_unchanged_message_on_reset_is_skipped_by_version(self, gmail_harness):
        """Unchanged: a known id re-yielded with the same (NULL) version is
        skipped by the runner — re-running a sync never duplicates."""
        h = gmail_harness
        _queue_message(h._state_service, "m1")
        h.runner.run()
        # Force a reset WITHOUT expiry scripting: yield m1 again via history.
        h._state_service.list_result = {"messages": [{"id": "m1", "threadId": "t1"}]}
        h._state_service.history_error = _FakeHttpError(404)
        summary = h.runner.run()
        assert summary.reset is True
        assert (summary.created, summary.updated, summary.deleted) == (0, 0, 0)

    def test_label_removed_marks_the_item_deleted_never_hard_delete(self, gmail_harness):
        """Deleted: the message left the polled label -> the item stays in the
        DB, meta gains source_deleted=true, retrieval hides it (superseded)."""

        from sqlalchemy import text as sql_text  # noqa: PLC0415

        from brain.db.session import engine  # noqa: PLC0415

        h = gmail_harness
        _queue_message(h._state_service, "m1")
        assert h.runner.run().created == 1
        # Incremental: history says m1's label was removed (tombstone text
        # comes from the real GET — the message still exists in Gmail).
        h._state_service.list_result = {"messages": []}
        h._state_service.history_pages = {
            "first": _fixture("history_removed_page2.json")  # removes m2...
        }
        h._state_service.history_pages["first"]["history"] = [
            {
                "id": "9005",
                "labelsRemoved": [{"labelIds": [LABEL], "message": {"id": "m1"}}],
            }
        ]
        h._state_service.history_pages["first"]["historyId"] = "9006"
        summary = h.runner.run()
        assert summary.deleted == 1
        item = h.item_for("m1")
        assert item is not None  # NEVER hard-deleted
        assert item.meta.get("source_deleted") is True
        assert item.superseded_by == item.id
        assert "The trip is tomorrow" in item.raw_text  # text preserved
        with engine.begin() as conn:
            n = conn.execute(sql_text("SELECT count(*) FROM memory_items")).scalar()
        assert n == 1

    def test_label_readded_restores_the_item(self, gmail_harness):
        """Un-delete: the label comes back -> the hidden item is restored."""
        h = gmail_harness
        _queue_message(h._state_service, "m1")
        h.runner.run()
        h._state_service.list_result = {"messages": []}
        h._state_service.history_pages = {
            "first": {
                "history": [
                    {
                        "id": "9005",
                        "labelsRemoved": [{"labelIds": [LABEL], "message": {"id": "m1"}}],
                    }
                ],
                "historyId": "9006",
            }
        }
        assert h.runner.run().deleted == 1
        # Re-added: history reports labelsAdded; the GET still sees the body.
        h._state_service.history_pages = {
            "first": {
                "history": [
                    {
                        "id": "9007",
                        "labelsAdded": [{"labelIds": [LABEL], "message": {"id": "m1"}}],
                    }
                ],
                "historyId": "9008",
            }
        }
        summary = h.runner.run()
        assert summary.updated == 1
        item = h.item_for("m1")
        assert item is not None
        assert item.meta.get("source_deleted") is None
        assert item.superseded_by is None

    def test_message_that_lost_the_label_is_not_captured_fresh(self, gmail_harness):
        """Old TestLabelMembershipDefence: a listed message that no longer
        carries the polled label is never captured (defence in depth on GET)."""
        h = gmail_harness
        msg = _msg_fixture("m9", "<lost@x>")
        msg["labelIds"] = ["Label_OTHER"]
        h._state_service.list_result = {"messages": [{"id": "m9", "threadId": "t"}]}
        h._state_service.messages["m9"] = msg
        summary = h.runner.run()
        assert summary.created == 0
        assert h.item_for("m9") is None  # not even a tombstone for an unknown id

    def test_attachments_flow_through_the_pipeline(self, gmail_harness):
        """Old F3: attachment bytes go through the attachments core
        (source='email', parent = the email item) — via the runner."""
        import base64  # noqa: PLC0415

        from brain.connectors.testing import RecordingAttachments  # noqa: PLC0415

        h = gmail_harness
        h.runner._attachments = RecordingAttachments()
        _queue_message(h._state_service, "m1", with_attachment=True)
        blob = base64.urlsafe_b64encode(b"%PDF-fake").decode().rstrip("=")
        h._state_service.attachment_blobs["att1"] = blob
        summary = h.runner.run()
        assert summary.created == 1
        att = h.runner._attachments
        assert len(att.captures) == 1
        call = att.captures[0]
        assert call["source"] == "email"
        assert call["source_ref"] == "m1"
        assert call["content"] == b"%PDF-fake"
        assert call["filename"] == "trip-form.pdf"
        assert att.submitted  # queued for the budgeted read
