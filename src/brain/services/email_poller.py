"""Email source poller (F2): one Gmail label -> ingest, deduped by Message-ID.

Reads a single immutable Gmail label every ``GMAIL_POLL_INTERVAL_S`` (60s),
parses each new message through the SAME paste path as `POST /capture`
(:func:`brain.services.paste.parse`, kind hint ``email``) and captures via
:meth:`IngestService.capture_parsed` — identical classification, indexing
and meta semantics as every other capture.

Deduping: a message is processed at most once, keyed by its RFC 5322
Message-ID; messages without one fall back to ``fp:`` + sha256 over
(sender, subject, internal minute). Seen ids live in the ``email_messages``
table (unique index) — re-polling can never double-capture.

Failure model (cockpit states, via brain.ops heartbeat detail):
- revoked/expired token  -> state ``needs re-auth`` (pause 5 min)
- label deleted           -> state ``label missing`` (pause 5 min)
- transient errors        -> state ``error``, exponential backoff
- healthy                 -> state ``ok``

The poller never writes to Gmail: read-only scope, list calls carry the
label ids by construction (:func:`brain.ai.gmail.gmail_list_params`).
Attachment REFERENCES (id/filename/mime/size) are stored in the item's
meta; fetching attachment bytes belongs to the attachments core (item 43).
"""

from __future__ import annotations

import hashlib
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Protocol

from brain import ops
from brain.ai.gmail import GmailAuthError, GmailClient, GmailError, GmailLabelError, GmailMessage
from brain.config import Settings
from brain.services.paste import parse

logger = logging.getLogger(__name__)

# Pauses for the non-transient failure states (5 min: the user must act).
_STALL_PAUSE_S = 300.0
_BACKOFF_CAP_S = 600.0


@dataclass(frozen=True)
class PollSummary:
    """One poll cycle's outcome (also the ops heartbeat payload)."""

    listed: int = 0
    new: int = 0
    skipped: int = 0
    errors: int = 0
    state: str = "ok"
    detail: dict[str, Any] = field(default_factory=dict)


class IngestProto(Protocol):
    """What the poller needs from IngestService (the shared capture path)."""

    def capture_parsed(
        self,
        parsed: Any,
        *,
        source: str | None = None,
        extra_meta: dict[str, Any] | None = None,
        occurred_at: Any = None,
    ) -> list[Any]: ...


class SeenRepoProto(Protocol):
    """What the poller needs from the seen-repository."""

    def seen(self, dedupe_id: str) -> bool: ...

    def mark(
        self, dedupe_id: str, gmail_id: str, rfc_message_id: str | None, item_id: Any
    ) -> None: ...


def dedupe_id_for_message(msg: GmailMessage) -> str:
    """RFC 5322 Message-ID, else a content fingerprint (never both paths for
    one message): re-polling the same message always yields the same key."""
    if msg.rfc_message_id:
        return msg.rfc_message_id.strip()
    minute = ""
    if msg.internal_date is not None:
        minute = msg.internal_date.astimezone(ops_tz_utc()).strftime("%Y%m%dT%H%M")
    payload = "\x1f".join((msg.sender or "", msg.subject or "", minute))
    return "fp:" + hashlib.sha256(payload.encode()).hexdigest()


def ops_tz_utc():  # small indirection keeps the import list tidy
    from datetime import UTC  # noqa: PLC0415

    return UTC


