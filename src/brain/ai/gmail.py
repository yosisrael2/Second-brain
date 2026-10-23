"""Gmail read client (Strategy seam for the email source, F2).

Design (per the F2 investigation + decision):
- READ-ONLY by construction: the only scope requested is ``gmail.readonly``
  and every list call carries the configured label ids — the one function
  :func:`gmail_list_params` builds list params, and it refuses to produce
  params without ``labelIds``.
- The OAuth refresh token lives in the macOS login keychain (never in the
  repo, never in ``.env``); this module only consumes it via a
  ``token_loader`` callable.
- The google libraries are an optional extra (``pip install -e '.[gmail]'``);
  they are imported lazily so the core suite runs without them. Tests inject
  a ``service_factory`` and duck-typed errors — no test touches Google.
- Attachments are surfaced as REFERENCES only in F2 (filename/mime/size/
  attachment id); fetching bytes is the attachments core's job (item 43).
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from email import policy
from email.parser import BytesParser
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, Protocol

logger = logging.getLogger(__name__)

GMAIL_SCOPE = "https://www.googleapis.com/auth/gmail.readonly"

# list_messages follows nextPageToken at most this many pages (Gmail's
# default page is ~100 messages; a 60s poll is unlikely to need even two,
# so the cap only bounds a pathological backlog walk).
_MAX_LIST_PAGES = 5


class GmailError(RuntimeError):
    """A Gmail call failed transiently (network, 5xx) — retry with backoff."""


class GmailAuthError(GmailError):
    """Authentication/authorization failed (revoked or expired refresh
    token) — the cockpit reports this as 'email: needs re-auth'."""


class GmailLabelError(GmailError):
    """The configured label id no longer exists — 'email: label missing'."""


def gmail_list_params(query: str, label_ids: Sequence[str]) -> dict[str, Any]:
    """Build ``users.messages.list`` params — labelIds present BY CONSTRUCTION.

    Every list call in the codebase goes through this function; it refuses to
    return params without at least one label id, so the poller can never
    accidentally read the whole mailbox.
    """
    labels = list(label_ids)
    if not labels:
        raise ValueError("gmail list params require at least one label id")
    params: dict[str, Any] = {"labelIds": labels}
    if query:
        params["q"] = query
    return params


@dataclass(frozen=True)
class GmailMessageStub:
    """A listed message: just its ids (get_message fetches the body)."""

    id: str
    thread_id: str


@dataclass(frozen=True)
class GmailAttachmentRef:
    """A reference to an attachment — bytes are NOT fetched in F2."""

    attachment_id: str
    filename: str
    mime_type: str
    size: int


@dataclass(frozen=True)
class GmailMessage:
    """A full message: headers + body text + attachment references."""

    id: str
    thread_id: str
    rfc_message_id: str | None
    sender: str | None
    subject: str | None
    internal_date: datetime | None
    label_ids: list[str] = field(default_factory=list)
    body_text: str = ""
    attachments: list[GmailAttachmentRef] = field(default_factory=list)


class GmailClient(Protocol):
    """What the email poller needs from Gmail (Strategy)."""

    def list_messages(self, *, query: str, label_ids: Sequence[str]) -> list[GmailMessageStub]:
        """List message stubs matching the query, scoped to the label ids."""
        ...

    def get_message(self, message_id: str) -> GmailMessage:
        """Fetch one full message."""
        ...

    def get_attachment(self, message_id: str, attachment_id: str) -> bytes:
        """Fetch one attachment's bytes (users.messages.attachments.get).

        Still read-only and label-scoped by construction: the caller only
        reaches here for message ids listed under the configured label.
        """
        ...

    def close(self) -> None: ...


def _translate_error(exc: Exception) -> Exception:
    """Map duck-typed Google errors onto the Gmail error taxonomy.

    Works without google libs importable: HttpError carries ``.resp.status``,
    RefreshError is recognized by type name.
    """
    if type(exc).__name__ == "RefreshError" or "invalid_grant" in str(exc):
        return GmailAuthError(f"refresh token rejected ({exc})")
    resp = getattr(exc, "resp", None)
    status = getattr(resp, "status", None)
    if status is not None:
        if status in (401,):
            return GmailAuthError(f"gmail http {status}")
        if status == 404:
            return GmailLabelError("label or message not found (404)")
        if status >= 500 or status == 429:
            return GmailError(f"gmail http {status}")
        content = getattr(exc, "content", b"") or b""
        if b"Invalid label" in content or b"Requested entity was not found" in content:
            return GmailLabelError("label not found")
        return GmailError(f"gmail http {status}")
    return GmailError(f"gmail call failed: {type(exc).__name__}: {exc}")


TokenLoader = Callable[[], str]
# (refresh_token, user_id) -> service-like object. Production passes
# :func:`default_service_factory` (which imports the google libs); tests pass
# a fake — no google import ever happens in tests.
ServiceFactory = Callable[[str, str], Any]


def default_service_factory(refresh_token: str, user_id: str) -> Any:
    """Production service factory: the ONLY place google libs are imported.

    google-auth cannot refresh an access token with the refresh token alone:
    Credentials also needs the OAuth client fields (client_id, client_secret,
    token_uri) — they come from the Desktop client secret JSON, the SAME file
    ``brain gmail-auth`` uses (outside the repo; GMAIL_CREDENTIALS_PATH).
    """
    from brain.config import get_settings  # noqa: PLC0415  (lazy; config never imports ai)

    path = get_settings().gmail_credentials_path
    client_file = Path(path).expanduser() if path else None
    if not client_file or not client_file.is_file():
        raise GmailAuthError(
            f"client secret missing/unreadable at {path!r} — re-run `brain gmail-auth "
            "--client-secret /path/to/secret.json`"
        )
    try:
        data = json.loads(client_file.read_text())
    except (OSError, ValueError) as exc:
        raise GmailAuthError(f"client secret unreadable at {path!r}: {type(exc).__name__}") from exc
    client = data.get("installed") or data.get("web") or {}
    client_id = client.get("client_id")
    client_secret = client.get("client_secret")
    token_uri = client.get("token_uri")
    if not (client_id and client_secret and token_uri):
        raise GmailAuthError(
            f"client secret file at {path!r} is not an OAuth client JSON "
            "(client_id/client_secret/token_uri missing)"
        )
    try:
        # Lazy + importlib: keeps mypy clean whether or not the gmail extra is
        # installed (warn_unused_ignores would make static ignores flaky).
        import importlib  # noqa: PLC0415

        credentials_mod = importlib.import_module("google.oauth2.credentials")
        discovery = importlib.import_module("googleapiclient.discovery")
    except ImportError as exc:  # pragma: no cover - env without the extra
        raise GmailError(
            "google libraries not installed — run `pip install -e '.[gmail]'`"
        ) from exc
    credentials = credentials_mod.Credentials(
        token=None,
        refresh_token=refresh_token,
        client_id=client_id,
        client_secret=client_secret,
        token_uri=token_uri,
        scopes=[GMAIL_SCOPE],
    )
    return discovery.build("gmail", "v1", credentials=credentials, cache_discovery=False)


class RestGmailClient:
    """GmailClient over google-api-python-client (via an injectable factory).

    Error mapping is duck-typed (``exc.resp.status`` / type names) so it is
    testable without google libs installed.
    """

    def __init__(
        self,
        *,
        token_loader: TokenLoader,
        service_factory: ServiceFactory = default_service_factory,
        user_id: str = "me",
    ) -> None:
        self._token_loader = token_loader
        self._service_factory = service_factory
        self._user_id = user_id
        self._service: Any = None

    def _get_service(self) -> Any:
        if self._service is None:
            token = self._token_loader()
            if not token:
                raise GmailAuthError("no refresh token in the keychain — run brain gmail-auth")
            self._service = self._service_factory(token, self._user_id)
        return self._service

    def _execute(self, request: Any) -> Any:
        try:
            return request.execute()
        except Exception as exc:  # noqa: BLE001 (translated below)
            raise _translate_error(exc) from exc

    def list_messages(self, *, query: str, label_ids: Sequence[str]) -> list[GmailMessageStub]:
        params = gmail_list_params(query, label_ids)
        service = self._get_service()
        stubs: list[GmailMessageStub] = []
        page_token: str | None = None
        for _page in range(_MAX_LIST_PAGES):
            call_params = dict(params)
            if page_token:
                call_params["pageToken"] = page_token
            result = self._execute(
                service.users().messages().list(userId=self._user_id, **call_params)
            )
            stubs.extend(
                GmailMessageStub(id=m["id"], thread_id=m.get("threadId", ""))
                for m in (result.get("messages") or [])
            )
            page_token = result.get("nextPageToken")
            if not page_token:
                break
        return stubs

    def get_message(self, message_id: str) -> GmailMessage:
        service = self._get_service()
        raw = self._execute(
            service.users().messages().get(userId=self._user_id, id=message_id, format="full")
        )
        return _message_from_payload(raw)

    def get_attachment(self, message_id: str, attachment_id: str) -> bytes:
        service = self._get_service()
        raw = self._execute(
            service.users()
            .messages()
            .attachments()
            .get(userId=self._user_id, messageId=message_id, id=attachment_id)
        )
        # Real shape: {'size': int, 'data': base64url}. A missing/empty 'data'
        # is an error, never an empty attachment (F3 round 1).
        data = raw.get("data") if isinstance(raw, dict) else None
        if not data:
            raise GmailError(f"attachment {attachment_id} of {message_id} carries no 'data'")
        import base64  # noqa: PLC0415

        return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))


def _header(payload: dict[str, Any], name: str) -> str | None:
    for header in payload.get("payload", {}).get("headers", []):
        if header.get("name", "").lower() == name.lower():
            return header.get("value")
    return None


def _walk_text_parts(payload: dict[str, Any]) -> list[tuple[str, str]]:
    """(mimetype, urlsafe-b64 data) for text parts, depth-first."""
    parts: list[tuple[str, str]] = []
    mime = payload.get("mimeType", "")
    body = payload.get("body", {}) or {}
    data = body.get("data")
    if mime.startswith("text/") and data:
        parts.append((mime, data))
    for part in payload.get("parts", []) or []:
        parts.extend(_walk_text_parts(part))
    return parts


def _decode_b64(data: str) -> str:
    import base64  # noqa: PLC0415

    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4)).decode("utf-8", errors="replace")


def _message_from_payload(raw: dict[str, Any]) -> GmailMessage:
    text_parts = _walk_text_parts(raw.get("payload", {}) or {})
    body_text = "\n".join(_decode_b64(data) for _mime, data in text_parts)
    internal_ms = raw.get("internalDate")
    internal_date: datetime | None = None
    if internal_ms:
        internal_date = datetime.fromtimestamp(int(internal_ms) / 1000, tz=UTC)
    rfc = _header(raw.get("payload", {}), "Message-ID")
    attachments: list[GmailAttachmentRef] = []
    for part in _iter_attachment_parts(raw.get("payload", {}) or {}):
        attachments.append(
            GmailAttachmentRef(
                attachment_id=part["body"].get("attachmentId", ""),
                filename=part.get("filename") or "",
                mime_type=part.get("mimeType") or "",
                size=int(part.get("body", {}).get("size") or 0),
            )
        )
    parsed_headers = BytesParser(policy=policy.default).parsebytes(
        _header_block(raw.get("payload", {}))
    )
    date_hdr = parsed_headers.get("Date")
    if internal_date is None and date_hdr is not None:
        try:
            internal_date = parsedate_to_datetime(str(date_hdr))
        except (TypeError, ValueError):
            internal_date = None
    return GmailMessage(
        id=raw["id"],
        thread_id=raw.get("threadId", ""),
        rfc_message_id=rfc,
        sender=_header(raw.get("payload", {}), "From"),
        subject=_header(raw.get("payload", {}), "Subject"),
        internal_date=internal_date,
        label_ids=list(raw.get("labelIds") or []),
        body_text=body_text,
        attachments=attachments,
    )


def _header_block(payload: dict[str, Any]) -> bytes:
    lines = [f"{h.get('name')}: {h.get('value')}" for h in payload.get("headers", [])]
    return "\n".join(lines).encode()


def _iter_attachment_parts(payload: dict[str, Any]) -> Iterator[dict[str, Any]]:
    body = payload.get("body", {}) or {}
    if body.get("attachmentId"):
        yield payload
    for part in payload.get("parts", []) or []:
        yield from _iter_attachment_parts(part)
    body = payload.get("body", {}) or {}
    if body.get("attachmentId"):
        yield payload
    for part in payload.get("parts", []) or []:
        yield from _iter_attachment_parts(part)
