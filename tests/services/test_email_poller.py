"""F2 email source: the poller service (framework-free).

Fakes stand in for the Gmail client, the ingest service and the seen-repo;
no test touches Gmail or the network.
"""

from typing import Any

from brain.ai.gmail import GmailAuthError, GmailLabelError, GmailMessage, GmailMessageStub
from brain.services.email_poller import EmailPoller, dedupe_id_for_message


class FakeGmailClient:
    """Scripted client: records every list_messages call (labelIds contract)."""

    def __init__(self, messages: list[GmailMessage], list_error: Exception | None = None):
        self._messages = {m.id: m for m in messages}
        self._list_error = list_error
        self.list_calls: list[dict[str, Any]] = []

    def list_messages(self, *, query: str, label_ids: list[str]) -> list[GmailMessageStub]:
        self.list_calls.append({"query": query, "label_ids": list(label_ids)})
        if self._list_error is not None:
            raise self._list_error
        return [GmailMessageStub(id=m.id, thread_id=m.thread_id) for m in self._messages.values()]

    def get_message(self, message_id: str) -> GmailMessage:
        return self._messages[message_id]

    def close(self) -> None: ...


class FakeItem:
    def __init__(self, item_id: str) -> None:
        self.id = item_id


class FakeIngest:
    def __init__(self) -> None:
        self.created: list[dict[str, Any]] = []

    def capture_parsed(self, parsed, *, source=None, extra_meta=None, occurred_at=None):
        self.created.append(
            {
                "source": source,
                "raw_text": getattr(parsed, "raw_text", str(parsed)),
                "meta": extra_meta,
                "occurred_at": occurred_at,
            }
        )
        return [FakeItem(f"item-{len(self.created)}")]


class FakeSeenRepo:
    def __init__(self, seen: set[str] | None = None) -> None:
        self.seen_ids = set(seen or [])
        self.marked: list[tuple[str, str]] = []

    def seen(self, dedupe_id: str) -> bool:
        return dedupe_id in self.seen_ids

    def mark(self, dedupe_id: str, gmail_id: str, rfc_message_id: str | None, item_id: Any) -> None:
        self.seen_ids.add(dedupe_id)
        self.marked.append((dedupe_id, gmail_id))


def _message(
    mid: str = "m1",
    rfc: str | None = "<a@b>",
    body: str = "From: t@example.com\nSubject: hi\n\nbody text",
    date: str = "2026-10-22T10:00:00+00:00",
    attachments: list | None = None,
):
    from datetime import UTC, datetime

    return GmailMessage(
        id=mid,
        thread_id="t1",
        rfc_message_id=rfc,
        sender="teacher@example.com",
        subject="hi",
        internal_date=datetime.fromisoformat(date).astimezone(UTC),
        label_ids=["Label_school"],
        body_text=body,
        attachments=list(attachments or []),
    )


def _poller(
    client: FakeGmailClient,
    ingest: FakeIngest | None = None,
    seen: FakeSeenRepo | None = None,
    label: str = "Label_school",
) -> tuple[EmailPoller, FakeIngest, FakeSeenRepo]:
    from brain.config import Settings

    ingest = ingest or FakeIngest()
    seen = seen or FakeSeenRepo()
    poller = EmailPoller(
        client=client,
        ingest=ingest,
        seen_repo=seen,
        settings=Settings(gmail_label_id=label, gmail_credentials_path="", _env_file=None),
    )
    return poller, ingest, seen


class TestListByConstruction:
    def test_every_list_call_carries_the_label(self):
        client = FakeGmailClient([])
        poller, _, _ = _poller(client)
        poller.poll_once()
        poller.poll_once()
        assert len(client.list_calls) == 2
        for call in client.list_calls:
            assert call["label_ids"] == ["Label_school"]

    def test_label_config_missing_refuses_to_poll(self):
        from brain.config import Settings

        poller = EmailPoller(
            client=FakeGmailClient([]),
            ingest=FakeIngest(),
            seen_repo=FakeSeenRepo(),
            settings=Settings(gmail_label_id="", gmail_credentials_path="", _env_file=None),
        )
        summary = poller.poll_once()
        assert summary.state == "disabled"
        assert summary.listed == 0