class EmailPoller:
    """Poll one Gmail label and capture new messages (framework-free)."""

    def __init__(
        self,
        *,
        client: GmailClient,
        ingest: IngestProto,
        seen_repo: Any,
        settings: Settings,
    ) -> None:
        self._client = client
        self._ingest = ingest
        self._seen = seen_repo
        self._settings = settings
        self.failures = 0

    # --- single cycle -------------------------------------------------------

    def poll_once(self) -> PollSummary:
        if not self._settings.gmail_label_id:
            return PollSummary(state="disabled", detail={"reason": "GMAIL_LABEL_ID unset"})
        try:
            stubs = self._client.list_messages(
                query=self._settings.gmail_query, label_ids=[self._settings.gmail_label_id]
            )
        except GmailAuthError as exc:
            return self._list_failed("needs re-auth", exc)
        except GmailLabelError as exc:
            return self._list_failed("label missing", exc)
        except GmailError as exc:
            return self._list_failed("error", exc)

        new = skipped = errors = 0
        for stub in stubs:
            try:
                outcome = self._process_message(stub)
            except Exception as exc:  # noqa: BLE001 (one bad message never stops the poll)
                errors += 1
                ops.record_event(
                    "email",
                    "message",
                    ok=False,
                    detail={"gmail_id": stub.id, "error": f"{type(exc).__name__}: {exc}"[:200]},
                )
                logger.warning("email: message %s failed: %s", stub.id, exc, exc_info=True)
                continue
            if outcome:
                new += 1
            else:
                skipped += 1
        summary = PollSummary(
            listed=len(stubs),
            new=new,
            skipped=skipped,
            errors=errors,
            state="ok",
            detail={"listed": len(stubs), "new": new, "skipped": skipped, "errors": errors},
        )
        self.failures = 0  # a clean list+process cycle resets the backoff
        ops.record_event("email", "poll", ok=True, detail=dict(summary.detail))
        return summary

    def _list_failed(self, state: str, exc: Exception) -> PollSummary:
        ops.record_event(
            "email",
            "poll",
            ok=False,
            detail={"state": state, "error": str(exc)[:200]},
        )
        return PollSummary(state=state, detail={"error": str(exc)[:200]})

    def _process_message(self, stub: Any) -> bool:
        """Capture one message; returns False when it was skipped as seen."""
        # Fast path: the ledger already knows this gmail id (every processed
        # message is marked with its gmail id) — skip without a GET. The
        # Message-ID dedupe below stays as the backstop.
        if self._seen.seen_gmail(stub.id):
            return False
        msg = self._client.get_message(stub.id)
        # Defence in depth: get_message can fetch ANY message by id — only
        # messages still carrying the polled label may be captured. Skipped
        # messages are NOT marked, so a re-labelled message captures again.
        if self._settings.gmail_label_id not in msg.label_ids:
            logger.info("email: message %s no longer carries the polled label — skipped", msg.id)
            return False
        dedupe_id = dedupe_id_for_message(msg)
        if self._seen.seen(dedupe_id):
            return False
        parsed = parse(msg.body_text, kind_hint="email", tz=self._settings.timezone)
        occurred_at = None
        if parsed.chunks and all(c.occurred_at is None for c in parsed.chunks):
            occurred_at = msg.internal_date
        meta = {
            "gmail_id": msg.id,
            "rfc_message_id": msg.rfc_message_id,
            "from": msg.sender,
            "subject": msg.subject,
            "attachments": [
                {
                    "attachment_id": a.attachment_id,
                    "filename": a.filename,
                    "mime_type": a.mime_type,
                    "size": a.size,
                }
                for a in msg.attachments
            ],
        }
        items = self._ingest.capture_parsed(
            parsed, source="email", extra_meta=meta, occurred_at=occurred_at
        )
        item_id = items[0].id if items else None
        self._seen.mark(dedupe_id, msg.id, msg.rfc_message_id, item_id)
        return True

    # --- loop ---------------------------------------------------------------

    def backoff_seconds(self) -> float:
        return min(
            float(self._settings.gmail_poll_interval_s) * (2 ** max(self.failures, 0)),
            _BACKOFF_CAP_S,
        )

    def run_forever(self) -> int:
        """Poll until interrupted; returns a process exit code (0)."""
        if not self._settings.gmail_label_id:
            logger.error("email poller disabled (GMAIL_LABEL_ID unset)")
            return 78
        while True:
            try:
                summary = self.poll_once()
            except KeyboardInterrupt:
                raise
            heartbeat_email(summary)
            if summary.state == "ok":
                self.failures = 0
                pause = float(self._settings.gmail_poll_interval_s)
            elif summary.state == "error":
                self.failures += 1
                pause = self.backoff_seconds()
            else:  # needs re-auth / label missing: user action required
                pause = _STALL_PAUSE_S
            logger.info(
                "email: state=%s listed=%s new=%s skipped=%s errors=%s; next poll in %.0fs",
                summary.state,
                summary.listed,
                summary.new,
                summary.skipped,
                summary.errors,
                pause,
            )
            time.sleep(pause)


def heartbeat_email(summary: PollSummary) -> None:
    """Cockpit heartbeat carrying the poller state (never raises)."""
    detail = {"state": summary.state, **(summary.detail or {})}
    try:
        ops.heartbeat("email", detail)
    except Exception:  # noqa: BLE001 (ops must never break polling)
        logger.debug("email: ops.heartbeat raised", exc_info=True)
