"""Google Tasks read connector (``gtasks``, scope ``tasks.readonly``).

Read-only by construction: the only scope is ``tasks.readonly`` (shared with
``SCOPE_BY_CONNECTOR["gtasks"]``) and every call is a ``users.*`` list on the
Tasks API.

Sync design:
- ALL task lists are walked each sync (optionally narrowed by the
  ``BRAIN_GTASKS_LISTS`` allowlist — entries match a list id exactly or a
  list title case-insensitively).
- Incremental via ``updatedMin``: the cursor stores the max ``updated`` seen
  across lists as small JSON (``{"updated_min": ..., "lists": [...]}``). The
  ``lists`` fingerprint invalidates the cursor when the allowlist changes
  (otherwise old tasks of newly-allowed lists would never be fetched).
  ``updatedMin`` is inclusive on Google's side: tasks at the watermark come
  back again and the runner skips them by version — no churn, no misses.
- Cursor invalidation: Tasks rejects a malformed ``updatedMin`` with HTTP
  400 (JSON-RPC ``invalid``); page tokens are server-issued and never
  persisted by this adapter, so a bad token cannot come from our cursor.
  A 400 (or 410, for symmetry with the other Google sources) on an
  incremental call invalidates the watermark: the fetch immediately retries
  as a full sync and returns ``reset=True``. The same status on a full walk
  is a real error and bubbles.
- ``showCompleted/showHidden/showDeleted=true``: deleted tasks map to
  ``deleted=True`` records; completed tasks are normal records with
  ``meta.status="completed"`` + ``meta.completed_at`` (never deleted).
- ``due`` is date-only in practice (Google drops the time): ``occurred_at``
  is that date at local midnight (Asia/Jerusalem in production) and
  ``meta.due_date_only=true``.
- Subtasks carry the parent external id in ``meta.parent_external_id`` and
  the parent title in ``text`` — resolved from the same list's already
  fetched tasks (one call per list, never N+1).
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any, ClassVar
from zoneinfo import ZoneInfo

from brain.connectors import register
from brain.connectors.base import ConnectorError, FetchResult, SourceRecord
from brain.connectors.google.base import (
    SCOPE_BY_CONNECTOR,
    GoogleConnector,
    default_service_factory,
)

logger = logging.getLogger(__name__)

GTASKS_SCOPE = "https://www.googleapis.com/auth/tasks.readonly"

# tasks.list page size (API maximum): fewer round trips on a first full sync.
_MAX_RESULTS = 100

# Cursor fingerprint stored when no allowlist is configured.
_CURSOR_LISTS_ANY = "*"

# Response statuses that invalidate the incremental watermark (Tasks signals
# a malformed updatedMin with 400; 410 is handled for symmetry with the
# other Google sources' cursor semantics).
_CURSOR_INVALID_STATUSES = (400, 410)


def parse_rfc3339(value: str | None) -> datetime | None:
    """Parse a Google timestamp (RFC 3339, e.g. ``2026-03-10T09:30:00.000Z``
    or a bare date); None when absent or unparseable."""
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def record_from_task(
    task: dict[str, Any],
    tz: ZoneInfo,
    *,
    list_id: str = "",
    list_title: str = "",
    parent_title: str | None = None,
) -> SourceRecord:
    """Map one users.tasks resource onto a SourceRecord (pure).

    ``tz`` is the local zone (Asia/Jerusalem in production) used to render
    times in ``text`` and to anchor the date-only due at local midnight.
    For subtasks the text carries the parent title when the caller resolved
    one; when unresolvable the 'Part of' line is omitted (meta always keeps
    the exact parent id).
    """
    task_id = str(task.get("id") or "")
    version = task.get("updated") or task.get("etag") or None
    title = (task.get("title") or "").strip() or "(untitled task)"
    status = task.get("status") or "needsAction"

    meta: dict[str, Any] = {"status": status, "list_id": list_id, "list_title": list_title}
    completed_at = parse_rfc3339(task.get("completed"))
    if completed_at is not None:
        meta["completed_at"] = completed_at.isoformat()
    if task.get("hidden"):
        meta["hidden"] = True
    parent_id = task.get("parent")
    if parent_id:
        meta["parent_external_id"] = str(parent_id)
    links = task.get("links") or []
    if links:
        meta["links"] = [
            {
                "type": link.get("type") or "",
                **({"description": link["description"]} if link.get("description") else {}),
                "url": link.get("link") or "",
            }
            for link in links
        ]

    due = _due_at_local_midnight(task.get("due"), tz)
    if due is not None:
        meta["due_date_only"] = True

    return SourceRecord(
        source="gtasks",
        external_id=task_id,
        version=version,
        title=title,
        text=_render_task_text(
            title,
            list_title=list_title,
            parent_title=parent_title,
            due=due,
            completed_at=completed_at,
            tz=tz,
            notes=task.get("notes") or "",
            links=links,
        ),
        occurred_at=due,
        url=task.get("webViewLink") or task.get("selfLink") or None,
        meta=meta,
        deleted=bool(task.get("deleted")),
    )


def _due_at_local_midnight(due: str | None, tz: ZoneInfo) -> datetime | None:
    """The due as tz-aware local midnight, or None.

    Google drops the time: the calendar date is read in the timestamp's OWN
    offset (``.date()`` on an aware datetime does not convert), so a
    midnight-UTC and a midnight-local due both yield the date the user
    picked — never shifted a day by a zone conversion.
    """
    parsed = parse_rfc3339(due)
    if parsed is None:
        return None
    return datetime.combine(parsed.date(), datetime.min.time(), tzinfo=tz)


def _render_task_text(
    title: str,
    *,
    list_title: str,
    parent_title: str | None,
    due: datetime | None,
    completed_at: datetime | None,
    tz: ZoneInfo,
    notes: str,
    links: list[dict[str, Any]],
) -> str:
    """Deterministic plain text for one task (labels in English; content is
    whatever language the user wrote)."""
    lines = [f"Task: {title}"]
    if list_title:
        lines.append(f"List: {list_title}")
    if parent_title:
        lines.append(f"Part of: {parent_title}")
    if due is not None:
        lines.append(f"Due: {due.date().isoformat()}")
    if completed_at is not None:
        local = completed_at.astimezone(tz)
        zone = getattr(tz, "key", str(tz))
        lines.append(f"Completed: {local.strftime('%Y-%m-%d %H:%M')} ({zone})")
    if notes:
        lines.append("Notes:")
        lines.append(notes)
    if links:
        lines.append("Links:")
        for link in links:
            url = link.get("link") or ""
            description = link.get("description") or ""
            lines.append(f"- {description}: {url}" if description else f"- {url}")
    return "\n".join(lines)


def parse_list_allowlist(raw: str) -> tuple[str, ...]:
    """BRAIN_GTASKS_LISTS value -> sorted, deduped, non-empty entries."""
    entries = {part.strip() for part in (raw or "").split(",") if part.strip()}
    return tuple(sorted(entries))


def select_lists(lists: list[dict[str, Any]], allowlist: tuple[str, ...]) -> list[dict[str, Any]]:
    """The task lists to sync: all of them, or the allowlisted ones (by id
    exactly, or by title case-insensitively)."""
    if not allowlist:
        return list(lists)
    allow_ids = set(allowlist)
    allow_titles = {entry.casefold() for entry in allowlist}
    return [
        lst
        for lst in lists
        if lst.get("id") in allow_ids or str(lst.get("title", "")).casefold() in allow_titles
    ]


def _allowlist_fingerprint(allowlist: tuple[str, ...]) -> tuple[str, ...]:
    """The fingerprint stored in the cursor for an allowlist (wildcard when
    empty). Used by BOTH build_cursor and fetch so the comparison can never
    disagree with what was stored."""
    return tuple(sorted(allowlist)) or (_CURSOR_LISTS_ANY,)


def build_cursor(updated_min: str | None, allowlist: tuple[str, ...]) -> str:
    """Cursor JSON: the incremental watermark + the allowlist fingerprint."""
    return json.dumps(
        {"updated_min": updated_min, "lists": list(_allowlist_fingerprint(allowlist))},
        sort_keys=True,
    )


def parse_cursor(cursor: str | None) -> tuple[str | None, tuple[str, ...] | None]:
    """(updated_min, fingerprint) from a cursor; (None, None) when absent or
    unreadable — a corrupt cursor means "no usable cursor" → full resync."""
    if not cursor:
        return None, None
    try:
        data = json.loads(cursor)
    except ValueError:
        return None, None
    if not isinstance(data, dict) or "updated_min" not in data:
        return None, None
    updated_min = data["updated_min"]
    lists = data.get("lists")
    if updated_min is not None and not isinstance(updated_min, str):
        return None, None
    if not isinstance(lists, list) or not all(isinstance(entry, str) for entry in lists):
        return None, None
    return updated_min, tuple(sorted(lists))


def _stamp_order(value: str | None) -> datetime:
    """Sort key for RFC 3339 stamps: parsed time, oldest first; missing or
    unparseable stamps sort first (they carry no watermark)."""
    parsed = parse_rfc3339(value)
    if parsed is None:
        return datetime.min.replace(tzinfo=UTC)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _local_tz() -> ZoneInfo:
    from brain.config import get_settings  # noqa: PLC0415 (lazy; config never imports connectors)

    return ZoneInfo(get_settings().timezone)


@register
class GTasksConnector(GoogleConnector):
    """Google Tasks: every list, incremental via the updatedMin watermark."""

    name: ClassVar[str] = "gtasks"
    # Single source for the scope string: the base's SCOPE_BY_CONNECTOR.
    SCOPES: ClassVar[tuple[str, ...]] = SCOPE_BY_CONNECTOR["gtasks"]

    def __init__(
        self,
        *,
        token_loader: Callable[[], str],
        service_factory: Callable[[str, list[str]], Any] = default_service_factory,
        user_id: str = "me",
        lists_allowlist: str | None = None,
    ) -> None:
        super().__init__(
            token_loader=token_loader,
            service_factory=service_factory,
            user_id=user_id,
        )
        # None -> read BRAIN_GTASKS_LISTS lazily (no settings access at build
        # time); a string pins the allowlist (tests).
        self._lists_allowlist = lists_allowlist

    # -- Connector API --------------------------------------------------------

    def fetch(self, cursor: str | None, *, limit: int) -> FetchResult:
        allowlist = self._allowlist()
        prev_updated, prev_lists = parse_cursor(cursor)
        updated_min = prev_updated
        reset = False
        if cursor is not None and prev_updated is None and prev_lists is None:
            updated_min, reset = None, True  # corrupt cursor -> full resync
        elif prev_lists is not None and prev_lists != _allowlist_fingerprint(allowlist):
            updated_min, reset = None, True  # allowlist changed -> full resync

        selected = select_lists(self._fetch_lists(), allowlist)
        try:
            records, last_emitted = self._collect(selected, updated_min, limit)
        except ConnectorError as exc:
            if updated_min is None or exc.status not in _CURSOR_INVALID_STATUSES:
                raise  # a real error, or one a full resync cannot fix
            # The source rejected the watermark: full resync, tell the runner.
            updated_min, reset = None, True
            records, last_emitted = self._collect(selected, None, limit)

        # The cursor is the LAST EMITTED record's 'updated' (sorted ascending,
        # so everything older is emitted; updatedMin is inclusive, so the next
        # fetch re-delivers at most the last tie group — skipped by version).
        # No records -> keep the previous watermark (or none on a first sync).
        next_updated = last_emitted
        if next_updated is None and not reset:
            next_updated = prev_updated
        return FetchResult(
            records=records, next_cursor=build_cursor(next_updated, allowlist), reset=reset
        )

    # -- internals ------------------------------------------------------------

    def _allowlist(self) -> tuple[str, ...]:
        if self._lists_allowlist is not None:
            return parse_list_allowlist(self._lists_allowlist)
        from brain.config import get_settings  # noqa: PLC0415 (lazy)

        return parse_list_allowlist(get_settings().gtasks_lists)

    def _fetch_lists(self) -> list[dict[str, Any]]:
        """All task lists, following pagination (base error translation)."""
        lists: list[dict[str, Any]] = []
        for page in self.iter_pages(
            lambda token: (
                self._service().tasklists().list(**({"pageToken": token} if token else {}))
            )
        ):
            lists.extend(page.get("items") or [])
        return lists

    def _fetch_list_tasks(self, list_id: str, updated_min: str | None) -> list[dict[str, Any]]:
        """All task resources of one list (all pages), honoring the watermark."""

        def make_request(token: str | None) -> Any:
            params: dict[str, Any] = {
                "showCompleted": True,
                "showHidden": True,
                "showDeleted": True,
                "maxResults": _MAX_RESULTS,
            }
            if updated_min:
                params["updatedMin"] = updated_min
            if token:
                params["pageToken"] = token
            return self._service().tasks().list(tasklist=list_id, **params)

        return [
            task for page in self.iter_pages(make_request) for task in (page.get("items") or [])
        ]

    def _collect(
        self, selected: list[dict[str, Any]], updated_min: str | None, limit: int
    ) -> tuple[list[SourceRecord], str | None]:
        """Fetch the selected lists, emit up to ``limit`` records, return them
        with the cursor watermark (the last emitted record's ``updated``).

        Livelock-free truncation: tasks from ALL selected lists are sorted by
        (updated, list_id, id) ASCENDING and emitted oldest-first, so the
        resume watermark (updatedMin is inclusive) always moves forward no
        matter what order the API returned. A tie group of equal ``updated``
        is extended past ``limit`` — otherwise a group larger than the limit
        would resume at its own watermark forever. Records AT the incoming
        watermark are known re-deliveries: emitted but not counted toward
        ``limit`` (a tie group at the watermark followed by newer tasks would
        otherwise re-fill the limit every run and the newer tasks would never
        be reached). Re-delivered unchanged records are skipped by the runner
        (idempotent by external_id+version).
        """
        tz = _local_tz()
        entries: list[tuple[tuple[datetime, str, str, str], dict[str, Any], str, str]] = []
        titles_by_list: dict[str, dict[str, str]] = {}
        for lst in selected:
            list_id = str(lst.get("id") or "")
            list_title = str(lst.get("title") or "")
            try:
                raw_tasks = self._fetch_list_tasks(list_id, updated_min)
            except ConnectorError as exc:
                if exc.status != 404:
                    raise
                # A list that vanished takes its tasks with it; skipping keeps
                # the rest of the sync alive (logged, never silent in ops).
                logger.warning("gtasks: task list %r unavailable, skipped: %s", list_id, exc)
                continue
            titles_by_list[list_id] = {
                str(t.get("id") or ""): str(t.get("title") or "") for t in raw_tasks
            }
            for task in raw_tasks:
                updated = task.get("updated") or None
                key = (_stamp_order(updated), updated or "", list_id, str(task.get("id") or ""))
                entries.append((key, task, list_id, list_title))
        entries.sort(key=lambda entry: entry[0])

        # One tasks().get per DISTINCT parent missing from the batch, shared
        # by every record in this fetch.
        missing_parents: dict[tuple[str, str], str | None] = {}
        records: list[SourceRecord] = []
        last_emitted: str | None = None
        counted = 0
        for _key, task, list_id, list_title in entries:
            updated = task.get("updated") or None
            # A record AT the incoming watermark was necessarily emitted by
            # the previous batch (emission is oldest-first and the cursor is
            # the last emitted stamp): it is a known re-delivery — emitted for
            # the runner to skip by version, but never counted toward limit.
            # Without this, a tie group at the watermark followed by newer
            # tasks would re-fill the whole limit every run and the newer
            # tasks would never be reached (livelock). It is NOT skipped
            # entirely: after a failed batch the cursor did not advance, so
            # the watermark records may never have been applied at all.
            is_redelivery = (
                updated is not None and updated_min is not None and updated == updated_min
            )
            if (
                not is_redelivery
                and counted >= limit
                and (updated or "") != (last_emitted or "")
            ):
                break  # limit reached, past the last emitted tie group
            parent_id = task.get("parent")
            parent_title = None
            if parent_id:
                parent_title = self._resolve_parent_title(
                    list_id,
                    str(parent_id),
                    titles_by_list.get(list_id, {}),
                    missing_parents,
                )
            records.append(
                record_from_task(
                    task,
                    tz,
                    list_id=list_id,
                    list_title=list_title,
                    parent_title=parent_title,
                )
            )
            last_emitted = updated
            if not is_redelivery:
                counted += 1
        return records, last_emitted

    def _resolve_parent_title(
        self,
        list_id: str,
        parent_id: str,
        batch_titles: dict[str, str],
        memo: dict[tuple[str, str], str | None],
    ) -> str | None:
        """The parent title for a subtask's 'Part of' line: from the same
        batch when the parent is in it, else ONE ``tasks().get`` per distinct
        missing parent (memoised per fetch). Unresolvable (deleted parent) ->
        None: the line is omitted; meta keeps the exact parent id."""
        if parent_id in batch_titles:
            return batch_titles[parent_id] or None
        key = (list_id, parent_id)
        if key not in memo:
            try:
                raw = self._execute(self._service().tasks().get(tasklist=list_id, task=parent_id))
            except ConnectorError as exc:
                if exc.status != 404:
                    raise
                raw = {}  # parent deleted: render without the 'Part of' line
            memo[key] = str(raw.get("title") or "") or None
        return memo[key]