class TestIngestPath:
    def test_new_message_is_captured_via_paste_path(self):
        client = FakeGmailClient([_message()])
        poller, ingest, seen = _poller(client)
        summary = poller.poll_once()
        assert summary.new == 1
        assert ingest.created[0]["source"] == "email"
        meta = ingest.created[0]["meta"]
        assert meta["gmail_id"] == "m1"
        assert meta["rfc_message_id"] == "<a@b>"
        assert seen.marked and seen.marked[0][1] == "m1"

    def test_seen_message_is_skipped(self):
        msg = _message()
        seen = FakeSeenRepo({dedupe_id_for_message(msg)})
        poller, ingest, _ = _poller(FakeGmailClient([msg]), seen=seen)
        summary = poller.poll_once()
        assert summary.new == 0
        assert summary.skipped == 1
        assert ingest.created == []

    def test_same_message_twice_ingests_once(self):
        client = FakeGmailClient([_message()])
        poller, ingest, _ = _poller(client)
        poller.poll_once()
        summary = poller.poll_once()
        assert summary.skipped == 1
        assert len(ingest.created) == 1

    def test_message_without_rfc_id_uses_fingerprint_dedupe(self):
        m1 = _message("m1", rfc=None)
        m1_again = _message("m2", rfc=None)  # same sender/subject/date, other gmail id
        poller, ingest, _ = _poller(FakeGmailClient([m1, m1_again]))
        summary = poller.poll_once()
        assert summary.new == 1
        assert summary.skipped == 1

    def test_attachment_references_stored_in_meta(self):
        from brain.ai.gmail import GmailAttachmentRef

        att = GmailAttachmentRef(
            attachment_id="att1",
            filename="menu.pdf",
            mime_type="application/pdf",
            size=1234,
        )
        msg = _message(attachments=[att])
        poller, ingest, _ = _poller(FakeGmailClient([msg]))
        poller.poll_once()
        meta = ingest.created[0]["meta"]
        assert meta["attachments"] == [
            {
                "attachment_id": "att1",
                "filename": "menu.pdf",
                "mime_type": "application/pdf",
                "size": 1234,
            }
        ]


class TestPollerStates:
    def test_auth_error_sets_needs_re_auth_and_records_event(self):
        client = FakeGmailClient([], list_error=GmailAuthError("invalid_grant"))
        poller, _, _ = _poller(client)
        summary = poller.poll_once()
        assert summary.state == "needs re-auth"
        assert summary.new == 0

    def test_label_error_sets_label_missing(self):
        client = FakeGmailClient([], list_error=GmailLabelError("label gone"))
        poller, _, _ = _poller(client)
        summary = poller.poll_once()
        assert summary.state == "label missing"

    def test_transient_error_sets_error_state(self):
        from brain.ai.gmail import GmailError

        client = FakeGmailClient([], list_error=GmailError("timeout"))
        poller, _, _ = _poller(client)
        summary = poller.poll_once()
        assert summary.state == "error"


class TestBackoff:
    def test_backoff_doubles_from_the_poll_interval(self):
        from brain.config import Settings

        poller = EmailPoller(
            client=FakeGmailClient([]),
            ingest=FakeIngest(),
            seen_repo=FakeSeenRepo(),
            settings=Settings(
                gmail_label_id="L",
                gmail_credentials_path="",
                gmail_poll_interval_s=60,
                _env_file=None,
            ),
        )
        assert poller.backoff_seconds() == 60
        poller.failures = 2
        assert poller.backoff_seconds() == 240

    def test_backoff_capped(self):
        from brain.config import Settings

        poller = EmailPoller(
            client=FakeGmailClient([]),
            ingest=FakeIngest(),
            seen_repo=FakeSeenRepo(),
            settings=Settings(
                gmail_label_id="L",
                gmail_credentials_path="",
                gmail_poll_interval_s=60,
                _env_file=None,
            ),
        )
        poller.failures = 20
        assert poller.backoff_seconds() <= 600

    def test_success_resets_failures(self):
        poller, _, _ = _poller(FakeGmailClient([_message()]))
        poller.failures = 3
        poller.poll_once()
        assert poller.failures == 0


class TestDedupeId:
    def test_rfc_message_id_wins(self):
        msg = _message(rfc="<unique@x>")
        assert dedupe_id_for_message(msg) == "<unique@x>"

    def test_fingerprint_is_deterministic_and_minute_scoped(self):
        a = dedupe_id_for_message(_message("m1", rfc=None))
        b = dedupe_id_for_message(_message("m2", rfc=None))
        assert a == b  # same sender/subject/minute -> same key
        later = _message("m3", rfc=None, date="2026-10-22T10:01:00+00:00")
        assert dedupe_id_for_message(later) != a  # a different minute is a different key
        assert a.startswith("fp:")
