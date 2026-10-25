"""email's conformance donation (the sweep's third customer).

``build(api)`` -> a FRESH :class:`GmailConnector` whose Gmail service is the
kit's :class:`~brain.connectors.conformance.FakeSourceAPI` wearing Gmail
clothes. The fake account is REAL-Gmail-shaped:

- ``users().messages().list`` serves the whole kit dataset as stubs in the
  kit's (scrambled) order; the ADAPTER truncates client-side at ``limit``
  (email's real termination shape: query listings have no server cursor).
  ``users().messages().get`` renders the kit record as a full message
  resource (base64url text/plain part, Message-ID/Subject/From headers,
  ``internalDate``).
- ``users().getProfile`` reconciles the kit dataset into the account's
  APPEND-ONLY history log (one entry per unseen external_id+version) and
  returns the CURRENT historyId — the query path's resume cursor.
- ``users().history().list`` serves entries strictly after the requested
  historyId; a kit ``next_fetch_only`` (the RESET check) surfaces as the
  Google-shaped 404 — the adapter's expired-historyId path (full resync,
  ``reset=True``).

The connector is built through its public constructor with the CLI's
production seams (ledger-backed ``is_known`` fast path, Message-ID
backstop, legacy seed) over the ambient DATABASE_URL — the same database
the kit's runner writes, so the fast path sees committed ledger rows.
"""

from __future__ import annotations

import base64
from typing import Any

from brain.connectors.google.gmail import GmailConnector

# Gmail messages are IMMUTABLE: a record never changes under the same id —
# upstream changes are new ids + re-deliveries. Declared for the kit: the
# CHANGE check runs the domain-true variant (new record ingested once; the
# same id re-delivered skipped) instead of a version bump.
IMMUTABLE_SOURCE = True

_LABEL = "KIT_LABEL"


class _FakeHttpError(Exception):
    """Duck-typed googleapiclient.errors.HttpError: carries .resp.status."""

    def __init__(self, status: int, message: str = "") -> None:
        super().__init__(message or f"http {status}")
        self.resp = type("Resp", (), {"status": status})()


def _b64(text: str) -> str:
    return base64.urlsafe_b64encode(text.encode()).decode().rstrip("=")


class _Mailbox:
    """The fake account's append-only history log (one per kit check)."""

    def __init__(self) -> None:
        self._entries: list[dict[str, Any]] = []
        self._seen: dict[str, str] = {}  # external_id -> last logged version
        self._seen_count: dict[str, int] = {}  # external_id -> logged copies

    def reconcile(self, records: list[Any]) -> None:
        """Log one entry per upstream change we have not logged yet.

        A change is a version we have not logged OR a record the source now
        carries one more copy of (a RE-DELIVERY — e.g. a label re-add: the
        same id again). Gmail messages never change under their id.
        """
        counts: dict[str, int] = {}
        by_id: dict[str, Any] = {}
        for record in records:
            counts[record.external_id] = counts.get(record.external_id, 0) + 1
            by_id.setdefault(record.external_id, record)
        for rid, record in by_id.items():
            unchanged = (
                rid in self._seen
                and self._seen[rid] == record.version
                and counts[rid] <= self._seen_count.get(rid, 0)
            )
            if unchanged:
                continue
            kind = "labelsRemoved" if record.deleted else "labelsAdded"
            self._entries.append(
                {
                    "id": f"h{len(self._entries) + 1}",
                    kind: [{"labelIds": [_LABEL], "message": {"id": rid}}],
                }
            )
            self._seen[rid] = record.version
        self._seen_count = counts

    def history_id(self) -> str:
        """The CURRENT historyId: one above every logged entry."""
        return f"h{len(self._entries)}"

    def after(self, cursor: str) -> list[dict[str, Any]]:
        start = int(cursor.removeprefix("h"))
        return [entry for entry in self._entries if int(entry["id"].removeprefix("h")) > start]


def _mailbox(api: Any) -> _Mailbox:
    """The kit check's mailbox (lives on the shared api, fresh per check)."""
    box = getattr(api, "_w7_mailbox", None)
    if box is None:
        box = _Mailbox()
        api._w7_mailbox = box
    return box


def _message_payload(record: Any) -> dict[str, Any]:
    """Kit record -> a users.messages.get response (Google's shape)."""
    occurred_ms = "0"
    if getattr(record, "occurred_at", None) is not None:
        occurred_ms = str(int(record.occurred_at.timestamp() * 1000))
    return {
        "id": record.external_id,
        "threadId": record.external_id,
        "internalDate": occurred_ms,
        "labelIds": [] if record.deleted else [_LABEL],
        "payload": {
            "headers": [
                {
                    "name": "Message-ID",
                    "value": f"<{record.external_id}@kit.conformance>",
                },
                {"name": "Subject", "value": record.title},
                {"name": "From", "value": "kit@conformance.test"},
            ],
            "parts": [
                {"mimeType": "text/plain", "body": {"data": _b64(record.text)}}
            ],
        },
    }


