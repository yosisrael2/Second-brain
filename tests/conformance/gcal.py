"""gcal's conformance donation (the sweep's second customer).

``build(api)`` -> a FRESH :class:`GCalConnector` whose Calendar service is
backed by the kit's :class:`~brain.connectors.conformance.FakeSourceAPI`.
The mapping is the adapter's real shape:

- ONE allowlisted calendar (``primary`` — pinned by the sweep's env
  fixture); ``events().list`` serves the kit dataset as event resources
  (``id``/``summary``/``description``/``start``/``end``/``updated``/
  ``status``), the whole kit page in one API page (the adapter's real
  ``maxResults`` is 250 — larger than any kit dataset, so walks complete in
  one page, exactly like a small real calendar).
- The kit API is drained ONCE per service instance and buffered (a walk
  that retries after a 410 re-reads the buffer; a kit ``next_fetch_only``
  lands on the first drain).
- ``syncToken`` semantics: the first complete walk issues ``kit-sync-1``;
  incremental calls (``syncToken`` present) are served the CURRENT dataset
  with the same held token — the runner's version skip absorbs the
  re-delivery, which is exactly how a quiet real calendar behaves.
- A kit fetch that reports ``reset=True`` (the RESET check's
  ``next_fetch_only``) invalidates the sync token: the incremental call
  raises the Google-shaped 410, which the base translation turns into the
  adapter's ``_SyncTokenExpired`` path (full re-list, ``reset=True``).
- ``mark_deleted``'s unparseable ``updated`` is passed through verbatim:
  the adapter versions on the raw string and never parses it.

The connector is built through its public constructor with an injected
service factory (no network), exactly like the adapter's own unit tests.
"""

from __future__ import annotations

from typing import Any

from brain.connectors.google.calendar import GCalConnector

_CALENDAR_ID = "primary"
_SYNC_TOKEN = "kit-sync-1"


class _FakeHttpError(Exception):
    """Duck-typed googleapiclient.errors.HttpError: carries .resp.status."""

    def __init__(self, status: int, message: str = "") -> None:
        super().__init__(message or f"http {status}")
        self.resp = type("Resp", (), {"status": status})()


def _event_from(record: Any) -> dict[str, Any]:
    """Kit record -> Calendar API event resource (the adapter maps it back)."""
    return {
        "id": record.external_id,
        "summary": record.title,
        "description": record.text,
        "start": {"dateTime": "2026-10-01T09:00:00Z"},
        "end": {"dateTime": "2026-10-01T10:00:00Z"},
        "updated": record.version,
        **({"status": "cancelled"} if record.deleted else {"status": "confirmed"}),
    }


class KitCalendarService:
    """events()-shaped fake backed by the kit's FakeSourceAPI.

    One WALK (an ``events().list`` sequence starting without ``pageToken``)
    drains the kit api fresh; a 410 on an incremental walk raises before
    anything is cached, so the adapter's full re-list re-reads the source.
    """

    def __init__(self, api: Any) -> None:
        self._api = api
        self._walk_buffer: list[dict[str, Any]] | None = None

    def events(self) -> _KitEventsApi:
        return _KitEventsApi(self)

    def _drain_fresh(self) -> tuple[list[dict[str, Any]], bool]:
        """Drain the kit api completely; return (event resources, reset seen)."""
        buffer: list[dict[str, Any]] = []
        cursor: str | None = None
        reset_seen = False
        while True:
            result = self._api.fetch(cursor, limit=self._api.page_size)
            reset_seen = reset_seen or result.reset
            buffer.extend(_event_from(record) for record in result.records)
            cursor = result.next_cursor
            if cursor is None:
                break
        return buffer, reset_seen


class _KitEventsApi:
    def __init__(self, svc: KitCalendarService) -> None:
        self._svc = svc

    def list(self, **params: Any) -> _KitRequest:
        return _KitRequest(self._svc, params)


class _KitRequest:
    def __init__(self, svc: KitCalendarService, params: dict[str, Any]) -> None:
        self._svc = svc
        self._params = params

    def execute(self) -> dict[str, Any]:
        if self._params.get("pageToken") is None:
            # A new walk: drain the kit api fresh. A reset flagged by the kit
            # on an INCREMENTAL walk (syncToken present) is the source
            # invalidating the sync cursor (kit RESET check): raise the
            # Google-shaped 410 BEFORE caching anything — the adapter's full
            # re-list then re-reads the source.
            buffer, reset_seen = self._svc._drain_fresh()
            if reset_seen and self._params.get("syncToken"):
                raise _FakeHttpError(410)
            self._svc._walk_buffer = buffer
        buffer = self._svc._walk_buffer or []
        return {
            "items": list(buffer),
            "nextSyncToken": _SYNC_TOKEN,  # a complete walk holds the token
        }


def build(api: Any) -> GCalConnector:
    """The registered gcal connector on a fake service backed by ``api``."""
    return GCalConnector(
        token_loader=lambda: "refresh-token",
        service_factory=lambda token, scopes, api_name, api_version: KitCalendarService(api),
    )
