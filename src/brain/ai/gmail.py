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
import time
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


class GmailError(RuntimeError):
    """A Gmail call failed transiently (network, 5xx) — retry with backoff.

    ``status`` carries the HTTP status when the error came from a response
    (None otherwise); the connector adapter reads it for the cursor-invalid
    (404 history) and retryable (429/5xx) decisions.
    """

    def __init__(self, message: str, *, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


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
    """Map duck-typed Google errors onto the Gmail error taxonomy — THIN by
    design: the HTTP status table lives ONCE in
    :meth:`brain.connectors.google.base.GoogleConnector._translate_error` and
    this wrapper only re-labels the connector errors into the Gmail classes
    the client's callers expect (C5 consolidation, single mapping)."""
    from brain.connectors.base import ConnectorAuthError  # noqa: PLC0415 (lazy)
    from brain.connectors.google.base import GoogleConnector  # noqa: PLC0415 (lazy: no cycle)

    translated = GoogleConnector._translate_error(exc)
    status = getattr(translated, "status", None)
    if isinstance(translated, ConnectorAuthError):
        return GmailAuthError(str(translated), status=status)
    if status == 404:
        return GmailLabelError(str(translated), status=status)
    content = getattr(exc, "content", b"") or b""
    if b"Invalid label" in content or b"Requested entity was not found" in content:
        return GmailLabelError(str(translated), status=status)
    return GmailError(str(translated), status=status)


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

    Quota hygiene (the live 403 rateLimitExceeded): every API call goes
    through :meth:`_execute`, which (a) paces consecutive calls at least
    ``min_interval_s`` apart (Gmail's 'Total Query Cost' budget is
    100 units/minute/USER, ~5 units per call — an unpaced burst of
    messages.get blows it inside one run), and (b) retries rate-limit
    errors (403 rateLimitExceeded / 429) with exponential backoff,
    honoring a ``Retry-After`` header when the response carries one —
    clamped to :data:`_MAX_IN_RUN_BACKOFF_S`: a larger server ask stops
    the retries and raises, so the runner's between-runs backoff owns the
    long wait. Any OTHER error is translated and raised as before (a
    plain 403 is never retried).

    Blocking-sleep note: every consumer of this client (``connectors
    sync`` / ``connectors adopt`` / the ``gmail-poll`` launchd loop) is a
    synchronous CLI process on its own main thread — no asyncio event
    loop and no API request thread ever reaches :meth:`_execute`.
    """

    # The longest in-run backoff: beyond this the wait belongs to the
    # runner's between-runs backoff, not to one blocking call.
    _MAX_IN_RUN_BACKOFF_S = 60.0

    def __init__(
        self,
        *,
        token_loader: TokenLoader | None = None,
        service_factory: ServiceFactory = default_service_factory,
        user_id: str = "me",
        service: Any | None = None,
        min_interval_s: float | None = None,
        max_retries: int = 3,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._token_loader = token_loader
        self._service_factory = service_factory
        self._user_id = user_id
        # Optional pre-built service (the connector passes the shared OAuth
        # service it already built — ONE service per connector, not two).
        self._service: Any = service
        if min_interval_s is None:
            from brain.config import get_settings  # noqa: PLC0415

            min_interval_s = float(getattr(get_settings(), "gmail_min_call_interval_s", 0.0))
        self._min_interval_s = float(min_interval_s)
        self._max_retries = max(0, int(max_retries))
        self._sleep = sleep
        self._clock = clock
        self._last_call_at: float | None = None

    def _get_service(self) -> Any:
        if self._service is None:
            token = self._token_loader() if self._token_loader else None
            if not token:
                raise GmailAuthError("no refresh token in the keychain — run brain gmail-auth")
            self._service = self._service_factory(token, self._user_id)
        return self._service

    def _pace(self) -> None:
        """Wait so consecutive calls are >= min_interval_s apart. A broken
        timer must never kill a sync (the throttle is hygiene, not
        correctness): sleep failures are swallowed."""
        if self._min_interval_s <= 0:
            self._last_call_at = self._clock()
            return
        now = self._clock()
        if self._last_call_at is not None:
            wait = self._last_call_at + self._min_interval_s - now
            if wait > 0:
                try:
                    self._sleep(wait)
                except Exception:  # noqa: BLE001 (hygiene, not correctness)
                    logger.warning("gmail pacing sleep failed — continuing unpaced")
        self._last_call_at = self._clock()

    @staticmethod
    def _retry_after(exc: Exception) -> float | None:
        """A duck-typed Retry-After header (seconds), or None."""
        headers = getattr(getattr(exc, "resp", None), "headers", None) or {}
        if not hasattr(headers, "get"):
            return None
        value = headers.get("Retry-After")
        if value is None:
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    def _execute(self, request: Any) -> Any:
        from brain.connectors.google.base import is_rate_limited  # noqa: PLC0415

        for attempt in range(self._max_retries + 1):
            self._pace()
            try:
                return request.execute()
            except Exception as exc:  # noqa: BLE001 (translated below)
                translated = _translate_error(exc)
                throttled = is_rate_limited(exc) or getattr(translated, "status", None) == 429
                if not throttled or attempt >= self._max_retries:
                    raise translated from exc
                delay = self._retry_after(exc)
                if delay is None:
                    delay = 2.0 * (2**attempt)  # 2s, 4s, 8s…
                if delay > self._MAX_IN_RUN_BACKOFF_S:
                    # The server asks for a longer wait than one run may
                    # block: stop retrying — the runner's between-runs
                    # backoff owns the long wait.
                    logger.warning(
                        "gmail asked for %.0fs backoff (> %.0fs in-run cap) — giving up "
                        "to the runner's backoff",
                        delay, self._MAX_IN_RUN_BACKOFF_S,
                    )
                    raise translated from exc
                logger.warning(
                    "gmail rate limited (attempt %d/%d) — retrying in %.1fs",
                    attempt + 1, self._max_retries, delay,
                )
                try:
                    self._sleep(delay)
                except Exception:  # noqa: BLE001 (hygiene, not correctness)
                    logger.warning("gmail backoff sleep failed — continuing")

    def list_messages_page(
        self, *, query: str, label_ids: Sequence[str],
        page_token: str | None = None, max_results: int | None = None,
    ) -> tuple[list[GmailMessageStub], str | None]:
        """ONE users.messages.list page: (stubs, next_page_token).

        The ONLY listing entry point (the eager whole-label walk is gone):
        callers decide whether the next page is needed, so a ``--limit``
        run never walks the label just to fill a small limit (each page
        costs 5 quota units).
        """
        params = gmail_list_params(query, label_ids)
        if max_results:
            params["maxResults"] = max_results
        if page_token:
            params["pageToken"] = page_token
        result = self._execute(
            self._get_service().users().messages().list(userId=self._user_id, **params)
        )
        stubs = [
            GmailMessageStub(id=m["id"], thread_id=m.get("threadId", ""))
            for m in (result.get("messages") or [])
        ]
        return stubs, result.get("nextPageToken")

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
        return decode_attachment_payload(raw, attachment_id, message_id)

    def list_history(self, *, start_history_id: str, label_id: str) -> Iterator[dict[str, Any]]:
        """Yield raw history pages after ``start_history_id`` for one label.

        The cursor-invalid case (an expired historyId) surfaces as a 404 —
        the CALLER (the connector) decides what a 404 means here; the client
        only translates transport errors.
        """
        service = self._get_service()
        page_token: str | None = None
        while True:
            params: dict[str, Any] = {
                "startHistoryId": start_history_id,
                "labelId": label_id,
            }
            if page_token:
                params["pageToken"] = page_token
            page = self._execute(service.users().history().list(userId=self._user_id, **params))
            yield page
            page_token = page.get("nextPageToken")
            if not page_token:
                return

    def profile_history_id(self) -> str | None:
        """The mailbox's CURRENT historyId (a fresh sync cursor)."""
        service = self._get_service()
        raw = self._execute(service.users().getProfile(userId=self._user_id))
        history_id = raw.get("historyId") if isinstance(raw, dict) else None
        return str(history_id) if history_id else None


def decode_attachment_payload(raw: Any, attachment_id: str, message_id: str) -> bytes:
    """Decode an attachments.get response (real shape: {'size', 'data': b64url}).

    A missing/empty 'data' is an error, never an empty attachment (F3).
    Module-level so the connector's fetch_attachment reuses the same decode.
    """
    import base64  # noqa: PLC0415

    data = raw.get("data") if isinstance(raw, dict) else None
    if not data:
        raise GmailError(f"attachment {attachment_id} of {message_id} carries no 'data'")
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))


def _header(payload: dict[str, Any], name: str) -> str | None:
    # NOTE: callers pass the MESSAGE payload (raw["payload"]), so headers sit
    # directly on it. (The old double lookup payload["payload"]["headers"]
    # never matched anything — headers parsed as None; fixed with the C5
    # payload-fixture tests.)
    for header in payload.get("headers", []):
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