class _FakeRequest:
    def __init__(self, result: Any = None, error: Exception | None = None) -> None:
        self._result = result
        self._error = error

    def execute(self) -> Any:
        if self._error is not None:
            raise self._error
        return self._result


class _KitGmailService:
    """users()-shaped fake backed by the kit's FakeSourceAPI."""

    def __init__(self, api: Any) -> None:
        self._api = api

    def users(self) -> _KitUsers:
        return _KitUsers(self._api)


class _KitUsers:
    def __init__(self, api: Any) -> None:
        self._api = api

    def messages(self) -> _KitMessages:
        return _KitMessages(self._api)

    def history(self) -> _KitHistory:
        return _KitHistory(self._api)

    def getProfile(self, userId: str) -> _FakeRequest:  # noqa: N802, ARG002
        # The mailbox's CURRENT historyId — the top of the log as it stands.
        # NO reconcile here: entries logged after earlier syncs consumed
        # their window must stay ahead of the cursor this returns, or the
        # fresh-cursor resync would skip them (a real mailbox only appends).
        return _FakeRequest(result={"historyId": _mailbox(self._api).history_id()})


class _KitMessages:
    def __init__(self, api: Any) -> None:
        self._api = api

    def list(self, **_params: Any) -> _FakeRequest:
        # The whole dataset in ONE page (kit order); the adapter truncates.
        return _FakeRequest(
            result={
                "messages": [
                    {"id": r.external_id, "threadId": r.external_id}
                    for r in self._api.records
                ]
            }
        )

    def get(self, userId: str, id: str, format: str = "full") -> _FakeRequest:  # noqa: A002, ARG002
        for record in self._api.records:
            if record.external_id == id:
                return _FakeRequest(result=_message_payload(record))
        return _FakeRequest(error=_FakeHttpError(404, "Requested entity was not found"))


class _KitHistory:
    def __init__(self, api: Any) -> None:
        self._api = api

    def list(self, **_params: Any) -> _FakeRequest:
        # The kit armed a reset for the next fetch: the historyId is expired
        # (Gmail's real cursor-invalidation) — 404 sends the adapter into its
        # full-resync query path, which then consumes the armed fetch. The
        # error rides the REQUEST (raising inside list() would escape the
        # client's _execute translation — arguments evaluate outside it).
        if getattr(self._api, "_next_only", None) is not None:
            return _FakeRequest(error=_FakeHttpError(404, "historyId expired"))
        box = _mailbox(self._api)
        box.reconcile(self._api.records)
        return _FakeRequest(result={"history": box.after(_params["startHistoryId"])})


def _ledger_session() -> Any:
    """A short-lived session over the ambient DATABASE_URL (the kit's DB)."""
    from brain.db.session import SessionLocal  # noqa: PLC0415

    return SessionLocal()


def build(api: Any) -> GmailConnector:
    """The registered email connector on a fake Gmail account backed by ``api``.

    The CLI's production seams (cli.py _build_connector): ledger-backed
    is_known, the Message-ID backstop, and the legacy seed — all over their
    own short-lived sessions (the runner commits through the kit's session;
    committed rows are visible here).
    """
    from brain.config import Settings  # noqa: PLC0415
    from brain.connectors.google.gmail import (  # noqa: PLC0415
        rfc_backstop_put,
        seed_connector_records_from_email_ledger,
    )
    from brain.connectors.state import ConnectorRecordRepo  # noqa: PLC0415

    def is_known(gmail_id: str) -> bool:
        session = _ledger_session()
        try:
            return ConnectorRecordRepo(session).get("email", gmail_id) is not None
        finally:
            session.close()

    def legacy_seed() -> int:
        session = _ledger_session()
        try:
            return seed_connector_records_from_email_ledger(session)
        finally:
            session.close()

    def backstop(rfc_message_id: str, gmail_id: str) -> bool:
        session = _ledger_session()
        try:
            return rfc_backstop_put(session, rfc_message_id, gmail_id)
        finally:
            session.close()

    return GmailConnector(
        token_loader=lambda: "refresh-token",
        service_factory=lambda token, scopes, api_name, api_version: _KitGmailService(api),
        settings=Settings(
            gmail_min_call_interval_s=0.0,
            gmail_label_id=_LABEL, gmail_query="", gmail_credentials_path="", _env_file=None
        ),
        legacy_seed=legacy_seed,
        is_known=is_known,
        rfc_backstop=backstop,
    )
