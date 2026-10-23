"""Google Drive connector (source name ``gdrive``, scope drive.readonly).

Sync model (per the connectors contract + C3 feature plan):

- ALLOWLIST: ``BRAIN_GDRIVE_FOLDERS`` — comma-separated immutable Drive folder
  ids. Empty (the default) means the connector fetches NOTHING: not a single
  API call is made. Only files inside allowlisted folders are surfaced.
- DEPTH: ``BRAIN_GDRIVE_DEPTH`` (default 1 = files directly in the allowlisted
  folders; hard cap 5). The full listing walks subfolders breadth-first up to
  the depth; deeper files are never listed (a file that later moves deeper
  than the cap surfaces as deleted on its next change).
- FULL LISTING (first sync, or after a reset): a ``changes.getStartPageToken``
  is captured BEFORE the folder walk, so edits made during the walk still
  arrive in the next incremental batch. The walk lists each folder's children
  WITHOUT a trash filter so trashed children are classified (deleted=True)
  rather than silently skipped.
- INCREMENTAL: ``changes.list`` with the stored startPageToken and
  ``restrictToMyDrive`` — shared drives are OUT of scope by decision (C3
  verification b): My Drive only. An invalid/expired token — Drive answers
  404 for an unknown page token and 410 when the sync window was truncated —
  yields ``reset=True`` WITH the full resync records in the same batch (the
  runner then re-runs from an empty cursor). The last page's
  ``newStartPageToken`` becomes the new cursor.
- DELETIONS: the changes feed cannot distinguish "tracked file that moved out
  of the allowlist" from "file that never was in the allowlist" without
  memory, so the incremental cursor carries the set of known in-allowlist
  file ids inside the opaque JSON blob (the cursor is runner-opaque; gtasks'
  plan blesses the same JSON-cursor pattern). For a KNOWN id: trashed /
  removed=true / parents no longer allowlisted -> ``deleted=True`` record
  (the runner marks the item ``source_deleted``; never a hard delete). For an
  unknown id outside the allowlist: skipped.
- CONTENT: Docs export text/plain, Sheets export text/csv, Slides export
  text/plain — each capped at ``BRAIN_GDRIVE_TEXT_CAP`` chars (default
  50000) with truncation flagged in meta. PDFs and images become
  AttachmentRefs fetched lazily via :meth:`DriveConnector.fetch_attachment`
  (the existing attachments pipeline reads them). Any other binary is
  metadata-only. A per-file export failure never kills the batch: 4xx notes
  ``meta["content_error"]`` and ships metadata-only (a 403 on one file is not
  an auth problem — the LIST call just succeeded with the same token);
  401/invalid_grant raises ConnectorAuthError via the base translation, and
  429/5xx/network raise ConnectorRetryableError so the runner backs off.
- version = the file's ``version`` (falls back to ``modifiedTime``);
  occurred_at = ``modifiedTime``; url = ``webViewLink``; text renders the
  modified time in the connector's tz (Asia/Jerusalem per contract).

The google libs are imported only by the shared GoogleConnector base; tests
inject a fake service — no test touches the network.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, ClassVar
from zoneinfo import ZoneInfo

from brain.connectors import register
from brain.connectors.base import (
    AttachmentRef,
    ConnectorError,
    ConnectorRetryableError,
    FetchResult,
    SourceRecord,
)
from brain.connectors.google.base import GoogleConnector, default_service_factory

SOURCE_NAME = "gdrive"
GDRIVE_SCOPE = "https://www.googleapis.com/auth/drive.readonly"

DEFAULT_TEXT_CAP = 50_000
DEFAULT_DEPTH = 1
MAX_DEPTH = 5  # hard cap on BRAIN_GDRIVE_DEPTH
_PAGE_SIZE = 200

MIME_FOLDER = "application/vnd.google-apps.folder"
MIME_DOC = "application/vnd.google-apps.document"
MIME_SHEET = "application/vnd.google-apps.spreadsheet"
MIME_SLIDE = "application/vnd.google-apps.presentation"
MIME_PDF = "application/pdf"

# The one export-by-mimetype table (C3 simplify target): Google-native types
# whose content we pull via files.export; everything else is attachments or
# metadata-only. Never an if/elif chain per type.
EXPORT_MIME: dict[str, str] = {
    MIME_DOC: "text/plain",
    MIME_SHEET: "text/csv",
    MIME_SLIDE: "text/plain",
}

KIND_OF_MIME: dict[str, str] = {
    MIME_FOLDER: "folder",
    MIME_DOC: "doc",
    MIME_SHEET: "sheet",
    MIME_SLIDE: "slide",
    MIME_PDF: "pdf",
}

_KIND_LABELS = {
    "doc": "Google Doc",
    "sheet": "Google Sheet",
    "slide": "Google Slides",
    "pdf": "PDF",
    "image": "image",
    "binary": "file",
}

_FILE_FIELDS = "id, name, mimeType, version, modifiedTime, webViewLink, size, parents, trashed"
_CHANGES_FIELDS = (
    f"nextPageToken, newStartPageToken, changes(fileId, removed, file({_FILE_FIELDS}))"
)
_CURSOR_VERSION = 1


def parse_folder_ids(raw: str) -> tuple[str, ...]:
    """Parse a BRAIN_GDRIVE_FOLDERS-style comma list (stripped, deduped)."""
    out: list[str] = []
    for part in raw.split(","):
        part = part.strip()
        if part and part not in out:
            out.append(part)
    return tuple(out)


def file_kind(mime_type: str) -> str:
    """Classify a Drive file by mime type (one table, no if/elif chains)."""
    if mime_type in KIND_OF_MIME:
        return KIND_OF_MIME[mime_type]
    if mime_type.startswith("image/"):
        return "image"
    return "binary"


def cap_text(text: str, cap: int) -> tuple[str, bool]:
    """(capped text, was_truncated)."""
    if len(text) <= cap:
        return text, False
    return text[:cap], True


def parse_drive_time(value: str | None) -> datetime | None:
    """Drive RFC 3339 modifiedTime -> aware UTC datetime (None when absent)."""
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


def _version_of(file_payload: dict[str, Any]) -> str | None:
    version = file_payload.get("version")
    if version is not None:
        return str(version)
    return file_payload.get("modifiedTime") or None


def record_from_file(
    payload: dict[str, Any],
    tz: ZoneInfo,
    *,
    text: str = "",
    truncated: bool = False,
    text_cap: int | None = None,
    attachment: Any = None,
    extra_meta: dict[str, Any] | None = None,
) -> SourceRecord:
    """Map one Drive ``files`` resource onto a SourceRecord (pure).

    Text renders as the file name, one metadata line (kind + local modified
    time), then the exported content when there is any.
    """
    mime = payload.get("mimeType", "")
    kind = file_kind(mime)
    modified = parse_drive_time(payload.get("modifiedTime"))
    label = _KIND_LABELS.get(kind, kind)
    meta_line = label
    if modified is not None:
        local = modified.astimezone(tz)
        meta_line += f" · modified {local.strftime('%Y-%m-%d %H:%M')} ({tz.key})"
    meta: dict[str, Any] = {"mime_type": mime, "kind": kind}
    if payload.get("size") is not None:
        try:
            meta["size"] = int(payload["size"])
        except (TypeError, ValueError):
            pass
    if payload.get("trashed"):
        meta["trashed"] = True
    if truncated:
        meta["truncated"] = True
        meta["text_cap"] = text_cap
    if extra_meta:
        meta.update(extra_meta)
    lines = [payload.get("name") or "", meta_line]
    if text:
        lines += ["", text]
    return SourceRecord(
        source=SOURCE_NAME,
        external_id=payload["id"],
        version=_version_of(payload),
        title=payload.get("name") or "",
        text="\n".join(lines),
        occurred_at=modified,
        url=payload.get("webViewLink"),
        meta=meta,
        attachments=(attachment,) if attachment else (),
        deleted=bool(payload.get("trashed")),
    )


def deleted_record(
    file_id: str, *, payload: dict[str, Any] | None = None, reason: str
) -> SourceRecord:
    """A deleted=True record for a file we previously ingested.

    The runner never hard-deletes: it marks the existing item
    ``source_deleted`` and hides it (self-superseded). ``payload`` carries
    whatever the change still knew (title/version for trash and move-outs;
    nothing for ``removed=true``).
    """
    payload = payload or {}
    return SourceRecord(
        source=SOURCE_NAME,
        external_id=file_id,
        version=_version_of(payload) if payload else None,
        title=payload.get("name") or "",
        text="",
        occurred_at=parse_drive_time(payload.get("modifiedTime")),
        url=payload.get("webViewLink"),
        meta={"mime_type": payload.get("mimeType", ""), "delete_reason": reason},
        deleted=True,
    )


@dataclass(frozen=True)
class _Decision:
    """What to do with one walked child / one change entry."""

    action: str  # "keep" | "deleted" | "skip"
    file_id: str = ""
    file: dict[str, Any] | None = None
    reason: str = ""


def classify_file(file_payload: dict[str, Any]) -> _Decision:
    """Classify a file listed inside an allowlisted folder tree.

    The walk proves allowlist membership by construction (it lists children
    of enqueued folders), so this only routes folder / file / trashed file.
    """
    file_id = file_payload.get("id") or ""
    if not file_id:
        return _Decision("skip")
    if file_kind(file_payload.get("mimeType", "")) == "folder":
        return _Decision("skip")
    if file_payload.get("trashed"):
        return _Decision("deleted", file_id, file_payload, "trashed")
    return _Decision("keep", file_id, file_payload)


def classify_change(
    change: dict[str, Any], allowlist: frozenset[str], known: frozenset[str]
) -> tuple[_Decision, frozenset[str]]:
    """Classify one ``changes.list`` entry; returns (decision, new_known).

    ``known`` is the set of file ids previously emitted as in-allowlist
    records (carried inside the cursor). Rules:
      - removed=true                    -> deleted (known ids only)
      - the allowlisted folder itself   -> skip (folders are never records)
      - parents intersect the allowlist, not trashed -> keep (+ known)
      - known id, now trashed           -> deleted("trashed")
      - known id, no longer allowlisted -> deleted("moved_out")
      - anything else (never known, outside allowlist) -> skip
    """
    file_payload = change.get("file") or {}
    file_id = change.get("fileId") or file_payload.get("id") or ""
    if not file_id:
        return _Decision("skip"), known
    if change.get("removed"):
        if file_id in known:
            return (
                _Decision("deleted", file_id, file_payload or None, "removed"),
                known - {file_id},
            )
        return _Decision("skip"), known
    if file_id in allowlist:
        return _Decision("skip"), known  # allowlisted folder itself changed
    parents = {str(p) for p in file_payload.get("parents") or []}
    trashed = bool(file_payload.get("trashed"))
    if parents & allowlist and not trashed:
        return _Decision("keep", file_id, file_payload), known | {file_id}
    if file_id in known:
        reason = "trashed" if trashed else "moved_out"
        return _Decision("deleted", file_id, file_payload, reason), known - {file_id}
    return _Decision("skip"), known  # outside the allowlist and never tracked


def encode_cursor(
    mode: str,
    token: str | None,
    queue: list[tuple[str, int]],
    page: str | None,
    folder: str | None,
    depth: int,
    known: frozenset[str] | set[str],
) -> str:
    """The opaque cursor: a JSON blob (changes token + walk resume state +
    known-allowlist membership). Bare Drive tokens are never JSON, so any
    non-JSON cursor decodes to "invalid -> reset"."""
    return json.dumps(
        {
            "v": _CURSOR_VERSION,
            "mode": mode,
            "token": token,
            "queue": [[folder_id, d] for folder_id, d in queue],
            "page": page,
            "folder": folder,
            "depth": depth,
            "known": sorted(known),
        },
        separators=(",", ":"),
    )


def _decode_cursor(cursor: str | None) -> tuple[dict[str, Any] | None, bool]:
    """(state, needs_reset). ``None`` state means run a full walk."""
    if not cursor:
        return None, False
    try:
        state = json.loads(cursor)
    except ValueError:
        return None, True
    if not isinstance(state, dict) or state.get("v") != _CURSOR_VERSION:
        return None, True
    return state, False


def _status_of(exc: BaseException | None) -> int | None:
    """Duck-typed HTTP status of a raw Google error (``exc.resp.status``)."""
    return getattr(getattr(exc, "resp", None), "status", None)


@register
class DriveConnector(GoogleConnector):
    """The ``gdrive`` source: allowlisted folders of the user's My Drive."""

    name: ClassVar[str] = SOURCE_NAME
    SCOPES: ClassVar[tuple[str, ...]] = (GDRIVE_SCOPE,)

    def __init__(
        self,
        *,
        token_loader: Any,
        service_factory: Any = default_service_factory,
        folders: tuple[str, ...] = (),
        text_cap: int = DEFAULT_TEXT_CAP,
        max_depth: int = DEFAULT_DEPTH,
        user_id: str = "me",
        tz: ZoneInfo | None = None,
    ) -> None:
        super().__init__(
            token_loader=token_loader, service_factory=service_factory, user_id=user_id
        )
        self._folder_ids = tuple(dict.fromkeys(folders))  # dedupe, keep order
        self._allowlist = frozenset(self._folder_ids)
        self._text_cap = max(1, int(text_cap))
        self._max_depth = min(max(int(max_depth), 1), MAX_DEPTH)
        if tz is None:
            from brain.config import get_settings  # noqa: PLC0415 (lazy, DAG-safe)

            tz = ZoneInfo(get_settings().timezone)
        self._tz = tz

    # -- fetch -------------------------------------------------------------------

    def fetch(self, cursor: str | None, *, limit: int) -> FetchResult:
        """Next batch after ``cursor``. Empty allowlist -> no API calls, ever."""
        if not self._folder_ids:
            return FetchResult(records=[], next_cursor=None, reset=False)
        state, needs_reset = _decode_cursor(cursor)
        if state is None:
            return self._fetch_full(limit=limit, reset=needs_reset)
        if state.get("mode") == "full":
            return self._fetch_full(limit=limit, reset=needs_reset, state=state)
        return self._fetch_incremental(state, limit=limit)

    # -- full walk -----------------------------------------------------------------

    def _fetch_full(
        self, *, limit: int, reset: bool, state: dict[str, Any] | None = None
    ) -> FetchResult:
        """Walk the allowlisted folders breadth-first.

        The changes token is captured BEFORE walking so edits made during the
        walk still arrive in the next incremental batch. Batches are
        page-aligned: a files.list page is never split across fetch calls;
        the pending queue rides inside the cursor.
        """
        state = dict(state or {})
        token = state.get("token") or self._start_page_token()
        queue: list[tuple[str, int]] = [
            (folder_id, int(d))
            for folder_id, d in (state.get("queue") or [(fid, 1) for fid in self._folder_ids])
        ]
        known: set[str] = set(state.get("known") or [])
        folder: str | None = state.get("folder")
        depth = int(state.get("depth") or 0)
        page: str | None = state.get("page")
        records: list[SourceRecord] = []

        def walk_cursor() -> str:
            return encode_cursor("full", token, queue, page, folder, depth, known)

        while True:
            if folder is None:
                if not queue:
                    break
                folder, depth = queue.pop(0)
                page = None
            response = self._execute(
                self._service().files().list(
                    q=f"'{folder}' in parents",
                    fields=f"nextPageToken, files({_FILE_FIELDS})",
                    pageSize=_PAGE_SIZE,
                    **({"pageToken": page} if page else {}),
                )
            )
            for child in response.get("files") or []:
                decision = classify_file(child)
                if decision.action == "keep":
                    if decision.file is None:  # defensive: keeps always carry it
                        raise ConnectorError(
                            f"drive file {decision.file_id} has no payload"
                        )
                    known.add(decision.file_id)
                    records.append(self._build_record(decision.file))
                elif decision.action == "deleted":
                    records.append(
                        deleted_record(
                            decision.file_id, payload=decision.file, reason=decision.reason
                        )
                    )
                elif (
                    file_kind(child.get("mimeType", "")) == "folder"
                    and depth + 1 <= self._max_depth
                ):
                    queue.append((child["id"], depth + 1))  # subfolder within cap
            next_page = response.get("nextPageToken")
            if next_page:
                page = next_page
                if len(records) >= limit:
                    return FetchResult(records, walk_cursor(), reset=reset)
                continue
            folder, page = None, None
            if len(records) >= limit and queue:
                return FetchResult(records, walk_cursor(), reset=reset)
        return FetchResult(
            records, encode_cursor("incr", token, [], None, None, 0, known), reset=reset
        )

    def _start_page_token(self) -> str:
        response = self._execute(self._service().changes().getStartPageToken())
        token = response.get("startPageToken")
        if not token:
            raise ConnectorError("drive returned no startPageToken")
        return str(token)

    # -- incremental -----------------------------------------------------------------

    def _fetch_incremental(self, state: dict[str, Any], *, limit: int) -> FetchResult:
        page_token = state.get("token")
        if not page_token:
            return self._fetch_full(limit=limit, reset=True)
        known: frozenset[str] = frozenset(state.get("known") or [])
        records: list[SourceRecord] = []
        while True:
            try:
                response = self._execute(
                    self._service().changes().list(
                        pageToken=page_token,
                        restrictToMyDrive=True,
                        spaces="drive",
                        fields=_CHANGES_FIELDS,
                    )
                )
            except ConnectorRetryableError as exc:
                if exc.status == 410:  # sync window truncated -> full resync
                    return self._fetch_full(limit=limit, reset=True)
                raise
            except ConnectorError as exc:
                if exc.status == 404:  # Drive: unknown/expired page token
                    return self._fetch_full(limit=limit, reset=True)
                raise
            for change in response.get("changes") or []:
                decision, known = classify_change(change, self._allowlist, known)
                if decision.action == "keep":
                    if decision.file is None:  # defensive: keeps always carry it
                        raise ConnectorError(
                            f"drive change for {decision.file_id} has no file payload"
                        )
                    records.append(self._build_record(decision.file))
                elif decision.action == "deleted":
                    records.append(
                        deleted_record(
                            decision.file_id, payload=decision.file, reason=decision.reason
                        )
                    )
            next_page = response.get("nextPageToken")
            new_token = response.get("newStartPageToken")
            if next_page:
                if len(records) >= limit:  # page-aligned stop
                    cursor = encode_cursor("incr", next_page, [], None, None, 0, known)
                    return FetchResult(records, cursor, reset=False)
                page_token = next_page
                continue
            cursor = encode_cursor(
                "incr", new_token or page_token, [], None, None, 0, known
            )
            return FetchResult(records, cursor, reset=False)

    # -- content -----------------------------------------------------------------

    def _build_record(self, file_payload: dict[str, Any]) -> SourceRecord:
        mime = file_payload.get("mimeType", "")
        kind = file_kind(mime)
        if kind in ("doc", "sheet", "slide"):
            return self._record_with_export(file_payload, EXPORT_MIME[mime])
        if kind in ("pdf", "image"):
            size = file_payload.get("size")
            attachment = AttachmentRef(
                ref_id=file_payload["id"],
                filename=file_payload.get("name") or "",
                mime_type=mime,
                size=int(size) if size else None,
            )
            return record_from_file(file_payload, self._tz, attachment=attachment)
        return record_from_file(file_payload, self._tz)  # other binaries: metadata only

    def _record_with_export(
        self, file_payload: dict[str, Any], export_mime: str
    ) -> SourceRecord:
        file_id = file_payload["id"]
        try:
            # Direct execute (not the base _execute): per-file granularity —
            # one unreadable file must not kill the batch. Only auth and
            # transient failures propagate.
            exported = (
                self._service()
                .files()
                .export(fileId=file_id, mimeType=export_mime)
                .execute()
            )
        except Exception as exc:  # noqa: BLE001 — classified below
            status = _status_of(exc)
            refresh_broken = type(exc).__name__ == "RefreshError" or (
                "invalid_grant" in str(exc)
            )
            if status == 401 or refresh_broken:
                raise self._translate_error(exc) from exc  # base mapper: single source
            if status is None or status == 429 or status >= 500:
                raise ConnectorRetryableError(
                    f"drive export {file_id}: http {status or 'network error'}",
                    status=status,
                ) from exc
            return record_from_file(
                file_payload, self._tz, extra_meta={"content_error": f"http {status}"}
            )
        raw = exported if isinstance(exported, bytes) else str(exported).encode()
        text = raw.decode("utf-8", errors="replace")
        capped, truncated = cap_text(text, self._text_cap)
        return record_from_file(
            file_payload,
            self._tz,
            text=capped,
            truncated=truncated,
            text_cap=self._text_cap,
        )

    # -- attachments -----------------------------------------------------------------

    def fetch_attachment(self, record: SourceRecord, ref: Any) -> bytes:
        data = self._execute(self._service().files().get_media(fileId=ref.ref_id))
        if not isinstance(data, bytes):
            raise ConnectorError(f"drive media for {ref.ref_id} is not bytes")
        return data
