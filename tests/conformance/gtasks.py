"""gtasks' conformance donation (the sweep's first real customer).

``build(api)`` returns a FRESH :class:`GTasksConnector` whose Tasks service
is backed by the kit's :class:`~brain.connectors.conformance.FakeSourceAPI`.
The mapping is the adapter's REAL shape, only the transport is fake:

- ONE task list (``kit-list``); ``tasks().list`` serves the kit dataset as
  users.tasks resources (``id``/``title``/``notes``/``updated``/``deleted``),
  paged with the adapter's real ``maxResults``.
- The kit API is drained ONCE per ``tasks().list`` start and buffered; the
  ``updatedMin`` watermark filter happens in the service (Google semantics:
  inclusive, server-side). The ADAPTER's watermark cursor is what the kit
  measures — the service deliberately does not paginate by the adapter's
  watermark, exactly like the real source (pages follow ``pageToken``, the
  feed is bounded by ``updatedMin``).
- Kit stamps the kit itself mangles (``mark_deleted`` -> ``updated``:
  "deleted", the CHANGE check's "conformance-v2") are unparseable; the
  service treats an unparseable stamp as "no ordering information" and
  INCLUDES the record on incremental syncs (conservative: never hide a
  record the adapter must see). The adapter's own sort tolerates these
  (``_stamp_order`` puts them first).
- A kit fetch that reports ``reset=True`` (the RESET check's
  ``next_fetch_only``) invalidates the watermark: on an INCREMENTAL call
  the service raises ``ConnectorError(status=410)`` — the adapter's real
  cursor-invalidation path (full resync, ``reset=True`` to the runner).

The connector is built the same way the adapter's own unit tests build it
(``object.__new__`` + injected ``_service``): no OAuth, no network.
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

from brain.connectors.google.tasks import GTasksConnector

_LIST_ID = "kit-list"
_LIST_TITLE = "Kit list"
_MIN_STAMP = datetime.min.replace(tzinfo=UTC)


class _FakeHttpError(Exception):
    """Duck-typed googleapiclient.errors.HttpError: carries .resp.status —
    the shape base._translate_error maps onto the connector error taxonomy
    (410 -> retryable with .status=410 -> the adapter's real reset path)."""

    def __init__(self, status: int, message: str = "") -> None:
        super().__init__(message or f"http {status}")
        self.resp = SimpleNamespace(status=status)


def _stamp(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _task_from(record: Any) -> dict[str, Any]:
    """Kit record -> users.tasks resource (the adapter maps it back)."""
    task: dict[str, Any] = {
        "id": record.external_id,
        "title": record.title,
        "notes": record.text,
        "updated": record.version,
        "status": "needsAction",
    }
    if record.deleted:
        task["deleted"] = True
    return task


class KitTasksService:
    """tasks()-shaped fake backed by the kit's FakeSourceAPI.

    One WALK (a ``tasks().list`` sequence starting without ``pageToken``)
    drains the kit api fresh — a 410 on an incremental walk raises before
    anything is cached, so the adapter's full resync re-reads the source.
    """

    def __init__(self, api: Any) -> None:
        self._api = api
        self._walk_buffer: list[dict[str, Any]] | None = None
        self.tasklists_pages = [{"items": [{"id": _LIST_ID, "title": _LIST_TITLE}]}]

    def tasklists(self) -> _KitCollection:
        return _KitCollection(self, "tasklists")

    def tasks(self) -> _KitCollection:
        return _KitCollection(self, "tasks")

    def _drain_fresh(self) -> tuple[list[dict[str, Any]], bool]:
        """Drain the kit api completely; return (task resources, reset seen)."""
        buffer: list[dict[str, Any]] = []
        cursor: str | None = None
        reset_seen = False
        while True:
            result = self._api.fetch(cursor, limit=self._api.page_size)
            reset_seen = reset_seen or result.reset
            buffer.extend(_task_from(record) for record in result.records)
            cursor = result.next_cursor
            if cursor is None:
                break
        return buffer, reset_seen


class _KitRequest:
    def __init__(self, svc: KitTasksService, kind: str, params: dict[str, Any]) -> None:
        self._svc = svc
        self._kind = kind
        self._params = params

    def execute(self) -> dict[str, Any]:
        if self._kind == "tasklists":
            return self._svc.tasklists_pages.pop(0) if self._svc.tasklists_pages else {
                "items": []
            }
        assert self._params.get("tasklist") == _LIST_ID
        updated_min = self._params.get("updatedMin")
        if self._params.get("pageToken") is None:
            # A new walk: drain the kit api fresh. A reset flagged by the kit
            # on an INCREMENTAL walk is the source invalidating the watermark
            # (kit RESET check): raise the Google-shaped 410 BEFORE caching
            # anything — the adapter's full resync then re-reads the source.
            buffer, reset_seen = self._svc._drain_fresh()
            if reset_seen and updated_min:
                raise _FakeHttpError(410, "kit api rejected the watermark")
            self._svc._walk_buffer = buffer
        buffer = self._svc._walk_buffer or []
        if updated_min:
            watermark = _stamp(updated_min)
            buffer = [
                task
                for task in buffer
                if watermark is None
                or (_stamp(task.get("updated")) is None)  # no ordering info: keep
                or (_stamp(task.get("updated")) or _MIN_STAMP) >= watermark
            ]
        max_results = int(self._params.get("maxResults") or 100)
        token = self._params.get("pageToken")
        start = int(token) if token else 0
        page = buffer[start : start + max_results]
        nxt = start + len(page)
        return {
            "items": page,
            **({"nextPageToken": str(nxt)} if nxt < len(buffer) else {}),
        }


class _KitCollection:
    def __init__(self, svc: KitTasksService, kind: str) -> None:
        self._svc = svc
        self._kind = kind

    def list(self, **params: Any) -> _KitRequest:
        return _KitRequest(self._svc, self._kind, params)

    def get(self, **params: Any) -> _KitRequest:
        return _KitRequest(self._svc, self._kind, params)


def build(api: Any) -> GTasksConnector:
    """The registered gtasks connector on a fake service backed by ``api``.

    Built through the PUBLIC constructor with an injected 4-arg service
    factory (no network) — the sweep exercises the real construction
    (API_NAME/API_VERSION check, the allowlist seam): an empty allowlist
    string pins "every list" (parse_list_allowlist('') = no entries), which
    the adapter syncs wholesale — the kit's one list included.
    """
    return GTasksConnector(
        token_loader=lambda: "refresh-token",
        service_factory=lambda token, scopes, api_name, api_version: KitTasksService(api),
        lists_allowlist="",
    )
