"""gdrive's conformance donation (the sweep's fourth customer).

``build(api)`` -> a FRESH :class:`DriveConnector` whose Drive service is
backed by the kit's :class:`~brain.connectors.conformance.FakeSourceAPI`.
The fake account is Drive-shaped:

- ONE allowlisted folder (``kit-folder``, pinned through the public
  constructor — the sweep's folders allowlist is non-empty, an empty
  allowlist would make zero API calls). ``changes().getStartPageToken``
  starts a WALK: the kit api is drained FRESH and buffered; ``files().list``
  serves the buffer page by page as Google Docs (exportable mimes — the
  adapter renders exported text).
- ``files().export(fileId, mimeType)`` returns the kit record's text (the
  exported-content path the adapter renders).
- ``changes().list`` serves the CURRENT dataset as change entries every
  time (a quiet Drive: re-deliveries the runner's version skip absorbs).
- A kit ``next_fetch_only`` (the RESET check) surfaces as the Google-shaped
  410 on ``changes().list`` — the adapter's truncated-sync-window path
  (full resync, ``reset=True``). The 410 raises before anything is consumed,
  so the resync's fresh walk reads the armed subset.

The connector is built through its PUBLIC constructor with an injected
4-arg service factory (no network), so the sweep exercises the real
construction (API_NAME/API_VERSION, folders allowlist, tz from settings).
"""

from __future__ import annotations

from typing import Any

from brain.connectors.google.drive import DriveConnector

_FOLDER_ID = "kit-folder"
_DOC_MIME = "application/vnd.google-apps.document"
_EXPORT_MIME = "text/plain"
_TOKEN = "1"


class _FakeHttpError(Exception):
    """Duck-typed googleapiclient.errors.HttpError: carries .resp.status."""

    def __init__(self, status: int, message: str = "") -> None:
        super().__init__(message or f"http {status}")
        self.resp = type("Resp", (), {"status": status})()


def _file_from(record: Any) -> dict[str, Any]:
    """Kit record -> Drive ``files`` resource (the adapter maps it back)."""
    modified = None
    if record.occurred_at is not None:
        modified = record.occurred_at.strftime("%Y-%m-%dT%H:%M:%SZ")
    return {
        "id": record.external_id,
        "name": record.title,
        "mimeType": _DOC_MIME,
        "version": record.version,
        "modifiedTime": modified,
        "webViewLink": f"https://drive.google.com/file/d/{record.external_id}/view",
        "parents": [_FOLDER_ID],
        "trashed": bool(record.deleted),
    }


class KitDriveService:
    """Drive-shaped fake backed by the kit's FakeSourceAPI.

    One WALK (from ``getStartPageToken`` to the last ``files().list`` page)
    drains the kit api fresh; the 410 on an incremental ``changes().list``
    raises before anything is consumed, so the adapter's full resync
    re-reads the source.
    """

    def __init__(self, api: Any) -> None:
        self._api = api
        self._walk_buffer: list[dict[str, Any]] | None = None

    def _drain_fresh(self) -> list[dict[str, Any]]:
        """Drain the kit api completely; return the file resources (kit order)."""
        buffer: list[dict[str, Any]] = []
        cursor: str | None = None
        while True:
            result = self._api.fetch(cursor, limit=self._api.page_size)
            buffer.extend(_file_from(record) for record in result.records)
            cursor = result.next_cursor
            if cursor is None:
                break
        return buffer

    def changes(self) -> _KitChanges:
        return _KitChanges(self)

    def files(self) -> _KitFiles:
        return _KitFiles(self)


class _KitChanges:
    def __init__(self, svc: KitDriveService) -> None:
        self._svc = svc

    def getStartPageToken(self) -> _KitRequest:
        # A walk starts here: drain the kit api fresh.
        self._svc._walk_buffer = self._svc._drain_fresh()
        return _KitRequest({"startPageToken": _TOKEN})

    def list(self, **_params: Any) -> _KitRequest:
        # The kit armed a reset for the next fetch: the sync window is
        # truncated (Drive's real cursor invalidation) — the adapter's 410
        # path. The error rides the REQUEST (raising inside list() would
        # escape the adapter's _execute translation — arguments evaluate
        # outside it), and nothing is consumed: the resync's fresh walk
        # reads the armed subset.
        if getattr(self._svc._api, "_next_only", None) is not None:
            return _KitRequest(None, error=_FakeHttpError(410))
        changes = [
            {"fileId": record.external_id, "file": _file_from(record)}
            for record in self._svc._api.records  # the CURRENT dataset
        ]
        return _KitRequest(
            {"changes": changes, "newStartPageToken": _TOKEN}
        )


class _KitFiles:
    def __init__(self, svc: KitDriveService) -> None:
        self._svc = svc

    def list(self, **params: Any) -> _KitRequest:
        buffer = self._svc._walk_buffer or []
        page_size = int(params.get("pageSize") or _PAGE_SIZE_DEFAULT)
        token = params.get("pageToken")
        start = int(token) if token else 0
        page = buffer[start : start + page_size]
        nxt = start + len(page)
        result: dict[str, Any] = {"files": page}
        if nxt < len(buffer):
            result["nextPageToken"] = str(nxt)
        return _KitRequest(result)

    def export(self, fileId: str, mimeType: str) -> _KitRequest:
        assert mimeType == _EXPORT_MIME
        for record in self._svc._api.records:
            if record.external_id == fileId:
                return _KitRequest(record.text.encode("utf-8"))
        raise _FakeHttpError(404, f"kit file {fileId} vanished")


_PAGE_SIZE_DEFAULT = 200


class _KitRequest:
    """A google-api-python-client style request: .execute() returns or raises."""

    def __init__(self, result: Any, error: Exception | None = None) -> None:
        self._result = result
        self._error = error

    def execute(self) -> Any:
        if self._error is not None:
            raise self._error
        return self._result


def build(api: Any) -> DriveConnector:
    """The registered gdrive connector on a fake Drive backed by ``api``."""
    return DriveConnector(
        token_loader=lambda: "refresh-token",
        service_factory=lambda token, scopes, api_name, api_version: KitDriveService(api),
        folders=(_FOLDER_ID,),
    )
