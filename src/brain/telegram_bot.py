"""Telegram bot: mobile capture channel over the in-process service layer.

``brain telegram`` long-polls Telegram (getUpdates, no webhook) and reuses
the SAME services as the API: paste-aware capture (:mod:`brain.services.paste`
via :meth:`IngestService.capture_parsed`), search, ask and inbox review.

Security: only user ids in ``TELEGRAM_ALLOWED_USER_IDS`` are processed —
anyone else gets no reply plus one INFO log line with their id. The token and
message bodies are never logged.

Interaction model:
- every capture is a card (kind emoji, extracted fields, type/importance/
  actionability/status, HTML-escaped) with inline buttons: File / Type /
  Delete / Undo. Buttons act on ids parked in a short-token registry and
  stay valid for 10 minutes; stale or unknown callbacks are answered
  gracefully.
- a multi-message WhatsApp paste is NOT saved immediately: the bot asks
  "Save as 1 item / Save separately" first (the paste text is parked in
  memory, keyed by a short token, expiring after 10 minutes).
- text ending in "?" (no slash) routes to /ask; the adapter sends the TYPING
  chat action while waiting (refresh every 4s); answers carry the model name
  and one "Open" button per cited item.
- forwarded messages record ``meta.sender`` / ``meta.forwarded`` and use the
  forward date as ``occurred_at``; source stays the parsed kind and
  ``meta.channel`` stays "telegram".
- /review walks the inbox one card at a time (File / Type / Delete / Next);
  /recent lists the last 10 items.
- .ics documents are captured as calendar items; other files, photos and
  voice get a "not supported yet" reply.

Ops integration (cockpit): :func:`heartbeat` and :func:`record_event` call
``brain.ops`` when that package exists (feature/cockpit) and degrade to no-ops
otherwise; neither ever raises. ``/status`` uses the same health service when
available and replies "cockpit not installed yet" when it is not.

Exit codes (launchd service contract): token unset → 0 ("disabled"); another
bot instance already running (machine-wide flock, per bot id) or 409 conflict
persisting past the retry window, or invalid token (Unauthorized) → 78
(EX_CONFIG, no restart loop); transient failures → 1 (the service restarts).
"""

from __future__ import annotations

import asyncio
import fcntl
import html
import importlib
import logging
import os
import re
import secrets
import sys
import threading
import time
import uuid as uuid_mod
from collections.abc import Callable, Generator, Sequence
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import wraps
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from brain import i18n
from brain.config import Settings, unwrap_secret
from brain.schemas import ItemUpdate
from brain.services.ask import AskResult
from brain.services.ingest import IngestService
from brain.services.memory import MemoryError
from brain.services.paste import parse as parse_paste
from brain.services.proposals import (
    CB_PROPOSED_DROP,
    CB_PROPOSED_FIX,
    CB_PROPOSED_KEEP,
    CB_PROPOSED_KEEP_ALL,
    FixError,
    apply_drop,
    apply_fix,
    apply_keep,
    due_proposed_children,
    mark_pinged,
    ping_line,
    proposal_keyboard,
    proposed_children_of,
    render_source_card,
)
from brain.services.relations_view import (
    CB_RECON_DISCARD,
    CB_REL_CONFIRM,
    CB_REL_KEEP_BOTH,
    CB_REL_RESTORE,
    CB_REL_UNDO,
    conflict_notice,
    correction_notice,
    details_notice,
    recon_relation_of,
    relations_keyboard,
    replaced_by_notice,
    strike,
)
from brain.services.schedule_spec import describe as describe_schedule
from brain.services.scheduled_tasks import (
    Claim,
    RunOutcome,
    ScheduledTaskNotFound,
    StoreFactory,
    default_store,
    execute_claim,
)

logger = logging.getLogger(__name__)

TELEGRAM_REPLY_MAX_CHARS = 4000  # Telegram messages cap at 4096
PENDING_TTL_S = 600  # inline buttons / WhatsApp choice stay valid 10 minutes
TYPING_REFRESH_S = 4

KIND_EMOJI = {
    "email": "📧",
    "whatsapp": "💬",
    "calendar": "📅",
    "note": "📝",
    "task": "✔️",
    "idea": "💡",
    "decision": "⚖️",
    "reference": "🔗",
}
KIND_NAME = {
    "email": "Email",
    "whatsapp": "WhatsApp",
    "calendar": "Event",
    "event": "Event",
    "note": "Note",
    "task": "Task",
    "idea": "Idea",
    "decision": "Decision",
    "reference": "Reference",
}
ACTIONABILITY_LABEL = {"now": "⏰ Now", "later": "🕐 Later"}
TYPE_EMOJI = {
    "task": "✔️",
    "decision": "⚖️",
    "note": "📝",
    "event": "📅",
    "reference": "🔗",
    "idea": "💡",
    "reminder": "🔔",  # the proposal card's legend uses the same two
    "fact": "ℹ️",
}

# Compact callback-data prefixes (kept < 64 bytes; ids live in the registry).
CB_UNDO = "u"  # u:<token>              -> delete the items created by that message
CB_FILE = "f"  # f:<token>              -> status inbox -> filed
CB_DELETE = "d"  # d:<token>            -> delete items
CB_TYPE = "t"  # t:<token>              -> type submenu
CB_BACK = "b"  # b:<token>              -> back from the type menu
CB_TYPE_APPLY = "ty"  # ty:<token>:<type>
CB_NEXT = "n"  # n:<offset>            -> next inbox card
CB_OPEN = "o"  # o:<token>:<index>     -> show a cited item card
CB_WA_JOIN = "w1"  # w1:<token>        -> save WhatsApp thread as one item
CB_WA_SPLIT = "wm"  # wm:<token>      -> save one item per message
CB_MEM_DEL = "m"  # m:<token>:<index> -> remove one memory fact (soft delete)
CB_SCHED_RUN = "sr"  # sr:<token>:<index>  -> queue a scheduled task's run-now
CB_SCHED_TOGGLE = "st"  # st:<token>:<index> -> pause / resume a scheduled task
SCHEDULES_LIST_MAX = 20  # /schedules shows (and buttons) at most this many tasks

BOT_COMMANDS = (
    ("ask", "Ask your memory a question"),
    ("search", "Semantic search"),
    ("review", "Review inbox items"),
    ("recent", "Show the last 10 items"),
    ("profile", "Your kids and remembered facts"),
    ("remember", "Remember a fact: /remember <text>"),
    ("schedules", "Scheduled tasks: run now, pause, resume"),
    ("status", "Component health"),
    ("language", "Bot language: /language he | en"),
    ("help", "How to use the bot"),
)



def bot_commands(lang: str | None = i18n.DEFAULT) -> list[tuple[str, str]]:
    """Telegram's command menu with descriptions in ``lang``."""
    return [(name, i18n.t(lang, f"cmd_{name}")) for name, _ in BOT_COMMANDS]


def _kind_name(kind: str, lang: str) -> str:
    """'Note' / 'פתק' for an item source or type (raw value when unknown)."""
    return i18n.label(lang, f"kind_{kind}", KIND_NAME.get(kind, kind))


# Kinds accepted by ops.record_event (cockpit contract).
EVENT_CAPTURE = "capture"
EVENT_ASK = "ask"
EVENT_SEARCH = "search"
EVENT_REVIEW = "review"
EVENT_CALLBACK = "callback"
EVENT_DOCUMENT = "document"
EVENT_REJECTED_USER = "rejected_user"
EVENT_MEMORY = "memory"
EVENT_ERROR = "error"
EVENT_SCHEDULE = "schedule"

# Exit codes (launchd contract; see module docstring).
EXIT_DISABLED = 0
EXIT_TRANSIENT = 1
EXIT_CONFIG = 78  # EX_CONFIG: permanent configuration error, no restart loop


# --- ops shim (never raises; no-ops without feature/cockpit) -----------------


def heartbeat(component: str, detail: dict[str, Any] | None = None) -> None:
    """Cockpit heartbeat; no-op when brain.ops is not installed."""
    try:
        ops = importlib.import_module("brain.ops")  # optional dependency

        ops.heartbeat(component, detail)
    except ImportError:
        return
    except Exception:  # noqa: BLE001  (ops must never break the bot)
        logger.debug("telegram: ops.heartbeat raised", exc_info=True)


def record_event(
    component: str,
    kind: str,
    ok: bool,
    latency_ms: int | None = None,
    detail: dict[str, Any] | None = None,
) -> None:
    """Cockpit event; no-op when brain.ops is not installed."""
    try:
        ops = importlib.import_module("brain.ops")

        ops.record_event(component, kind, ok, latency_ms=latency_ms, detail=detail)
    except ImportError:
        return
    except Exception:  # noqa: BLE001
        logger.debug("telegram: ops.record_event raised", exc_info=True)


def _ops_status_entries() -> list[tuple[str, str, str]] | None:
    """(status, component, summary) per component from the cockpit health
    service — the language-neutral form of :func:`_ops_status_lines` (the
    bot renders it in the user's language). None when unavailable."""
    try:
        health_mod = importlib.import_module("brain.ops.health")
        run = getattr(health_mod, "run_health_checks", None)
        if run is None:
            return None
        health = run()
        entries = [
            (
                str(entry.get("status") or "not probed"),
                str(name),
                str(entry.get("summary") or ""),
            )
            for name, entry in health.get("components", {}).items()
        ]
    except Exception:  # noqa: BLE001  (a broken cockpit must not crash /status)
        logger.debug("telegram: cockpit status failed", exc_info=True)
        return None
    return entries or None


def _ops_status_lines() -> list[str] | None:
    """Per-component status lines from the cockpit health service, or None.

    Prefers ``run_health_checks`` (full ok/degraded/down/disabled ->
    🟢🟡🔴⚪); falls back to the ``component_status()`` tuple contract, then
    plain strings. Anything unusable counts as "not installed".
    """
    try:
        ops = importlib.import_module("brain.ops")
    except ImportError:
        return None
    icon = {"ok": "🟢", "degraded": "🟡", "down": "🔴", "disabled": "⚪", "not probed": "⚪"}
    try:
        try:
            health_mod = importlib.import_module("brain.ops.health")
        except ImportError:
            health_mod = None
        run = getattr(health_mod, "run_health_checks", None)
        if run is not None:
            health = run()
            lines = [
                f"{icon.get(entry.get('status'), '⚪')} {_esc(name)}: "
                f"{_esc(entry.get('summary') or entry.get('status') or '?')}"
                for name, entry in health.get("components", {}).items()
            ]
            if lines:
                return lines
        fn = getattr(ops, "component_status", None)
        if fn is None:
            return None
        raw = fn()
    except Exception:  # noqa: BLE001  (a broken cockpit must not crash /status)
        logger.debug("telegram: cockpit status failed", exc_info=True)
        return None
    lines = []
    for entry in raw or []:
        if isinstance(entry, str):
            lines.append(entry)
        elif isinstance(entry, (tuple, list)) and len(entry) == 3:
            name, ok, summary = entry
            lines.append(f"{'🟢' if ok else '🔴'} {_esc(name)}: {_esc(summary)}")
    return lines or None


# --- core data model ----------------------------------------------------------


@dataclass(frozen=True)
class Reply:
    """What the adapter should send: HTML text + inline keyboard rows.

    ``keyboard`` rows are tuples of ``(label, callback_data)``; the adapter
    turns them into a PTB InlineKeyboardMarkup. Empty keyboard = plain text.
    ``edit=True`` asks the adapter to EDIT the message a callback came from
    (in-place card updates) instead of sending a new one. ``toast`` is a
    callback's short feedback ("expired", "already handled", "kept"): the
    adapter shows it on the tapped button (answerCallbackQuery), and an
    empty ``text`` then sends nothing to the chat.
    """

    text: str
    keyboard: list[list[tuple[str, str]]] | None = None
    edit: bool = False
    toast: str | None = None


@dataclass(frozen=True)
class Pending:
    """State parked behind a short callback token (10-minute TTL)."""

    ids: tuple[uuid_mod.UUID, ...] = ()  # target items for the action
    text: str | None = None  # WhatsApp paste waiting for the join/split choice
    # PR C proposal cards: what the token refers to + the fix-flow step
    kind: str = "capture"  # "capture" | "proposal" | "fix"
    step: str | None = None  # fix flow: "title" | "date"


class PendingRegistry:
    """Short-token -> state map so callback data stays under 64 bytes."""

    def __init__(self, ttl_s: int = PENDING_TTL_S) -> None:
        self._ttl_s = ttl_s
        self._entries: dict[str, tuple[Pending, float]] = {}  # token -> (state, expires_at)

    def put(self, pending: Pending) -> str:
        self._prune()
        token = secrets.token_hex(4)  # 8 chars; collisions are irrelevant here
        self._entries[token] = (pending, time.monotonic() + self._ttl_s)
        return token

    def get(self, token: str) -> Pending | None:
        self._prune()
        entry = self._entries.get(token)
        return entry[0] if entry else None

    def _prune(self) -> None:
        now = time.monotonic()
        for token in [t for t, (_, exp) in self._entries.items() if exp <= now]:
            del self._entries[token]


@dataclass(frozen=True)
class BotServices:
    """The per-update service bundle (fresh DB session per update)."""

    ingest: IngestService
    items: Any  # ItemsService
    search: Any  # SearchService
    ask: Any  # AskService
    memory: Any = None  # MemoryService (PR 0 core; None -> feature missing)
    attachments: Any = None  # AttachmentService (F1; None -> feature missing)


ServicesFactory = Callable[[], AbstractContextManager["BotServices"]]


def _live_services_factory() -> ServicesFactory:
    """Build the real per-update service factory from the shared AI clients.

    The AI clients are the same app-lifetime singletons the API uses
    (brain.api.deps); only the DB session is per update.
    """
    from brain.api import deps  # noqa: PLC0415  (deferred: keeps core testable)

    @contextmanager
    def factory() -> Generator[BotServices, None, None]:
        from brain.db.session import SessionLocal  # noqa: PLC0415
        from brain.repositories.items import ItemsRepository  # noqa: PLC0415
        from brain.repositories.memory import MemoryRepository  # noqa: PLC0415
        from brain.services.items import ItemsService  # noqa: PLC0415
        from brain.services.memory import MemoryService  # noqa: PLC0415
        from brain.services.search import SearchService  # noqa: PLC0415

        session = SessionLocal()
        try:
            repo = ItemsRepository(session)
            search = SearchService(
                repo,
                deps.get_embedder(),
                deps.get_vector_store(),
                deps.get_reranker(),
            )
            from brain.repositories.memory import MemoryRepository  # noqa: PLC0415
            from brain.services.memory import MemoryService  # noqa: PLC0415

            memory = MemoryService(MemoryRepository(session))
            yield BotServices(
                ingest=IngestService(repo, deps.get_classifier(), deps.get_item_indexer()),
                items=ItemsService(repo, deps.get_item_indexer()),
                search=search,
                ask=deps.build_ask_service(
                    search,
                    deps.get_llm_client(),
                    memory_block=memory.compact_block,
                    memory=memory,
                    items_repo=repo,
                ),
                memory=memory,
            )
        finally:
            session.close()

    return factory


def _esc(value: Any) -> str:
    """HTML-escape anything user- or parser-derived before it lands in a card."""
    return html.escape(str(value), quote=False)


def _flat_line(text: str, limit: int) -> str:
    """Collapse to one line, cut at ``limit`` with an ellipsis when cut."""
    flat = " ".join(str(text).split())
    if len(flat) > limit:
        flat = flat[:limit].rstrip() + "…"
    return flat


def _one_line(text: str, limit: int) -> str:
    """_flat_line, HTML-escaped."""
    return _esc(_flat_line(text, limit))


_SUPERSCRIPT = str.maketrans("0123456789", "⁰¹²³⁴⁵⁶⁷⁸⁹")


def _ref(n: int) -> str:
    """Numbered citation ref: 1 -> ¹ (fallback [n] beyond the superscript set)."""
    return str(n).translate(_SUPERSCRIPT) if 0 <= n <= 20 else f"[{n}]"


def _stars(importance: Any) -> str:
    """Importance as stars ('⭐⭐⭐'); empty string when unset."""
    try:
        n = int(importance) if importance is not None else 0
    except (TypeError, ValueError):
        return ""
    return "⭐" * max(0, min(n, 5))


def _locale_of(session: Any, lang: str | None = None) -> tuple[str, dict[str, str]]:
    """(language, display names) from the memory layer; English on any error
    so a memory problem never blocks a card. A ``lang`` already read for
    this update is reused (one language read per update)."""
    try:
        from brain.repositories.memory import MemoryRepository  # noqa: PLC0415
        from brain.services.memory import MemoryService  # noqa: PLC0415

        memory = MemoryService(MemoryRepository(session))
        if lang is None:
            lang = memory.user_language()
        return lang, memory.display_names(lang)
    except Exception:  # noqa: BLE001
        logger.warning("telegram: could not read the language from memory", exc_info=True)
        return i18n.DEFAULT, {}


def _speaks(method: Callable[..., Any]) -> Callable[..., Any]:
    """Run ``method`` inside the bot's per-update language scope: the user's
    language is read from memory ONCE (on first use) and every string the
    update renders reuses it. Re-entrant; per thread (the ask and scheduled
    runs live in worker threads)."""

    @wraps(method)
    def wrapper(self: TelegramBot, *args: Any, **kwargs: Any) -> Any:
        local = self._lang_local
        if getattr(local, "active", False):
            return method(self, *args, **kwargs)
        local.active, local.value = True, None
        try:
            return method(self, *args, **kwargs)
        finally:
            local.active, local.value = False, None

    return wrapper


class TelegramBot:
    """Update-processing core: allowlist, commands, capture; PTB-agnostic.

    Handlers are synchronous and return :class:`Reply` — the polling adapter
    (:func:`poll_loop`) stays thin (typing indicator, keyboards, file
    download) and the whole core is testable without network or
    python-telegram-bot.
    """

    def __init__(
        self,
        *,
        services_factory: ServicesFactory,
        allowed_ids: frozenset[int],
        timezone: ZoneInfo,
        registry: PendingRegistry | None = None,
        schedule_store: StoreFactory | None = None,
    ) -> None:
        self._services_factory = services_factory
        self._allowed_ids = allowed_ids
        self._timezone = timezone
        self.registry = registry or PendingRegistry()
        # PR C proposal cards: user -> fix-flow token; outbound card queue;
        # 48h-ping throttle
        self._fix_flow: dict[int, str] = {}
        self._outbound: list[tuple[int, Reply]] = []
        self._last_ping_check = 0.0
        # A2 enrichment worker: last-run marker (monotonic) + the in-flight
        # pass task — kept referenced so it is not garbage-collected (a
        # shutdown path can await/cancel it)
        self._enrich_last_run = 0.0
        self._enrich_task: Any = None
        # Scheduled tasks: the store (fresh session per call), the tasks
        # whose worker thread is still running (never claimed again until it
        # ends, even after a timeout), and the in-flight asyncio tasks (kept
        # referenced so they are not garbage-collected).
        self._schedule_store: StoreFactory = schedule_store or default_store
        self._sched_inflight: set[uuid_mod.UUID] = set()
        self._sched_tasks: set[Any] = set()
        # per-update language cache (see _speaks / _language)
        self._lang_local = threading.local()

    def is_allowed(self, user_id: int | None) -> bool:
        return user_id is not None and user_id in self._allowed_ids

    @property
    def allowed_count(self) -> int:
        """Allowlist size (for the ops heartbeat; no ids, no names)."""
        return len(self._allowed_ids)

    # -- routing ---------------------------------------------------------------

    def is_question(self, text: str) -> bool:
        """Plain text ending in '?' is an ask, even without the /ask command."""
        stripped = text.strip()
        return bool(stripped) and not stripped.startswith("/") and stripped.endswith("?")

    @_speaks
    def route(self, text: str, user_id: int, *, forward: dict[str, Any] | None = None) -> Reply:
        """Dispatch one allowed text message; returns the reply."""
        stripped = text.strip()
        if user_id in self._fix_flow:  # PR C: an active Fix flow captures the input
            return self._fix_input(user_id, stripped)
        if not stripped.startswith("/"):
            if self.is_question(stripped):
                return self.ask(stripped)
            return self.capture(stripped, forward=forward)
        command, _, argument = stripped.partition(" ")
        argument = argument.strip()
        if command == "/start":
            return self.start()
        if command == "/help":
            return self.help_text()
        if command == "/status":
            return self.status()
        if command == "/search":
            return self.search(argument)
        if command == "/ask":
            return self.ask(argument)
        if command == "/language":
            return self.set_language(argument)
        if command == "/review":
            return self.review()
        if command == "/recent":
            return self.recent()
        if command == "/profile":
            return self.profile()
        if command == "/remember":
            return self.remember(argument)
        if command == "/schedules":
            return self.schedules()
        return self.capture(stripped, forward=forward)

    # -- capture ----------------------------------------------------------------

    def capture(self, text: str, *, forward: dict[str, Any] | None = None) -> Reply:
        """Paste-aware capture; WhatsApp threads ask join-vs-split first."""
        parsed = parse_paste(text, tz=self._timezone)
        if parsed.kind == "whatsapp" and self._message_count(parsed) > 1:
            token = self.registry.put(Pending(text=text))
            count = self._message_count(parsed)
            lang = self._language()
            return Reply(
                text=i18n.t(lang, "wa_thread", n=count),
                keyboard=[
                    [(i18n.t(lang, "btn_wa_join"), f"{CB_WA_JOIN}:{token}")],
                    [(i18n.t(lang, "btn_wa_split"), f"{CB_WA_SPLIT}:{token}")],
                ],
            )
        return self._save_parsed(parsed, forward=forward)

    @_speaks
    def capture_document(self, file_name: str, text: str) -> Reply:
        """Capture an .ics document's contents as a calendar item."""
        parsed = parse_paste(text or "", kind_hint="calendar", tz=self._timezone)
        reply = self._save_parsed(parsed)
        record_event("telegram", EVENT_DOCUMENT, ok=True, detail={"chars": len(text or "")})
        name = i18n.iso(_esc(file_name), self._language())
        line = i18n.t(self._language(), "doc_file", name=name)
        return Reply(text=f"📎 {reply.text}\n{line}", keyboard=reply.keyboard)

    def capture_attachment_parent(self, text: str) -> Any:
        """Capture the caption as an attachment's parent item (item 45).

        The caption is the parent's text; on an exact-duplicate caption the
        EXISTING item is the parent (ingest dedupe returns its snapshot).
        """
        parsed = parse_paste(text, tz=self._timezone)
        with self._services_factory() as services:
            items = services.ingest.capture_parsed(parsed, extra_meta={"channel": "telegram"})
        return items[0]

    def read_attachment(self, attachment_id: Any) -> Any:
        """Run one attachment read through the (shared) attachment service."""
        with self._services_factory() as services:
            if services.attachments is None:
                return "unsupported"
            return services.attachments.process(attachment_id)

    @_speaks
    def attachment_summary(self, attachment_id: Any, status: str) -> Reply:
        """Short follow-up for a finished (or failed) attachment read."""
        lang = self._language()
        with self._services_factory() as services:
            att = services.attachments.get(attachment_id) if services.attachments else None
        if att is None:
            return Reply(text=i18n.t(lang, "att_saved"))
        name = i18n.iso(_esc(att.filename), lang)
        if status == "done":
            with self._services_factory() as services:
                own = services.attachments.own_item(attachment_id)
            description = (own.meta or {}).get("description", "") if own else ""
            if description:
                return Reply(text=f"📎 {name}\n{description}")
            head = (att.text or "").strip().splitlines()
            preview = head[0][:160] if head else ""
            return Reply(text=f"📎 {name}\n{preview}" if preview else f"📎 {name}")
        if status == "unsupported":
            return Reply(text=i18n.t(lang, "att_unreadable", name=name))
        if status == "too_large":
            return Reply(text=i18n.t(lang, "att_too_large", name=name))
        # failed or queued
        return Reply(text=i18n.t(lang, "att_failed", name=name))

    def _save_parsed(self, parsed: Any, *, forward: dict[str, Any] | None = None) -> Reply:
        extra_meta: dict[str, Any] = {"channel": "telegram"}
        occurred_at = None
        if forward:
            extra_meta["forwarded"] = True
            if forward.get("sender"):
                extra_meta["sender"] = forward["sender"]
            if forward.get("sender_id") is not None:
                extra_meta["sender_id"] = forward["sender_id"]
            occurred_at = forward.get("date")
        with self._services_factory() as services:
            items = services.ingest.capture_parsed(
                parsed, extra_meta=extra_meta, occurred_at=occurred_at
            )
        record_event(
            "telegram",
            EVENT_CAPTURE,
            ok=True,
            detail={
                "item_ids": [str(item.id) for item in items],
                "chars": sum(len(chunk.raw_text) for chunk in parsed.chunks),
            },
        )
        return Reply(
            text=self._capture_card(parsed, items),
            keyboard=self._capture_buttons(items),
        )

    def _capture_buttons(self, items: list[Any]) -> list[list[tuple[str, str]]]:
        """One action row: [✅ File] [🏷 Type] [🗑 Delete] [↩️ Undo].

        File is hidden when every captured item is already filed. A deduped
        capture ("Already saved") created NOTHING, so it must not offer Undo
        — Undo deletes the items "created by this message", which here would
        be the ORIGINAL item; the delete button is relabelled instead so the
        destruction is explicit.
        """
        token = self.registry.put(Pending(ids=tuple(item.id for item in items)))
        filed = bool(items) and all(item.status == "filed" for item in items)
        deduped = bool(items) and all(getattr(item, "deduplicated", False) for item in items)
        lang = self._language()
        row: list[tuple[str, str]] = []
        if not filed:
            row.append((i18n.t(lang, "btn_file"), f"{CB_FILE}:{token}"))
        row.append((i18n.t(lang, "btn_type"), f"{CB_TYPE}:{token}"))
        if deduped:
            row.append((i18n.t(lang, "btn_delete_original"), f"{CB_DELETE}:{token}"))
            return [row]
        row += [
            (i18n.t(lang, "btn_delete"), f"{CB_DELETE}:{token}"),
            (i18n.t(lang, "btn_undo_capture"), f"{CB_UNDO}:{token}"),
        ]
        return [row]

    def _message_count(self, parsed: Any) -> int:
        """Number of WhatsApp messages across the parsed chunks."""
        total = 0
        for chunk in parsed.chunks:
            total += len(chunk.meta.get("messages", [])) or max(chunk.meta.get("count", 1), 1)
        return total

    # -- cards -------------------------------------------------------------------

    def _card_header(self, items: list[Any], notice: str | None = None) -> str:
        """'✅ Saved · 💬 WhatsApp [· 2 items]' (+ optional notice line).

        An exact-duplicate capture (every returned item flagged deduplicated
        by the ingest guard) says 'Already saved' instead — the card below is
        the EXISTING item.
        """
        lang = self._language()
        if items and all(getattr(item, "deduplicated", False) for item in items):
            header = i18n.t(lang, "already_saved", kind=self._kind_label(items))
        else:
            header = i18n.t(lang, "saved", kind=self._kind_label(items))
        if len(items) > 1:
            header += i18n.t(lang, "n_items", n=len(items))
        return f"{notice}\n{header}" if notice else header

    def _capture_card(self, parsed: Any, items: list[Any]) -> str:
        return self._card_header(items) + "\n" + "\n".join(self._item_card(item) for item in items)

    def _kind_label(self, items: list[Any]) -> str:
        """Source kind for email/whatsapp/calendar; classified type otherwise."""
        lang = self._language()
        item = items[0] if items else None
        source = str(getattr(item, "source", "note") or "note")
        if source in ("email", "whatsapp", "calendar"):
            return f"{KIND_EMOJI.get(source, '📝')} {_kind_name(source, lang)}"
        item_type = str(getattr(item, "type", "") or "note")
        if item_type not in KIND_NAME and item_type not in TYPE_EMOJI:
            item_type = "note"
        return f"{TYPE_EMOJI.get(item_type, '📝')} {_kind_name(item_type, lang)}"

    def _item_card(self, item: Any, indent: str = "") -> str:
        """Compact HTML card: text preview, existing fields, rating, status.

        PR B (D4): a superseded item's text renders struck-through.
        """
        lang = self._language()
        text_line = _one_line(item.raw_text, 200)
        if getattr(item, "superseded_by", None) is not None:
            text_line = strike(_flat_line(item.raw_text, 200))
        lines = [f"{indent}{text_line}"]
        meta = item.meta or {}
        fields: list[str] = []
        who = meta.get("from") or meta.get("sender")
        if not who and meta.get("senders"):
            who = ", ".join(meta["senders"])

        def field(key: str, value: str) -> str:
            return i18n.t(lang, key, value=i18n.iso(value, lang))

        if who:
            fields.append(field("field_from", _one_line(who, 60)))
        if meta.get("subject"):
            fields.append(field("field_subject", _one_line(meta["subject"], 60)))
        when = self._when_of(item, lang)
        if when:
            fields.append(field("field_when", _esc(when)))
        if meta.get("location"):
            fields.append(field("field_where", _one_line(meta["location"], 60)))
        lines += [f"{indent}{line}" for line in fields[:3]]
        actionability = str(item.actionability or "")
        if actionability in ACTIONABILITY_LABEL:
            act = i18n.t(lang, f"act_{actionability}")
        else:  # English keeps its "—"; Hebrew leaves an unknown out
            act = "—" if lang == i18n.DEFAULT else ""
        rating = " · ".join(part for part in (_stars(item.importance), act) if part)
        if rating:
            lines.append(f"{indent}{rating}")
        if item.status == "needs_review":
            percent = int(round((item.confidence or 0) * 100))
            lines.append(f"{indent}{i18n.t(lang, 'needs_review', percent=percent)}")
        return "\n".join(lines)

    def _when_of(self, item: Any, lang: str = i18n.DEFAULT) -> str | None:
        """When-line in the USER'S zone (occurred_at is aware UTC): 'Sat 7 Nov
        19:35' / 'שבת 07.11 19:35' (Hebrew weekday, no English month)."""
        if item.occurred_at is not None:
            local = item.occurred_at.astimezone(self._timezone)
            if lang != i18n.DEFAULT:
                return i18n.short_date(local, lang)
            return f"{local:%a} {local.day} {local:%b} {local:%H:%M}"
        meta = item.meta or {}
        for key in ("start", "date"):
            if meta.get(key):
                return str(meta[key])
        return None

    # -- commands ------------------------------------------------------------------

    def _list_line(self, position: int, item: Any, lang: str) -> str:
        """'3. 💬 <text…> · 6 Nov' — one row of /search and /recent."""
        emoji = KIND_EMOJI.get(item.source, "📝")
        when = self._short_date(item, lang)
        suffix = f" · {when}" if when else ""
        text = i18n.iso(_one_line(item.raw_text, 60), lang)
        return f"{position}. {emoji} {text}{suffix}"

    @_speaks
    def search(self, query: str) -> Reply:
        lang = self._language()
        if not query:
            record_event("telegram", EVENT_SEARCH, ok=False, detail={"error": "empty query"})
            return Reply(i18n.t(lang, "search_usage"))
        with self._services_factory() as services:
            hits = services.search.search(query, limit=5)
        shown = i18n.iso(_esc(query), lang)
        if not hits:
            detail = {"item_ids": [], "chars": len(query)}
            record_event("telegram", EVENT_SEARCH, ok=True, detail=detail)
            return Reply(i18n.t(lang, "search_none", query=shown))
        lines = [i18n.tn(lang, "search_header", len(hits), query=shown)]
        for position, hit in enumerate(hits, start=1):
            lines.append(self._list_line(position, hit.item, lang))
        hint = i18n.t(lang, "open_hint")
        if hint:
            lines.append(hint)
        record_event(
            "telegram",
            EVENT_SEARCH,
            ok=True,
            detail={"item_ids": [str(hit.item.id) for hit in hits], "chars": len(query)},
        )
        return Reply(
            text="\n".join(lines),
            keyboard=self._open_buttons([str(hit.item.id) for hit in hits]),
        )

    @_speaks
    def ask(self, question: str) -> Reply:
        lang = self._language()
        if not question:
            record_event("telegram", EVENT_ASK, ok=False, detail={"error": "empty question"})
            return Reply(i18n.t(lang, "ask_usage"))
        from brain.ai.ask import LLMError  # noqa: PLC0415

        started = time.monotonic()
        try:
            with self._services_factory() as services:
                result = services.ask.ask(question)
        except LLMError as exc:
            record_event(
                "telegram",
                EVENT_ASK,
                ok=False,
                detail={"error": f"{type(exc).__name__}: {exc!s}"[:200]},
            )
            return Reply(i18n.t(lang, "llm_unavailable"))
        elapsed = time.monotonic() - started
        record_event(
            "telegram",
            EVENT_ASK,
            ok=True,
            detail={
                "item_ids": list(result.cited_ids),
                "chars": len(result.answer),
                "model": result.model,
                "elapsed_s": round(elapsed, 1),
            },
        )
        return self._ask_card(result, elapsed, lang=lang)

    def set_language(self, argument: str) -> Reply:
        """/language he|en: store the bot language in the memory layer."""
        current = self._language()
        code = argument.strip().lower()
        usage = i18n.t(
            current,
            "language_usage",
            current=current,
            current_name=i18n.t(current, f"lang_name_{current}"),
        )
        if code not in i18n.SUPPORTED:
            return Reply(usage)
        try:
            with self._services_factory() as services:
                if services.memory is None:
                    return Reply(usage)
                services.memory.set_user_language(code)
        except MemoryError as exc:
            return Reply(i18n.t(current, "language_failed", error=_esc(str(exc))))
        self._lang_local.value = code  # the rest of this update speaks the new language
        return Reply(i18n.t(code, "language_set"))

    def _session_locale(self, session: Any) -> tuple[str, dict[str, str]]:
        """(language, display names) for a card rendered on ``session``:
        the update's language when already read, else read here (and kept
        for the rest of the update)."""
        local = self._lang_local
        known = local.value if getattr(local, "active", False) else None
        lang, names = _locale_of(session, known)
        if getattr(local, "active", False):
            local.value = lang
        return lang, names

    def _language(self) -> str:
        """The user's bot language: read from the memory layer once per update
        (inside a :func:`_speaks` scope) — English on any error."""
        local = self._lang_local
        if getattr(local, "active", False):
            if local.value is None:
                local.value = self._read_language()
            return local.value
        return self._read_language()

    def _read_language(self) -> str:
        try:
            with self._services_factory() as services:
                memory = getattr(services, "memory", None)
                return memory.user_language() if memory is not None else i18n.DEFAULT
        except Exception:  # noqa: BLE001
            logger.warning("telegram: could not read the language from memory", exc_info=True)
            return i18n.DEFAULT

    def _ask_card(self, result: AskResult, elapsed_s: float, *, lang: str = i18n.DEFAULT) -> Reply:
        """Format an answer: 🧠 header, refs instead of raw uuids, 📚 sources.

        No model/latency footer: it meant nothing to the reader (the model
        and elapsed time stay in the ops log)."""
        order = {item_id: n for n, item_id in enumerate(result.cited_ids, start=1)}
        body = _esc(result.answer)
        for item_id, n in order.items():
            body = body.replace(f"[{item_id}]", _ref(n))
        lines = [i18n.t(lang, "ask_answer"), body]
        if order:  # nothing cited -> plain answer, no sources block
            by_id = {str(hit.item.id): hit for hit in result.sources}
            source_lines: list[str] = []
            for item_id, n in order.items():
                hit = by_id.get(item_id)
                if hit is None:
                    continue
                emoji = KIND_EMOJI.get(hit.item.source, "📝")
                when = self._short_date(hit.item, lang)
                suffix = f" · {i18n.t(lang, 'ask_received', date=when)}" if when else ""
                text = i18n.iso(_one_line(hit.item.raw_text, 60), lang)
                source_lines.append(f"{n}. {emoji} {text}{suffix}")
            if source_lines:
                lines += ["", i18n.t(lang, "ask_sources"), *source_lines]
        return Reply(text="\n".join(lines), keyboard=self._open_buttons(result.cited_ids))

    def _short_date(self, item: Any, lang: str = i18n.DEFAULT) -> str | None:
        """Day + month in the bot's timezone (source-line suffix): '7 Nov',
        or 'שבת 07.11' in Hebrew (weekday, no English month name; the
        cards' date shape)."""
        instant = item.occurred_at or item.created_at
        if instant is None:
            return None
        local = instant.astimezone(self._timezone)
        if lang == "he":
            return f"{i18n.weekday(local, lang)} {local:%d.%m}"
        return f"{local.day} {local.strftime('%b')}"

    def _open_buttons(
        self, cited_ids: list[str], limit: int = 5
    ) -> list[list[tuple[str, str]]]:
        """📎 buttons (max ``limit``, rows of 5), numbered like the list."""
        valid: list[uuid_mod.UUID] = []
        for raw in cited_ids or []:
            try:
                valid.append(uuid_mod.UUID(str(raw)))
            except (ValueError, TypeError, AttributeError):
                continue
        if not valid:
            return []
        valid = valid[:limit]
        token = self.registry.put(Pending(ids=tuple(valid)))
        buttons = [(f"📎 {n}", f"{CB_OPEN}:{token}:{n - 1}") for n in range(1, len(valid) + 1)]
        return [buttons[i : i + 5] for i in range(0, len(buttons), 5)]

    @_speaks
    def review(self, offset: int = 0) -> Reply:
        """Walk the inbox one card at a time (File / Type / Delete / Next)."""
        lang = self._language()
        with self._services_factory() as services:
            items = list(
                services.items.list(
                    item_type=None, project=None, status="inbox", limit=10, offset=0
                )
            )
        if not items:
            record_event("telegram", EVENT_REVIEW, ok=True, detail={"item_ids": [], "chars": 0})
            return Reply(i18n.t(lang, "inbox_empty"))
        if not 0 <= offset < len(items):
            return Reply(i18n.t(lang, "inbox_end"))
        item = items[offset]
        token = self.registry.put(Pending(ids=(item.id,)))
        row = [
            (i18n.t(lang, "btn_file"), f"{CB_FILE}:{token}"),
            (i18n.t(lang, "btn_type"), f"{CB_TYPE}:{token}"),
            (i18n.t(lang, "btn_delete"), f"{CB_DELETE}:{token}"),
        ]
        rows = [row]
        if offset + 1 < len(items):
            rows.append([(i18n.t(lang, "btn_next"), f"{CB_NEXT}:{offset + 1}")])
        record_event(
            "telegram", EVENT_REVIEW, ok=True, detail={"item_ids": [str(item.id)], "chars": 0}
        )
        return Reply(
            text=(
                i18n.t(lang, "inbox_position", i=offset + 1, n=len(items))
                + f"\n{self._item_card(item)}"
            ),
            keyboard=rows,
            edit=offset > 0,
        )

    def recent(self) -> Reply:
        """List the last 10 items as compact rows with an Open button each."""
        lang = self._language()
        with self._services_factory() as services:
            items = list(
                services.items.list(item_type=None, project=None, status=None, limit=10, offset=0)
            )
        if not items:
            return Reply(i18n.t(lang, "recent_empty"))
        lines = [i18n.tn(lang, "recent_header", len(items))]
        for position, item in enumerate(items, start=1):
            lines.append(self._list_line(position, item, lang))
        hint = i18n.t(lang, "open_hint")
        if hint:
            lines.append(hint)
        return Reply(
            text="\n".join(lines),
            # every listed row gets its 📎 (rows of 5): the hint says "tap its number"
            keyboard=self._open_buttons([str(item.id) for item in items], limit=len(items)),
        )

    # -- memory (profile + remember) -------------------------------------------

    _KID_FACT_ORDER = ("gender", "school", "grade", "class", "teacher")

    def profile(self) -> Reply:
        """Kids first (gender/class/grade/teacher), then remembered facts.

        Lines are numbered to match the ✖ buttons (soft delete; history kept).
        Fact ids ride in the pending registry so callback data stays < 64B.
        """
        lang = self._language()
        with self._services_factory() as services:
            memory = getattr(services, "memory", None)
            if memory is None:
                return Reply(i18n.t(lang, "memory_missing"))
            # MemoryService.overview returns a LIST of entity dicts
            entities = memory.overview(status="confirmed", q=None)
            names = memory.display_names(lang)
        # Never listed: 'photo' facts (the value is the avatar's sha256 and the
        # numbered remove buttons bypass the screen's photo undo flow) and the
        # bot 'language' setting (changed with /language, not a remembered fact)
        hidden = {"photo", "language"}
        for e in entities:
            if e.get("kind") == "person":
                e["facts"] = [f for f in e.get("facts", [])
                              if f.get("predicate", "").lower() not in hidden]
        persons = [e for e in entities if e.get("kind") == "person" and e.get("facts")]
        by_id = {str(e.get("id")): e.get("name", "?") for e in entities}

        def shown(name: str) -> str:
            return names.get(name, name)

        def value_of(fact: dict[str, Any]) -> str:
            """The fact's value in words: a connection names the other person;
            a known value (boy/girl…) is translated; data shows as stored."""
            value = fact.get("value")
            if value is None and fact.get("object_id"):
                value = shown(by_id.get(str(fact["object_id"]), "?"))
            if value is None:
                return ""
            text = str(value)
            return i18n.label(lang, f"val_{text.lower()}", text)

        def fact_line(n: int, fact: dict[str, Any], bullet: str = "") -> str:
            predicate = str(fact.get("predicate", "note"))
            word = i18n.label(lang, f"pred_{predicate.lower()}", predicate)
            word = word.removeprefix("connection:")
            value = i18n.iso(_one_line(value_of(fact), 80), lang)
            return f"{n}. {bullet}{_esc(word)}: {value}"

        # kids (persons carrying a kid-fact) first, then the rest by name
        persons.sort(key=lambda e: (0 if self._is_kid(e) else 1, e.get("name", "")))
        lines = [i18n.t(lang, "profile_title")]
        fact_ids: list[str] = []
        for entity in persons:
            facts = entity.get("facts", [])
            if entity.get("name") == "me":
                lines.append("")
                lines.append(i18n.t(lang, "profile_remembered"))
                for fact in facts:
                    fact_ids.append(str(fact["id"]))
                    lines.append(fact_line(len(fact_ids), fact, "• "))
                continue
            lines.append("")
            icon = "🧒" if self._is_kid(entity) else "👤"
            name = shown(entity.get("name", "?"))
            lines.append(f"{icon} <b>{i18n.iso(_esc(name), lang)}</b>")
            by_predicate = {f["predicate"]: f for f in facts}
            summary = " · ".join(
                value_of(by_predicate[p])
                for p in self._KID_FACT_ORDER
                if p in by_predicate and by_predicate[p].get("value")
            )
            if summary:
                lines.append(i18n.iso(_esc(summary), lang))
            for fact in by_predicate.values():
                fact_ids.append(str(fact["id"]))
                lines.append(fact_line(len(fact_ids), fact))
        if not fact_ids:
            return Reply(i18n.t(lang, "profile_empty"))
        hint = i18n.t(lang, "profile_hint")
        if hint:
            lines += ["", hint]
        return Reply(text="\n".join(lines), keyboard=self._memory_remove_buttons(fact_ids))

    @staticmethod
    def _is_kid(entity: dict) -> bool:
        """A person carrying at least one SCHOOL fact (grade/class/teacher).

        Gender alone does not make a kid: the parents carry it too (they
        were listed with the 🧒 icon before)."""
        kid_predicates = {"grade", "class", "teacher"}
        return any(f.get("predicate") in kid_predicates for f in entity.get("facts", []))

    def _memory_remove_buttons(self, fact_ids: list[str]) -> list[list[tuple[str, str]]]:
        """Numbered ✖ buttons (rows of 4), numbered like the profile lines."""
        token = self.registry.put(Pending(ids=tuple(uuid_mod.UUID(fid) for fid in fact_ids)))
        buttons = [(f"✖ {n}", f"{CB_MEM_DEL}:{token}:{n - 1}") for n in range(1, len(fact_ids) + 1)]
        return [buttons[i : i + 4] for i in range(0, len(buttons), 4)]

    def _remove_profile_fact(self, pending: Pending, index: int) -> Reply:
        """Soft-delete one profile fact, then re-render the card in place."""
        lang = self._language()
        if not 0 <= index < len(pending.ids):
            return self._toast(i18n.t(lang, "btn_unknown"))
        with self._services_factory() as services:
            memory = getattr(services, "memory", None)
            if memory is None:
                return Reply(i18n.t(lang, "memory_missing"))
            try:
                memory.delete_fact(pending.ids[index])
            except (KeyError, MemoryError):
                return self._toast(i18n.t(lang, "fact_gone"))
        return self.profile_reply(notice=i18n.t(lang, "fact_removed"))

    def profile_reply(self, notice: str | None = None) -> Reply:
        """Re-render the profile card (edit-in-place after a removal)."""
        reply = self.profile()
        text = f"{notice}\n{reply.text}" if notice else reply.text
        return Reply(text=text, keyboard=reply.keyboard, edit=True)

    def remember(self, text: str) -> Reply:
        """Store a free-text fact on the 'me' entity (confirmed)."""
        lang = self._language()
        if not text:
            return Reply(i18n.t(lang, "remember_usage"))
        with self._services_factory() as services:
            memory = getattr(services, "memory", None)
            if memory is None:
                return Reply(i18n.t(lang, "memory_missing"))
            fact = memory.remember(text)
        record_event(
            "telegram",
            EVENT_MEMORY,
            ok=True,
            detail={"fact_id": str((fact or {}).get("id", "")), "chars": len(text)},
        )
        return Reply(i18n.t(lang, "remembered", text=_one_line(text, 300)))

    def _status_hebrew(self, lang: str) -> Reply | None:
        """/status in the user's (non-English) language: header + one line per
        component in words; the technical summary only where something is
        wrong (direction-isolated). None -> the English rendering."""
        entries = _ops_status_entries()
        if entries is None:
            return None
        icon = {"ok": "🟢", "degraded": "🟡", "down": "🔴"}
        statuses = {status for status, _, _ in entries}
        if "down" in statuses:
            header = i18n.t(lang, "status_down")
        elif statuses - {"ok", "disabled", "not probed"}:
            header = i18n.t(lang, "status_degraded")
        else:
            header = i18n.t(lang, "status_healthy")
        lines = [header]
        for status, name, summary in entries:
            word = i18n.label(lang, f"health_{status}", status)
            component = _esc(i18n.label(lang, f"comp_{name}", name))
            line = f"{icon.get(status, '⚪')} {component}: {word}"
            if status in ("degraded", "down") and summary:
                line += f" ({i18n.iso(_esc(summary), lang)})"
            lines.append(line)
        return Reply("\n".join(lines))

    @_speaks
    def status(self) -> Reply:
        """Component health from the cockpit (no event: /status is not data ops)."""
        lang = self._language()
        if lang != i18n.DEFAULT:
            localized = self._status_hebrew(lang)
            if localized is not None:
                return localized
        lines = _ops_status_lines()
        if lines is None:
            return Reply(i18n.t(lang, "status_missing"))
        emoji_lines = [line for line in lines if line.startswith(("🟢", "🟡", "🔴", "⚪"))]
        if emoji_lines and len(emoji_lines) == len(lines):
            if any(line.startswith("🔴") for line in lines):
                header = i18n.t(lang, "status_down")
            elif any(not line.startswith(("🟢", "⚪")) for line in lines):
                header = i18n.t(lang, "status_degraded")
            else:
                header = i18n.t(lang, "status_healthy")
        else:  # legacy plain-string component_status: only ok/not-ok known
            all_ok = all(line.startswith("🟢") or ":" not in line for line in lines)
            header = i18n.t(lang, "status_healthy" if all_ok else "status_degraded")
        return Reply(header + "\n" + "\n".join(lines))

    def start(self) -> Reply:
        return Reply(i18n.t(self._language(), "start"))

    def help_text(self) -> Reply:
        return Reply(i18n.t(self._language(), "help"))

    _UNSUPPORTED = {
        "Voice and video": "unsupported_voice",
        "Files other than .ics": "unsupported_files",
    }

    @_speaks
    def unsupported(self, what: str) -> Reply:
        key = self._UNSUPPORTED.get(what, "unsupported_other")
        return Reply(i18n.t(self._language(), key, what=_esc(what)))

    # -- proposal cards (PR C: delta items 19/20/D2/D4/D7) -----------------------

    @_speaks
    def proposals_changed(self, outcomes: list) -> None:
        """The EnrichmentService completion hook: queue one card per changed
        source for every allowed chat (flushed by the poll loop)."""
        for outcome in outcomes:
            reply = self._proposal_reply_for(outcome.source)
            if reply is None:
                continue
            for chat_id in sorted(self._allowed_ids):
                self._outbound.append((chat_id, reply))

    def drain_outbound(self) -> list[tuple[int, Reply]]:
        """Cards queued by the completion hook, oldest first."""
        queued = self._outbound
        self._outbound = []
        return queued

    def _recon_payload(  # type: ignore[no-untyped-def]  # (untyped before: session/rows)
        self, session, children, lang: str = i18n.DEFAULT
    ):
        """(recon, blocks) for a proposal card: the verdict rows and the
        CONFLICT both-versions blocks (D-recon-2a). The verdict rows carry
        the relation ID in the callback data — nothing positional to
        misalign."""
        recon = self._recon_rows(session, children)
        blocks = [
            conflict_notice(session, rel, lang)  # type: ignore[arg-type]  # rel: ItemRelation
            for verdict, rel, _title in recon.values()
            if verdict == "conflict"
        ]
        return recon, blocks

    def _proposal_reply_for(self, source, *, notice: str | None = None) -> Reply | None:
        """Render the source's proposal card (None when nothing is proposed).

        Verdict children (D-recon-2a) get their verdict row instead of
        Keep/Fix/Drop, and a CONFLICT child adds the both-versions block;
        the verdict relations are parked aligned with the children so the
        callbacks stay < 64 bytes."""
        from brain.db.session import SessionLocal  # noqa: PLC0415

        session = SessionLocal()
        try:
            children = proposed_children_of(session, source.id)
            if not children:
                return None
            lang, names = self._session_locale(session)
            recon, blocks = self._recon_payload(session, children, lang)
            token = self.registry.put(
                Pending(ids=(source.id, *(c.id for c in children)), kind="proposal")
            )
            parts = [part for part in (
                notice,
                render_source_card(
                    source, children, self._timezone.key, recon=recon, lang=lang, names=names
                ),
                *blocks,
            ) if part]
            return Reply(
                text="\n".join(parts),
                keyboard=proposal_keyboard(token, children, recon=recon, lang=lang),
            )
        finally:
            session.close()

    def _recon_rows(self, session, children) -> dict[int, tuple[str, object, str | None]]:
        """index -> (verdict, relation, matched_title) for the card's verdict
        children (D-recon-2a); matched_title names a cancellation's target."""
        from brain.db.models import MemoryItem  # noqa: PLC0415
        from brain.services.relations_view import summary_of  # noqa: PLC0415

        rows: dict[int, tuple[str, object, str | None]] = {}
        for index, child in enumerate(children):
            meta = child.extraction_meta or {}
            verdict = meta.get("recon_verdict")
            if not verdict:
                continue
            rel = recon_relation_of(session, child.id)
            if rel is None:
                continue
            matched_title = None
            if verdict == "cancellation" and session.get(MemoryItem, rel.to_id) is not None:
                matched_title = summary_of(session, rel.to_id)
            rows[index] = (verdict, rel, matched_title)
        return rows

    def _proposal_child_ids(self, pending: Pending) -> list:
        """The child ids a card token refers to (ids[1:])."""
        return list(pending.ids[1:])

    def _proposal_keep(self, pending: Pending, index: int) -> Reply:
        from brain.db.models import MemoryItem  # noqa: PLC0415
        from brain.db.session import SessionLocal  # noqa: PLC0415

        ids = self._proposal_child_ids(pending)
        if not 0 <= index < len(ids):
            return self._toast(i18n.t(self._language(), "card_moved_on"))
        with SessionLocal() as session:
            child = session.get(MemoryItem, ids[index])
            if child is None or child.child_state != "proposed":
                return self._toast(i18n.t(self._language(), "card_moved_on"))
            title = _esc(child.raw_text[:40])
            apply_keep(_repo_of(session), child)
        notice = i18n.t(self._language(), "kept_title", title=title)
        return self._proposal_card_reply(pending, notice=notice)

    def _proposal_drop(self, pending: Pending, index: int) -> Reply:
        from brain.db.models import MemoryItem  # noqa: PLC0415
        from brain.db.session import SessionLocal  # noqa: PLC0415

        ids = self._proposal_child_ids(pending)
        if not 0 <= index < len(ids):
            return self._toast(i18n.t(self._language(), "card_moved_on"))
        with SessionLocal() as session:
            child = session.get(MemoryItem, ids[index])
            if child is None or child.child_state != "proposed":
                return self._toast(i18n.t(self._language(), "card_moved_on"))
            title = _esc(child.raw_text[:40])
            apply_drop(_repo_of(session), child)
        notice = i18n.t(self._language(), "dropped_title", title=title)
        return self._proposal_card_reply(pending, notice=notice)

    def _proposal_keep_all(self, pending: Pending) -> Reply:
        from brain.db.models import MemoryItem  # noqa: PLC0415
        from brain.db.session import SessionLocal  # noqa: PLC0415

        changed = 0
        with SessionLocal() as session:
            repo = _repo_of(session)
            for child_id in self._proposal_child_ids(pending):
                child = session.get(MemoryItem, child_id)
                if child is not None and child.child_state == "proposed":
                    child.child_state = "kept"
                    changed += 1
            if changed:
                repo.commit()
        notice = i18n.tn(self._language(), "kept_n", changed)
        return self._proposal_card_reply(pending, notice=notice)

    def _proposal_card_reply(self, pending: Pending, *, notice: str) -> Reply:
        """Re-render the source card after an action (edit-in-place)."""
        from brain.db.models import MemoryItem  # noqa: PLC0415
        from brain.db.session import SessionLocal  # noqa: PLC0415

        session = SessionLocal()
        try:
            source = session.get(MemoryItem, pending.ids[0]) if pending.ids else None
            children = [
                row
                for row in (session.get(MemoryItem, cid) for cid in pending.ids[1:])
                if row is not None and row.child_state == "proposed"
            ]
            if source is None or not children:
                lang = self._language()
                done = i18n.t(lang, "all_done")
                return Reply(f"{notice}\n{done}", edit=True, toast=notice)
            lang, names = self._session_locale(session)
            recon, blocks = self._recon_payload(session, children, lang)
            token = self.registry.put(
                Pending(ids=(source.id, *(c.id for c in children)), kind="proposal")
            )
            parts = [part for part in (
                notice,
                render_source_card(
                    source, children, self._timezone.key, recon=recon, lang=lang, names=names
                ),
                *blocks,
            ) if part]
            return Reply(
                text="\n".join(parts),
                keyboard=proposal_keyboard(token, children, recon=recon, lang=lang),
                edit=True,
                toast=notice,
            )
        finally:
            session.close()

    # -- relation cards (PR B: items 20a/20b/D4) --------------------------------

    def _relation_error(self, exc: Exception) -> str:
        """A relation service refusal, for the user: English keeps the service
        text; other languages say "already handled" (the raw text names
        internal ids and states)."""
        lang = self._language()
        if lang == i18n.DEFAULT:
            return f"⌛ {_esc(str(exc))}"
        return i18n.t(lang, "already_handled")

    def _relation_transition(self, prefix: str, pending: Pending, arg: str) -> Reply:
        """↩︎ Undo / ✅ Confirm / ✖ Keep-both on a relation (items 20a/20b);
        a proposal card's verdict rows route to the D-recon-2a handler."""
        if pending.kind == "proposal":
            return self._recon_transition(prefix, pending, arg)
        from brain.services.relations import RelationsError, RelationsService  # noqa: PLC0415

        try:
            index = int(arg)
        except ValueError:
            return self._toast(i18n.t(self._language(), "btn_unknown"))
        rel_ids = list(pending.ids[1:])
        if not 0 <= index < len(rel_ids):
            return self._toast(i18n.t(self._language(), "card_moved_on"))
        service = RelationsService()
        try:
            if prefix == CB_REL_UNDO:
                service.undo_supersede(rel_ids[index])
                notice = i18n.t(self._language(), "rel_undone")
            elif prefix == CB_REL_CONFIRM:
                service.confirm_supersede(rel_ids[index])
                notice = i18n.t(self._language(), "rel_confirmed")
            else:  # CB_REL_KEEP_BOTH
                service.discard_proposal(rel_ids[index])
                notice = i18n.t(self._language(), "rel_kept_both")
        except RelationsError as exc:
            return self._toast(self._relation_error(exc))
        return self._card_reply(pending, notice=notice)

    def _recon_transition(self, prefix: str, pending: Pending, arg: str) -> Reply:
        """Verdict rows on a proposal card (D-recon-2a): ✅ confirm update /
        keep new / confirm cancellation, ✖ keep both, ✅ keep existing /
        ✖ not cancelled, ↩︎ undo. The callback carries the RELATION ID (not
        a position), so a mixed card cannot misalign plain and verdict
        rows; nothing ever applies without a tap, and a stale callback
        after the relation changed answers "already handled" — every
        action re-checks the relation's state."""
        from brain.db.models import ItemRelation  # noqa: PLC0415
        from brain.db.session import SessionLocal  # noqa: PLC0415
        from brain.services.extraction.reconciliation import (  # noqa: PLC0415
            ReconciliationService,
        )
        from brain.services.relations import RelationsError, RelationsService  # noqa: PLC0415

        try:
            rel_id = uuid_mod.UUID(str(arg))
        except ValueError:
            return self._toast(i18n.t(self._language(), "btn_unknown"))
        already = i18n.t(self._language(), "already_handled")
        notice = None
        with SessionLocal() as session:
            rel = session.get(ItemRelation, rel_id)
            if rel is None:
                return self._toast(already)
            if rel.from_id not in pending.ids[1:]:
                # a relation id from ANOTHER card must never resolve here
                return self._toast(already)
            child_id = rel.from_id  # the verdict child owns the relation
            state = (rel.provenance or {}).get("state")
            service = ReconciliationService(
                session, _repo_of(session), tz_name=self._timezone.key,
            )
            try:
                if prefix == CB_REL_CONFIRM:
                    if rel.kind == "supersedes":
                        if state != "proposed":
                            return self._toast(already)
                        RelationsService().confirm_supersede(rel_id)
                        self._recon_child_kept(session, child_id)
                        notice = i18n.t(self._language(), "rel_confirmed")
                    elif rel.kind == "conflicts":
                        service.resolve_conflict(rel_id, keep="new")
                        notice = i18n.t(self._language(), "rel_kept_new")
                    elif rel.kind == "cancels":
                        if state != "proposed":
                            return self._toast(already)
                        service.apply_cancellation(rel_id)
                        self._recon_child_kept(session, child_id)
                        notice = i18n.t(self._language(), "rel_cancelled")
                elif prefix == CB_REL_KEEP_BOTH:  # ✖ Keep both on a proposed UPDATE
                    RelationsService().discard_proposal(rel_id)
                    self._recon_child_kept(session, child_id)
                    notice = i18n.t(self._language(), "rel_kept_both")
                elif prefix == CB_RECON_DISCARD:
                    if rel.kind == "conflicts":
                        service.resolve_conflict(rel_id, keep="existing")
                        notice = i18n.t(self._language(), "rel_kept_existing")
                    elif rel.kind == "cancels":
                        service.discard_cancellation(rel_id)
                        notice = i18n.t(self._language(), "rel_not_cancelled")
                elif prefix == CB_REL_UNDO:
                    if rel.kind == "cancels":
                        if state != "applied":
                            return self._toast(already)
                        service.restore_cancellation(rel_id)
                        notice = i18n.t(self._language(), "rel_restored")
                    else:
                        if state != "applied":
                            return self._toast(already)
                        RelationsService().undo_supersede(rel_id)
                        notice = i18n.t(self._language(), "rel_undone")
            except (RelationsError, ValueError):
                return self._toast(already)
        if notice is None:
            return self._toast(already)
        return self._proposal_card_reply(pending, notice=notice)

    def _recon_child_kept(self, session, child_id) -> None:
        """The acted verdict child becomes kept (it stays visible; a re-run
        never overwrites a user-acted child)."""
        from brain.db.models import MemoryItem  # noqa: PLC0415

        child = session.get(MemoryItem, child_id)
        if child is not None:
            child.child_state = "kept"
            session.commit()

    def _relation_restore(self, pending: Pending) -> Reply:
        """♻️ Restore a superseded (D4) or cancelled (D-recon-2a) item: undo
        the relation that struck it."""
        from brain.db.session import SessionLocal  # noqa: PLC0415
        from brain.services.extraction.reconciliation import (  # noqa: PLC0415
            ReconciliationService,
        )
        from brain.services.relations import RelationsError, RelationsService  # noqa: PLC0415
        from brain.services.relations_view import (  # noqa: PLC0415
            cancelled_by_rel,
            resolved_conflict_by_rel,
            superseded_by_rel,
        )

        item_id = pending.ids[0]
        session = SessionLocal()
        try:
            rel = (
                superseded_by_rel(session, item_id)
                or cancelled_by_rel(session, item_id)
                or resolved_conflict_by_rel(session, item_id)
            )
        finally:
            session.close()
        if rel is None:
            return self._toast(i18n.t(self._language(), "card_moved_on"))
        try:
            if rel.kind == "cancels":
                with SessionLocal() as svc_session:
                    ReconciliationService(
                        svc_session, _repo_of(svc_session), tz_name=self._timezone.key,
                    ).restore_cancellation(rel.id)
            elif rel.kind == "conflicts":
                with SessionLocal() as svc_session:
                    ReconciliationService(
                        svc_session, _repo_of(svc_session), tz_name=self._timezone.key,
                    ).undo_resolution(rel.id)
            else:
                RelationsService().undo_supersede(rel.id)
        except (RelationsError, ValueError) as exc:
            return self._toast(self._relation_error(exc))
        return self._card_reply(pending, notice=i18n.t(self._language(), "rel_restored"))

    def _proposal_fix_start(self, user_id: int | None, pending: Pending, index: int) -> Reply:
        from brain.db.models import MemoryItem  # noqa: PLC0415
        from brain.db.session import SessionLocal  # noqa: PLC0415

        ids = self._proposal_child_ids(pending)
        if not 0 <= index < len(ids):
            return self._toast(i18n.t(self._language(), "card_moved_on"))
        with SessionLocal() as session:
            child = session.get(MemoryItem, ids[index])
        if child is None or child.child_state != "proposed":
            return self._toast(i18n.t(self._language(), "card_moved_on"))
        if user_id is None:
            return Reply(i18n.t(self._language(), "fix_no_user"), edit=True)
        token = self.registry.put(Pending(ids=(child.id,), kind="fix", step="title"))
        self._fix_flow[user_id] = token
        return Reply(i18n.t(self._language(), "fix_title_prompt", title=_esc(child.raw_text[:60])))

    def _fix_input(self, user_id: int, text: str) -> Reply:
        """One step of the Fix flow (title, then date)."""
        token = self._fix_flow.get(user_id)
        pending = self.registry.get(token) if token else None
        if pending is None or pending.kind != "fix":
            self._fix_flow.pop(user_id, None)
            return self.capture(text)
        if text.lower() in i18n.CANCEL_WORDS:
            self._fix_flow.pop(user_id, None)
            return Reply(i18n.t(self._language(), "fix_cancelled"))
        if pending.step == "title":
            self._fix_flow[user_id] = self.registry.put(
                Pending(ids=pending.ids, text=text, kind="fix", step="date")
            )
            return Reply(i18n.t(self._language(), "fix_date_prompt", title=_esc(text[:60])))
        # step == "date"
        title_pending = self.registry.get(token)
        title = (title_pending.text if title_pending else "") or ""
        child_id = pending.ids[0]
        from brain.db.session import SessionLocal  # noqa: PLC0415

        with SessionLocal() as session:
            from brain.db.models import MemoryItem  # noqa: PLC0415

            child = session.get(MemoryItem, child_id)
            if child is None:
                self._fix_flow.pop(user_id, None)
                return Reply(i18n.t(self._language(), "fix_item_gone"))
            source = self._proposal_source_of(child, session)
            try:
                if text.lower() in i18n.KEEP_WORDS:
                    fixed = child
                    fixed.raw_text = title
                    fixed.child_state = "kept"
                    fixed.extraction_meta = {**(fixed.extraction_meta or {}), "user_edited": True}
                    _repo_of(session).commit()
                else:
                    fixed = apply_fix(
                        _repo_of(session),
                        child,
                        title=title,
                        when_raw=text,
                        source=source,
                        tz_name=self._timezone.key,
                    )
            except FixError as exc:
                lang = self._language()
                error = i18n.label(lang, f"fixerr_{exc.code}", str(exc))
                return Reply(i18n.t(lang, "fix_retry", error=_esc(error)))
            self._fix_flow.pop(user_id, None)
            return Reply(i18n.t(self._language(), "fix_done", title=_esc(fixed.raw_text[:60])))

    def _proposal_source_of(self, child, session):
        from sqlalchemy import select  # noqa: PLC0415

        from brain.db.models import ItemSource, MemoryItem  # noqa: PLC0415

        row = session.execute(
            select(MemoryItem)
            .join(ItemSource, ItemSource.source_id == MemoryItem.id)
            .where(ItemSource.child_id == child.id)
            .order_by(ItemSource.created_at.asc())
            .limit(1)
        ).scalar_one_or_none()
        return row

    @_speaks
    def due_pings(self, now: datetime | None = None) -> list[tuple[int, Reply]]:
        """The 48h ping (D7): one Keep/Drop ping per due proposed task."""
        from brain.db.session import SessionLocal  # noqa: PLC0415

        moment = now or datetime.now(tz=UTC)
        session = SessionLocal()
        try:
            children = due_proposed_children(session, moment, self._timezone.key)
            if not children:
                return []
            mark_pinged(children, moment)
            session.commit()
            pings: list[tuple[int, Reply]] = []
            lang, names = self._session_locale(session)
            for child in children:
                token = self.registry.put(Pending(ids=(child.id,), kind="proposal"))
                reply = Reply(
                    text=ping_line(child, self._timezone.key, lang=lang, names=names),
                    keyboard=[
                        [(i18n.t(lang, "btn_keep", n="").strip(), f"{CB_PROPOSED_KEEP}:{token}:0")],
                        [(i18n.t(lang, "btn_drop", n="").strip(), f"{CB_PROPOSED_DROP}:{token}:0")],
                    ],
                )
                for chat_id in sorted(self._allowed_ids):
                    pings.append((chat_id, reply))
            return pings
        finally:
            session.close()

    # -- scheduled tasks ----------------------------------------------------------

    def schedules(self, notice: str | None = None, *, edit: bool = False) -> Reply:
        """/schedules: every task with its state + next run, and per task a
        ▶️ run-now and a ⏸ pause / ▶ resume button (ids parked in the
        registry: callback data stays ~15 bytes)."""
        lang = self._language()
        with self._schedule_store() as store:
            rows = [
                (task.id, task.name, task.schedule, task.timezone, task.enabled, task.next_run_at)
                for task in store.all()[:SCHEDULES_LIST_MAX]
            ]
        head = [notice] if notice else []
        if not rows:
            return Reply("\n".join([*head, i18n.t(lang, "sched_list_empty")]), edit=edit)
        token = self.registry.put(Pending(ids=tuple(row[0] for row in rows), kind="schedules"))
        lines = [*head, i18n.t(lang, "sched_list_header")]
        keyboard: list[list[tuple[str, str]]] = []
        for n, (_, name, schedule, tz_name, enabled, next_run_at) in enumerate(rows, start=1):
            icon = "▶" if enabled else "⏸"
            words = describe_schedule(schedule, lang)
            lines.append(f"{n}. {icon} <b>{_one_line(name, 60)}</b> — {_esc(words)}")
            state = i18n.t(lang, "sched_active" if enabled else "sched_paused")
            if enabled and next_run_at is not None:
                local = next_run_at.astimezone(ZoneInfo(tz_name))
                when = f"{i18n.weekday(local, lang)} {local.strftime('%d.%m %H:%M')}"
                state += " · " + i18n.t(lang, "sched_next", when=when)
            lines.append(f"    {state}")
            toggle = "btn_sched_pause" if enabled else "btn_sched_resume"
            keyboard.append(
                [
                    (i18n.t(lang, "btn_sched_run", n=n), f"{CB_SCHED_RUN}:{token}:{n - 1}"),
                    (i18n.t(lang, toggle, n=n), f"{CB_SCHED_TOGGLE}:{token}:{n - 1}"),
                ]
            )
        return Reply("\n".join(lines), keyboard=keyboard, edit=edit)

    def _schedule_action(self, prefix: str, pending: Pending, index: int) -> Reply:
        """▶️ queues a run-now (the flush right after this update claims it);
        ⏸ / ▶ pauses or resumes. Re-renders the list in place."""
        lang = self._language()
        if pending.kind != "schedules" or not 0 <= index < len(pending.ids):
            return self._toast(i18n.t(self._language(), "btn_unknown"))
        task_id = pending.ids[index]
        try:
            with self._schedule_store() as store:
                if prefix == CB_SCHED_RUN:
                    task = store.request_run(task_id)
                    notice = i18n.t(lang, "sched_queued", name=_one_line(task.name, 60))
                else:
                    current = store.get(task_id)
                    task = store.set_enabled(task_id, not current.enabled)
                    key = "sched_now_resumed" if task.enabled else "sched_now_paused"
                    notice = i18n.t(lang, key, name=_one_line(task.name, 60))
        except ScheduledTaskNotFound:
            notice = i18n.t(lang, "sched_gone")
        return self.schedules(notice, edit=True)

    def claim_scheduled(self, *, max_late_s: float, stale_after_s: float) -> list[Claim]:
        """Claim every due task (compare-and-set; see scheduled_tasks).
        Tasks whose previous worker is still running are left for later."""
        with self._schedule_store() as store:
            return store.claim_due(
                max_late_s=max_late_s,
                exclude=tuple(self._sched_inflight),
                stale_after_s=stale_after_s,
            )

    def run_scheduled(self, claim: Claim) -> tuple[RunOutcome, str]:
        """Execute one claimed task through the /ask pipeline -> (outcome,
        lang). BLOCKING (the LLM client uses asyncio.run): worker thread only."""
        lang = self._language()

        def ask(prompt: str) -> Any:
            with self._services_factory() as services:
                return services.ask.ask(prompt)

        return execute_claim(self._schedule_store, claim, ask), lang

    def finish_scheduled_timeout(self, claim: Claim, timeout_s: float) -> tuple[bool, str]:
        """Record a timed-out run as an error (unless the worker beat us)."""
        with self._schedule_store() as store:
            recorded = store.finish_run(
                claim.run_id, status="error", error=f"timed out after {timeout_s:.0f}s"
            )
        return recorded, self._language()

    def scheduled_reply(self, claim: Claim, outcome: RunOutcome, lang: str) -> Reply:
        """The message for a finished run: ⏰ <name> + the ask answer card, or
        the failure line. Loop thread only (the card parks its 📎 buttons in
        the registry)."""
        header = i18n.t(lang, "sched_result_header", name=_one_line(claim.name, 100))
        if outcome.status == "ok" and outcome.result is not None:
            card = self._ask_card(outcome.result, 0.0, lang=lang)
            return Reply(f"{header}\n{card.text}", keyboard=card.keyboard)
        if outcome.llm_unavailable:
            return Reply(f"{header}\n{i18n.t(lang, 'llm_unavailable')}")
        error = _one_line(outcome.error or "error", 200)
        return Reply(f"{header}\n{i18n.t(lang, 'sched_failed', error=error)}")

    def scheduled_timeout_reply(self, claim: Claim, timeout_s: float, lang: str) -> Reply:
        header = i18n.t(lang, "sched_result_header", name=_one_line(claim.name, 100))
        body = i18n.t(lang, "sched_timed_out", seconds=f"{timeout_s:.0f}")
        return Reply(f"{header}\n{body}")

    def scheduled_recipients(self) -> list[int]:
        """Every allowed user gets the result (same as the proposal cards)."""
        return sorted(self._allowed_ids)

    # -- callbacks -----------------------------------------------------------------

    @staticmethod
    def _toast(text: str) -> Reply:
        """A feedback-only callback result: a toast on the tapped button, no
        chat message, and the card it came from stays as it is."""
        return Reply(text="", toast=text)

    @_speaks
    def handle_callback(self, data: str, user_id: int | None = None) -> Reply:
        """Dispatch an inline-button press; stale/unknown → graceful reply.

        ``user_id`` is optional (PR C): the Fix button opens a per-user edit
        flow, so the callback needs to know who pressed it.
        """
        prefix, _, rest = data.partition(":")
        try:
            if prefix == CB_NEXT:
                return self.review(int(rest))
            if prefix in (
                CB_TYPE_APPLY,
                CB_OPEN,
                CB_MEM_DEL,
                CB_PROPOSED_KEEP,
                CB_PROPOSED_FIX,
                CB_PROPOSED_DROP,
                CB_REL_UNDO,
                CB_REL_CONFIRM,
                CB_REL_KEEP_BOTH,
                CB_RECON_DISCARD,
                CB_SCHED_RUN,
                CB_SCHED_TOGGLE,
            ):
                token, _, arg = rest.partition(":")
            elif prefix in (
                CB_UNDO,
                CB_FILE,
                CB_DELETE,
                CB_TYPE,
                CB_BACK,
                CB_WA_JOIN,
                CB_WA_SPLIT,
                CB_PROPOSED_KEEP_ALL,
                CB_REL_RESTORE,
            ):
                token, arg = rest, ""
            else:
                return self._toast(i18n.t(self._language(), "btn_unknown"))
            if prefix in (
                CB_OPEN,
                CB_MEM_DEL,
                CB_PROPOSED_KEEP,
                CB_PROPOSED_FIX,
                CB_PROPOSED_DROP,
                CB_SCHED_RUN,
                CB_SCHED_TOGGLE,
            ):
                index = int(arg)
        except ValueError:
            return self._toast(i18n.t(self._language(), "btn_unknown"))
        pending = self.registry.get(token)
        if pending is None:
            return self._toast(i18n.t(self._language(), "btn_expired"))
        if prefix == CB_WA_JOIN:
            return self._save_whatsapp_choice(pending, split=False)
        if prefix == CB_WA_SPLIT:
            return self._save_whatsapp_choice(pending, split=True)
        if prefix == CB_TYPE:
            return self._type_menu(token)
        if prefix == CB_BACK:
            return self._card_reply(pending)
        if prefix == CB_TYPE_APPLY:
            return self._apply_type(pending, arg)
        if prefix == CB_FILE:
            changed = self._update_items(pending, {"status": "filed"})
            return self._card_reply(pending, notice=i18n.tn(self._language(), "filed", changed))
        if prefix == CB_OPEN:
            return self._open_card(pending, index)
        if prefix == CB_MEM_DEL:
            return self._remove_profile_fact(pending, index)
        if prefix == CB_PROPOSED_KEEP:
            return self._proposal_keep(pending, index)
        if prefix == CB_PROPOSED_DROP:
            return self._proposal_drop(pending, index)
        if prefix == CB_PROPOSED_FIX:
            return self._proposal_fix_start(user_id, pending, index)
        if prefix == CB_PROPOSED_KEEP_ALL:
            return self._proposal_keep_all(pending)
        if prefix in (CB_REL_UNDO, CB_REL_CONFIRM, CB_REL_KEEP_BOTH, CB_RECON_DISCARD):
            return self._relation_transition(prefix, pending, arg)
        if prefix == CB_REL_RESTORE:
            return self._relation_restore(pending)
        if prefix in (CB_SCHED_RUN, CB_SCHED_TOGGLE):
            return self._schedule_action(prefix, pending, index)
        if prefix == CB_UNDO:
            return self._delete_items(pending, undo=True)
        return self._delete_items(pending, undo=False)  # CB_DELETE

    def _card_reply(self, pending: Pending, notice: str | None = None) -> Reply:
        """Re-render the capture card for the pending items (edit-in-place).

        PR B: items with applied/proposed supersedes relations carry the
        correction notice + the ↩︎ Undo / ✅ Confirm / ✖ Keep-both rows
        (items 20a/20b).
        """
        from brain.services.items import ItemNotFound  # noqa: PLC0415

        items = []
        with self._services_factory() as services:
            for item_id in pending.ids:
                try:
                    items.append(services.items.get(item_id))
                except ItemNotFound:
                    continue
        if not items:
            return Reply(i18n.t(self._language(), "deleted_card"), edit=True)
        relation_notices: list[str] = []
        relation_rows: list[list[tuple[str, str]]] = []
        from brain.db.session import SessionLocal  # noqa: PLC0415

        lang = self._language()
        session = SessionLocal()
        try:
            for item in items:
                rel_notice = correction_notice(session, item.id, lang)
                if rel_notice:
                    relation_notices.append(rel_notice)
                relation_rows += self._relations_rows_for(item.id)
        finally:
            session.close()
        text = (
            self._card_header(items, notice=notice)
            + "\n"
            + "\n".join(self._item_card(item) for item in items)
        )
        if relation_notices:
            text += "\n" + "\n".join(relation_notices)
        return Reply(text=text, keyboard=self._capture_buttons(items) + relation_rows, edit=True)

    def _open_card(self, pending: Pending, index: int) -> Reply:
        """Show one cited item's card (the Open button under /ask answers).

        PR B: a superseded item renders struck-through with ♻️ Restore
        (D4); a card whose item applied/proposed a supersedes shows the
        correction notice with ↩︎ Undo / ✅ Confirm / ✖ Keep-both rows.
        """
        from brain.services.items import ItemNotFound  # noqa: PLC0415

        if not 0 <= index < len(pending.ids):
            return self._toast(i18n.t(self._language(), "btn_unknown"))
        with self._services_factory() as services:
            try:
                item = services.items.get(pending.ids[index])
            except ItemNotFound:
                return self._toast(i18n.t(self._language(), "item_deleted"))
        from brain.db.session import SessionLocal  # noqa: PLC0415

        lang = self._language()
        session = SessionLocal()
        try:
            replaced_by = replaced_by_notice(session, item.id, lang)
            notice = correction_notice(session, item.id, lang)
            details = details_notice(session, item.id, lang)
        finally:
            session.close()
        lines = [self._item_card(item)]
        if replaced_by:
            lines.append(replaced_by)
        if notice:
            lines.append(notice)
        if details:
            lines.append(details)
        keyboard = self._relations_rows_for(item.id)
        return Reply("\n".join(lines), keyboard=keyboard or None)

    def _relations_rows_for(self, item_id) -> list[list[tuple[str, str]]]:
        """Relation action rows for one item (D4 Restore + 20a/b rows + the
        cancelled target's Restore, D-recon-2a)."""
        from brain.db.session import SessionLocal  # noqa: PLC0415
        from brain.services.relations_view import (  # noqa: PLC0415
            applied_supersedes_of,
            cancelled_by_rel,
            proposed_supersedes_of,
            resolved_conflict_by_rel,
            superseded_by_rel,
        )

        lang = self._language()
        rows: list[list[tuple[str, str]]] = []
        session = SessionLocal()
        try:
            struck = (
                superseded_by_rel(session, item_id)
                or cancelled_by_rel(session, item_id)
                or resolved_conflict_by_rel(session, item_id)
            )
            if struck is not None:
                token = self.registry.put(Pending(ids=(item_id,), kind="relation"))
                rows.append([(i18n.t(lang, "btn_restore"), f"{CB_REL_RESTORE}:{token}")])
            applied = applied_supersedes_of(session, item_id)
            proposed = proposed_supersedes_of(session, item_id)
            if applied or proposed:
                rel_ids = [rel.id for rel in applied] + [rel.id for rel in proposed]
                token = self.registry.put(Pending(ids=(item_id, *rel_ids), kind="relation"))
                rows += relations_keyboard(token, applied=applied, proposed=proposed, lang=lang)
        finally:
            session.close()
        return rows

    def _save_whatsapp_choice(self, pending: Pending, *, split: bool) -> Reply:
        parsed = parse_paste(pending.text or "", split=split, tz=self._timezone)
        return self._save_parsed(parsed)

    def _type_menu(self, token: str) -> Reply:
        """Type picker: emoji labels in rows of up to 3 + a back row."""
        from brain.db.enums import ItemType  # noqa: PLC0415

        lang = self._language()
        buttons = [
            (
                f"{TYPE_EMOJI.get(t.value, '🏷')} {_kind_name(t.value, lang)}",
                f"{CB_TYPE_APPLY}:{token}:{t.value}",
            )
            for t in ItemType
        ]
        grid = [buttons[i : i + 3] for i in range(0, len(buttons), 3)]
        back = [[(i18n.t(lang, "btn_back"), f"{CB_BACK}:{token}")]]
        return Reply(i18n.t(lang, "pick_type"), keyboard=grid + back)

    def _apply_type(self, pending: Pending, type_value: str) -> Reply:
        from brain.db.enums import ItemType  # noqa: PLC0415

        try:
            item_type = ItemType(type_value)
        except ValueError:
            return self._toast(i18n.t(self._language(), "type_unknown"))
        self._update_items(pending, {"type": item_type})
        lang = self._language()
        label = _kind_name(item_type.value, lang)
        return self._card_reply(pending, notice=i18n.t(lang, "type_set", label=label))

    def _update_items(self, pending: Pending, changes: dict[str, Any]) -> int:
        """Apply an ItemUpdate to the pending ids; returns how many changed."""
        from brain.services.items import ItemNotFound  # noqa: PLC0415

        changed = 0
        with self._services_factory() as services:
            for item_id in pending.ids:
                try:
                    services.items.update(item_id, ItemUpdate(**changes))
                    changed += 1
                except ItemNotFound:
                    continue  # item deleted since the button was rendered
        return changed

    def _delete_items(self, pending: Pending, *, undo: bool) -> Reply:
        from brain.services.items import ItemNotFound  # noqa: PLC0415

        deleted = 0
        with self._services_factory() as services:
            for item_id in pending.ids:
                try:
                    services.items.delete(item_id)
                    deleted += 1
                except ItemNotFound:
                    continue  # already gone: Undo pressed twice, etc.
        key = "undone" if undo else "deleted"
        return Reply(i18n.tn(self._language(), key, deleted), edit=True)


# --- polling adapter (needs python-telegram-bot) -----------------------------


async def poll_loop(
    tg: Any,
    core: TelegramBot,
    *,
    poll_timeout_s: int = 50,
    conflict_window_s: float = 600.0,
    conflict_backoff_s: float = 5.0,
) -> None:
    """Long-poll getUpdates and process updates until an exception escapes.

    ``tg`` is a python-telegram-bot ``Bot`` (or anything with async
    ``initialize``/``shutdown``/``set_my_commands``/``get_updates``/
    ``send_message``/``send_chat_action``/``get_file`` — fakes in tests).
    Registers bot commands once, heartbeats once per poll cycle; a 409
    conflict is usually TRANSIENT (the previous poller's long-poll lingers
    after a restart/takeover), so it is retried with backoff (5s doubling to
    60s) for ``conflict_window_s`` (default 10 minutes), recording an ops
    "conflict" event per retry; only a conflict persisting past the window
    stops the bot. An Unauthorized token is recorded and stops the bot;
    per-update failures never stop the loop.
    """
    from telegram import BotCommand  # noqa: PLC0415
    from telegram.error import Conflict, InvalidToken, NetworkError  # noqa: PLC0415

    await tg.initialize()
    # the menu speaks the bot's language at startup (one memory read)
    language = getattr(core, "_language", None)  # stub cores in older tests lack it
    lang = await asyncio.to_thread(language) if language is not None else None
    commands = [BotCommand(name, description) for name, description in bot_commands(lang)]
    try:
        await tg.set_my_commands(commands)
    except Exception:  # noqa: BLE001  (command registration is cosmetic)
        logger.debug("telegram: set_my_commands failed", exc_info=True)
    offset: int | None = None
    last_update_id = 0
    conflict_until: float | None = None
    backoff = conflict_backoff_s
    # Network-failure streak tracking: the first successful poll after >=1
    # failure records one telegram/network_recovered event (failures + outage
    # seconds) so streaks are visible in ops without grepping logs.
    net_failures = 0
    net_first_failure_monotonic: float | None = None
    try:
        while True:
            # Liveness on the poll path: the long-poll itself can occupy the
            # whole timeout window, so the heartbeat must start FRESH before
            # each attempt (previously only successful polls heartbeated, and
            # a quiet chat pushed the age past health's threshold).
            heartbeat("telegram", {"phase": "polling", "last_update_id": last_update_id})
            try:
                updates = await tg.get_updates(offset=offset, timeout=poll_timeout_s)
            except Conflict as exc:
                now = time.monotonic()
                if conflict_until is None:
                    conflict_until = now + conflict_window_s
                if now >= conflict_until:
                    record_event(
                        "telegram",
                        "conflict",
                        ok=False,
                        detail={
                            "error": f"{type(exc).__name__}: conflict persisted past "
                            f"{conflict_window_s:.0f}s window"[:200],
                            "gave_up": True,
                        },
                    )
                    logger.error(
                        "telegram: getUpdates conflict persisted %.0fs — stopping (%s)",
                        conflict_window_s,
                        exc,
                    )
                    raise
                record_event(
                    "telegram",
                    "conflict",
                    ok=False,
                    detail={
                        "error": f"{type(exc).__name__}: another getUpdates is running"[:200],
                        "retry_in_s": round(backoff, 2),
                    },
                )
                logger.warning(
                    "telegram: getUpdates conflict — another poller may be starting; "
                    "retrying in %.0fs (%s)",
                    backoff,
                    exc,
                )
                # Liveness on the retry path: the backoff sleep can outlast the
                # poll timeout (doubling up to 60s) — prove the loop is alive.
                heartbeat("telegram", {"phase": "backoff", "last_update_id": last_update_id})
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60.0)
                continue
            except InvalidToken as exc:
                record_event(
                    "telegram",
                    EVENT_ERROR,
                    ok=False,
                    detail={"error": f"{type(exc).__name__}: invalid token"[:200]},
                )
                logger.error("telegram: invalid bot token — stopping (%s)", exc)
                raise
            except NetworkError as exc:
                logger.warning("telegram: network error, retrying (%s)", type(exc).__name__)
                net_failures += 1
                if net_first_failure_monotonic is None:
                    net_first_failure_monotonic = time.monotonic()
                # Liveness on the retry path (see the conflict branch).
                heartbeat("telegram", {"phase": "backoff", "last_update_id": last_update_id})
                await asyncio.sleep(3)
                continue
            # A successful poll means the other poller is gone: fresh window.
            conflict_until = None
            backoff = conflict_backoff_s
            if net_failures:
                outage_s = time.monotonic() - (net_first_failure_monotonic or time.monotonic())
                logger.info(
                    "telegram: network recovered after %d failure(s) (%.0fs)",
                    net_failures,
                    outage_s,
                )
                record_event(
                    "telegram",
                    "network_recovered",
                    ok=True,
                    detail={"failures": net_failures, "outage_s": round(outage_s, 1)},
                )
                net_failures = 0
                net_first_failure_monotonic = None
            heartbeat(
                "telegram",
                {
                    "allowed_users": core.allowed_count,
                    "last_update_id": last_update_id,
                    "pending": len(updates),
                },
            )
            for update in updates:
                offset = update.update_id + 1
                last_update_id = update.update_id
                try:
                    await _handle_update(tg, core, update)
                except Exception as exc:  # noqa: BLE001  (one bad update must not stop the bot)
                    record_event(
                        "telegram",
                        EVENT_ERROR,
                        ok=False,
                        detail={"error": f"{type(exc).__name__}: {exc!s}"[:200]},
                    )
                    logger.exception("telegram: update handling failed")
            await _flush_bot_jobs(tg, core)
    finally:
        # An in-flight enrichment pass gets a short grace: deliver what it
        # produced, cancel what overruns (see _shutdown_enrichment).
        await _shutdown_enrichment(tg, core)
        shutdown = getattr(tg, "shutdown", None)
        if shutdown is not None:
            await shutdown()


def _item_indexer():
    """The shared vector indexer (app-lifetime singleton), or None in tests."""
    try:
        from brain.api import deps  # noqa: PLC0415

        return deps.get_item_indexer()
    except Exception:  # noqa: BLE001 — no app context: skip re-indexing
        return None


def _repo_of(session):
    """An ItemsRepository bound to an existing session (proposal actions)."""
    from brain.repositories.items import ItemsRepository  # noqa: PLC0415

    return ItemsRepository(session)


def _build_pass_service(settings: Any, session: Any) -> Any:
    """The pass's own EnrichmentService — built ONCE PER PASS, never per
    update (the per-update factory stays exactly as it was). Same seams as
    the factory used to wire: settings-driven extractor (the A2 activation
    seam's default), memory compaction, shared indexer, daily cap, timezone.
    """
    from brain.api import deps  # noqa: PLC0415
    from brain.repositories.items import ItemsRepository  # noqa: PLC0415
    from brain.repositories.memory import MemoryRepository  # noqa: PLC0415
    from brain.services.extraction.service import (  # noqa: PLC0415
        EnrichmentService,
    )
    from brain.services.memory import MemoryService  # noqa: PLC0415

    return EnrichmentService(
        ItemsRepository(session),
        session,
        memory=MemoryService(MemoryRepository(session)),
        indexer=deps.get_item_indexer(),
        daily_cap=settings.enrichment_daily_cap,
        timezone=settings.timezone,
    )


def _enrich_pass(service: Any, *, sources: Sequence[str] | None = None) -> list:
    """One ``run_once`` pass with a LOCAL completion collector (B2).

    The hook delivers one LIST of changed-source outcomes per call — they
    collect into a plain local list; core state is NEVER touched from the
    worker thread. The loop thread alone turns outcomes into proposal cards.
    ``sources`` is the A2 step-2 week-1 filter: the worker drains only those
    sources (empty/None = every source; the CLI is never filtered).
    """
    outcomes: list = []
    service.on_children_changed = outcomes.extend
    service.run_once(datetime.now(tz=UTC), limit=20, sources=sources or None)
    return outcomes


def _run_enrichment_pass() -> list:
    """One synchronous enrichment pass (A2 worker; runs in a worker thread).

    Own session + own service (B3: the per-update factory stays as it was).
    Returns the changed-source outcomes (possibly empty); the LOOP thread
    turns them into proposal cards.
    """
    from brain.config import get_settings  # noqa: PLC0415

    settings = get_settings()
    from brain.db.session import SessionLocal  # noqa: PLC0415

    session = SessionLocal()
    try:
        return _enrich_pass(
            _build_pass_service(settings, session),
            sources=settings.worker_source_list or None,
        )
    finally:
        session.close()


async def _enrichment_tick(tg: Any, core: TelegramBot) -> None:
    """The scheduled enrichment pass (A2 wiring; ENRICHMENT_WORKER_ENABLED).

    Off by default: when the switch is false nothing is scheduled and nothing
    changes. When true: one ``run_once`` pass per interval, STARTED here as a
    background task (B1: the poll loop never waits for a pass — LLM calls are
    slow) and never overlapping the previous one (guard: the task's done()).
    A finished pass is harvested first: its outcomes become proposal cards on
    the LOOP thread (B2), its exception becomes log + ops event — the bot
    keeps running either way.

    Deadline (ENRICHMENT_PASS_TIMEOUT_S): a pass older than the deadline is
    declared stalled ONCE — ops event ``worker_pass_timeout`` (age, extractor),
    log, and the ``enrichment-worker`` heartbeat flips to phase=stalled so
    /ops health shows degraded — and the tick stops waiting for it. A
    to_thread body CANNOT be killed: while the stalled thread is still
    running no new pass is started (never two concurrent passes); the next
    due tick starts a pass only after the old thread has actually finished.
    """
    from brain.config import get_settings  # noqa: PLC0415

    task = getattr(core, "_enrich_task", None)  # stub cores (old tests) lack it
    if task is not None and task.done():
        core._enrich_task = None  # noqa: SLF001 (same module)
        try:
            core.proposals_changed(task.result())
        except Exception as exc:  # noqa: BLE001 — the worker must never kill the bot
            _record_pass_failure(exc)
        _worker_heartbeat("idle")
    stalled = getattr(core, "_enrich_stalled_task", None)
    if stalled is not None:
        if stalled.done():
            # the old thread finished: harvest it EXACTLY like a normal pass —
            # its children still earned their proposal cards (B2: loop thread
            # only); a failure is logged + ops-evented like any other
            core._enrich_stalled_task = None  # noqa: SLF001 (same module)
            try:
                core.proposals_changed(stalled.result())
            except Exception as exc:  # noqa: BLE001 — the worker must never kill the bot
                _record_pass_failure(exc)
            _worker_heartbeat("idle")
        else:
            return  # never a second concurrent pass (see docstring)
    settings = get_settings()
    if not settings.enrichment_worker_enabled:
        return
    task = getattr(core, "_enrich_task", None)
    if task is not None and not task.done():
        now = time.monotonic()
        age = now - getattr(core, "_enrich_started", now)
        if age > settings.enrichment_pass_timeout_s:
            _declare_pass_stalled(core, task, age, settings)
        return  # in-flight: a pass never overlaps the previous one
    now = time.monotonic()
    # 0.0 = never ran (monotonic is boot-relative: a just-rebooted host would
    # otherwise silently skip the first pass) — the interval gates only
    # CONSECUTIVE passes.
    last_run = getattr(core, "_enrich_last_run", 0.0)  # stub cores: treat as never
    if last_run and (now - last_run < settings.enrichment_worker_interval_s):
        return
    core._enrich_last_run = now  # noqa: SLF001 (same module)
    core._enrich_started = now  # noqa: SLF001 (same module)
    core._enrich_task = asyncio.create_task(  # noqa: SLF001 (same module)
        asyncio.to_thread(_run_enrichment_pass)
    )
    _worker_heartbeat("running")


def _worker_heartbeat(phase: str) -> None:
    """Publish the worker's phase for /ops health (idle/running/stalled).

    Heartbeats only fire on worker state transitions (a disabled worker
    writes nothing); the enrichment health check reads this row.
    """
    try:
        from brain.ops import heartbeat  # noqa: PLC0415

        heartbeat("enrichment-worker", {"phase": phase})
    except Exception:  # noqa: BLE001 — a health signal must never kill the tick
        logger.debug("telegram: enrichment-worker heartbeat failed", exc_info=True)


def _declare_pass_stalled(core: TelegramBot, task: Any, age: float, settings: Any) -> None:
    """Deadline passed for the in-flight pass: ops event ONCE, health degraded,
    and the tick stops waiting (the task moves to the stalled slot; while that
    thread is alive no new pass may start — see _enrichment_tick)."""
    timeout = settings.enrichment_pass_timeout_s
    record_event(
        "enrichment",
        "worker_pass_timeout",
        ok=False,
        detail={
            "error": f"pass running {age:.0f}s > deadline {timeout}s"[:200],
            "age_s": round(age),
            "deadline_s": timeout,
            "extractor": settings.enrichment_extractor,
        },
    )
    logger.error(
        "telegram: enrichment pass exceeded its %ss deadline (%.0fs) — worker "
        "stalled until the thread finishes; no new pass until then",
        timeout,
        age,
    )
    try:
        from brain.ops import heartbeat  # noqa: PLC0415

        heartbeat(
            "enrichment-worker",
            {
                "phase": "stalled",
                "age_s": round(age),
                "deadline_s": timeout,
                "extractor": settings.enrichment_extractor,
            },
        )
    except Exception:  # noqa: BLE001 — a health signal must never kill the tick
        logger.debug("telegram: enrichment-worker heartbeat failed", exc_info=True)
    core._enrich_stalled_task = task  # noqa: SLF001 (same module)
    core._enrich_task = None  # noqa: SLF001 (same module)


def _record_pass_failure(exc: BaseException) -> None:
    """One ops event per failed pass: error type + message, truncated."""
    record_event(
        "enrichment",
        "worker_pass_failed",
        ok=False,
        detail={"error": f"{type(exc).__name__}: {exc!s}"[:200]},
    )
    logger.exception("telegram: enrichment worker pass failed")


async def _shutdown_enrichment(tg: Any, core: TelegramBot, *, grace_s: float = 30.0) -> None:
    """Poll-loop shutdown: give an in-flight enrichment pass a short grace.

    A finished-but-unharvested pass still delivers its cards; a pass that
    exceeds the grace is cancelled (a to_thread worker cannot be
    interrupted, but the bot stops waiting) and logged + ops-evented —
    shutdown itself must always complete.
    """
    task = getattr(core, "_enrich_task", None)  # stub cores (old tests) lack it
    if task is None:
        return
    core._enrich_task = None  # noqa: SLF001 (same module)
    try:
        outcomes = await asyncio.wait_for(task, timeout=grace_s)
        core.proposals_changed(outcomes)
        for chat_id, reply in core.drain_outbound():
            await _send(tg, chat_id, reply)
    except TimeoutError:
        task.cancel()
        record_event(
            "enrichment",
            "worker_pass_cancelled_at_shutdown",
            ok=False,
            detail={"error": f"pass exceeded the {grace_s:.0f}s shutdown grace"[:200]},
        )
        logger.warning("telegram: enrichment pass cancelled at shutdown (grace %.0fs)", grace_s)
    except Exception as exc:  # noqa: BLE001 — shutdown must complete
        _record_pass_failure(exc)


async def _flush_bot_jobs(tg: Any, core: TelegramBot) -> None:
    """Send queued proposal cards + the throttled 48h pings; never stops the loop."""
    import time as _time  # noqa: PLC0415

    try:
        await _schedules_tick(tg, core)
    except Exception:  # noqa: BLE001 — isolated: a schedule failure never blocks the rest
        record_event("telegram", EVENT_ERROR, ok=False, detail={"error": "schedules tick failed"})
        logger.exception("telegram: schedules tick failed")
    try:
        await _enrichment_tick(tg, core)  # starts/harvests the pass FIRST...
        for chat_id, reply in core.drain_outbound():  # ...so its cards go out
            await _send(tg, chat_id, reply)  # in THIS flush
        now = _time.monotonic()
        if now - core._last_ping_check >= 60.0:  # noqa: SLF001 (same module)
            core._last_ping_check = now
            for chat_id, reply in core.due_pings():
                await _send(tg, chat_id, reply)
    except Exception:  # noqa: BLE001 — background jobs must never kill polling
        record_event("telegram", EVENT_ERROR, ok=False, detail={"error": "proposal flush failed"})
        logger.exception("telegram: proposal flush failed")


async def _schedules_tick(tg: Any, core: TelegramBot) -> None:
    """Claim due scheduled tasks and start each as its own background task.

    Called from every poll cycle (<= telegram_poll_timeout_s apart, so a
    task fires within about a minute of its time, and a ▶️ run-now pressed
    in this cycle is claimed by the flush right after it). The claim is a
    quick DB call, still done in a worker thread; the poll loop never waits
    for a run.
    """
    if not hasattr(core, "claim_scheduled"):  # stub cores in older tests
        return
    from brain.config import get_settings  # noqa: PLC0415

    settings = get_settings()
    timeout_s = float(settings.scheduled_task_timeout_s)
    claims = await asyncio.to_thread(
        core.claim_scheduled,
        max_late_s=float(settings.scheduled_task_max_late_s),
        stale_after_s=timeout_s + 120.0,
    )
    for claim in claims:
        core._sched_inflight.add(claim.task_id)  # noqa: SLF001 (same module)
        task = asyncio.create_task(_run_scheduled(tg, core, claim, timeout_s))
        core._sched_tasks.add(task)  # noqa: SLF001 (same module)
        task.add_done_callback(core._sched_tasks.discard)  # noqa: SLF001 (same module)


async def _run_scheduled(tg: Any, core: TelegramBot, claim: Claim, timeout_s: float) -> None:
    """One scheduled run: the ask in a worker thread (NEVER on the loop),
    bounded by ``timeout_s``, then the ⏰ card to every allowed chat.

    Never raises (a background task): a failure is logged + an ops event and
    touches nothing else. A timed-out thread cannot be killed: the run is
    recorded as an error now, its late result is dropped (the run row's
    first-writer-wins update), and the task stays in-flight — not claimed
    again — until that thread actually ends.
    """
    started = time.monotonic()
    worker = asyncio.ensure_future(asyncio.to_thread(core.run_scheduled, claim))

    def _released(future: Any) -> None:
        core._sched_inflight.discard(claim.task_id)  # noqa: SLF001 (same module)
        if not future.cancelled():
            future.exception()  # retrieved: no 'exception was never retrieved'

    worker.add_done_callback(_released)
    try:
        done, _ = await asyncio.wait({worker}, timeout=timeout_s)
        if done:
            outcome, lang = worker.result()
            reply = core.scheduled_reply(claim, outcome, lang)
            deliver, ok = outcome.recorded, outcome.status == "ok"
        else:
            deliver, lang = await asyncio.to_thread(core.finish_scheduled_timeout, claim, timeout_s)
            reply = core.scheduled_timeout_reply(claim, timeout_s, lang)
            ok = False
        record_event(
            "telegram",
            EVENT_SCHEDULE,
            ok=ok,
            detail={
                "task_id": str(claim.task_id),
                "trigger": claim.trigger,
                "timed_out": not done,
                "elapsed_s": round(time.monotonic() - started, 1),
            },
        )
        if not deliver:
            return
        for chat_id in core.scheduled_recipients():
            try:
                await _send(tg, chat_id, reply)
            except Exception:  # noqa: BLE001 — one chat failing never blocks the others
                logger.exception("telegram: scheduled task message failed")
    except Exception as exc:  # noqa: BLE001 — a background run must never escape
        record_event(
            "telegram",
            EVENT_SCHEDULE,
            ok=False,
            detail={"task_id": str(claim.task_id), "error": type(exc).__name__},
        )
        logger.exception("telegram: scheduled task %s failed", claim.task_id)


def _inline_keyboard(rows: list[list[tuple[str, str]]] | None) -> Any:
    """Convert core keyboard rows into a PTB InlineKeyboardMarkup (or None)."""
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup  # noqa: PLC0415

    if not rows:
        return None
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton(text=label, callback_data=data) for label, data in row]
            for row in rows
        ]
    )


async def _send(tg: Any, chat_id: int, reply: Reply, *, message_id: int | None = None) -> None:
    """Send a reply; edit in place when asked; never lose it to a parse rejection.

    On a Telegram BadRequest about parse entities the same text is resent
    with tags stripped and parse_mode=None, and an ops event records the
    fallback — a reply must never be silently lost.
    """
    if not reply.text:  # a toast-only callback result: nothing for the chat
        return
    text = reply.text[:TELEGRAM_REPLY_MAX_CHARS]
    markup = _inline_keyboard(reply.keyboard)
    if getattr(reply, "edit", False) and message_id is not None:
        edit_message = getattr(tg, "edit_message_text", None)
        if edit_message is not None:
            try:
                await edit_message(
                    chat_id=chat_id,
                    message_id=message_id,
                    text=text,
                    parse_mode="HTML",
                    reply_markup=markup,
                )
                return
            except Exception as exc:  # noqa: BLE001
                if _is_parse_entity_error(exc):
                    record_event(
                        "telegram",
                        EVENT_ERROR,
                        ok=False,
                        detail={"error": f"edit fallback: {type(exc).__name__}: {exc!s}"[:200]},
                    )
                    await edit_message(
                        chat_id=chat_id,
                        message_id=message_id,
                        text=_strip_html(text),
                        parse_mode=None,
                        reply_markup=markup,
                    )
                    return
                logger.debug("telegram: edit_message_text failed, sending new (%s)", exc)
    try:
        await tg.send_message(chat_id=chat_id, text=text, parse_mode="HTML", reply_markup=markup)
    except Exception as exc:  # noqa: BLE001  (fallback only for parse-entity errors)
        if not _is_parse_entity_error(exc):
            raise
        record_event(
            "telegram",
            EVENT_ERROR,
            ok=False,
            detail={"error": f"reply fallback: {type(exc).__name__}: {exc!s}"[:200]},
        )
        await tg.send_message(
            chat_id=chat_id,
            text=_strip_html(text),
            parse_mode=None,
            reply_markup=markup,
        )


def _is_parse_entity_error(exc: BaseException) -> bool:
    """True for Telegram BadRequest 'Can't parse entities' (any PTB version)."""
    return type(exc).__name__ == "BadRequest" and "parse entities" in str(exc).lower()


def _strip_html(text: str) -> str:
    """Unescape then drop tags: the plain-text fallback of an HTML reply."""
    return re.sub(r"<[^>]*>", "", html.unescape(text))


def _forward_info(message: Any) -> dict[str, Any] | None:
    """Extract forward metadata from message.forward_origin (PTB v20+).

    User origins carry the full ``sender_user`` — ``meta.sender`` prefers
    ``@username``, falls back to "First Last", then ``user:<id>``; the
    numeric id always lands in ``meta.sender_id``.
    """
    origin = getattr(message, "forward_origin", None)
    if origin is None:
        return None
    sender: str | None = None
    sender_id: int | None = None
    sender_user = getattr(origin, "sender_user", None)
    if sender_user is not None:
        sender_id = getattr(sender_user, "id", None)
        username = getattr(sender_user, "username", None)
        if username:
            sender = f"@{username}"
        else:
            first = str(getattr(sender_user, "first_name", "") or "").strip()
            last = str(getattr(sender_user, "last_name", "") or "").strip()
            sender = " ".join(part for part in (first, last) if part) or f"user:{sender_id}"
    elif getattr(origin, "sender_user_name", None):
        sender = str(origin.sender_user_name).strip()
    else:
        sender_chat = getattr(origin, "sender_chat", None)
        chat = sender_chat if sender_chat is not None else getattr(origin, "chat", None)
        if chat is not None:
            sender = (
                getattr(chat, "title", None)
                or getattr(chat, "username", None)
                or f"chat:{getattr(chat, 'id', '?')}"
            )
            sender_id = getattr(chat, "id", None)
    meta: dict[str, Any] = {"sender": sender, "date": getattr(origin, "date", None)}
    if sender_id is not None:
        meta["sender_id"] = sender_id
    return meta


async def _download_text(tg: Any, document: Any) -> str:
    """Download a document's bytes and decode them as text."""
    file = await tg.get_file(file_id=document.file_id)
    return bytes(file.download_as_bytearray()).decode("utf-8", errors="replace")


async def _download_bytes(tg: Any, file_id: str) -> bytes:
    """Download a file's raw bytes (attachments keep them verbatim)."""
    file = await tg.get_file(file_id=file_id)
    return bytes(file.download_as_bytearray())


async def _ask_with_typing(tg: Any, chat_id: int, core: TelegramBot, question: str) -> Reply:
    """Run the (blocking) ask in a thread, sending TYPING every 4 seconds."""
    stop = asyncio.Event()

    async def typing_loop() -> None:
        while not stop.is_set():
            try:
                await tg.send_chat_action(chat_id=chat_id, action="typing")
            except Exception:  # noqa: BLE001  (chat-action failures are cosmetic)
                logger.debug("telegram: send_chat_action failed", exc_info=True)
            try:
                await asyncio.wait_for(stop.wait(), timeout=TYPING_REFRESH_S)
            except TimeoutError:
                continue

    task = asyncio.create_task(typing_loop())
    try:
        return await asyncio.to_thread(core.ask, question)
    finally:
        stop.set()
        await asyncio.gather(task, return_exceptions=True)


async def _handle_attachment(
    tg: Any,
    chat_id: int,
    core: TelegramBot,
    message: Any,
    *,
    filename: str,
    mime: str | None,
    file_id: str,
) -> None:
    """Item 45: save the attachment + caption-parent, reply, read, report."""
    with core._services_factory() as services:
        if services.attachments is None:  # feature not wired (older factory)
            await _send(tg, chat_id, core.unsupported("Files other than .ics"))
            return
        caption = getattr(message, "caption", None) or f"[attachment: {filename}]"
        content = await _download_bytes(tg, file_id)
        parent = core.capture_attachment_parent(caption)
        att = services.attachments.capture(
            parent.id,
            content,
            filename,
            mime,
            "telegram",
            str(getattr(message, "message_id", "") or ""),
        )
        reading = i18n.t(core._language(), "att_reading")  # noqa: SLF001
        await _send(tg, chat_id, Reply(text=reading))
        attachment_id = att.id
    task = asyncio.create_task(_read_and_report(tg, chat_id, core, attachment_id))
    logger.info("telegram: attachment %s reading in background", attachment_id)
    del task  # fire and forget; failures are recorded on the row


async def _read_and_report(tg: Any, chat_id: int, core: TelegramBot, attachment_id: Any) -> None:
    """Read the attachment (budget/queue aware) and send a short summary."""
    try:
        status = await core.read_attachment(attachment_id)
        await _send(tg, chat_id, core.attachment_summary(attachment_id, status))
    except Exception as exc:  # noqa: BLE001 — never crash the bot on a follow-up
        logger.error("telegram: attachment follow-up failed: %s", exc, exc_info=True)
        record_event("telegram", "attachment_report", ok=False, detail={"error": str(exc)})


TOAST_MAX_CHARS = 200  # Telegram's answerCallbackQuery text limit


async def _answer_callback(query: Any, toast: str | None = None) -> None:
    """Acknowledge a button tap; ``toast`` shows as a transient notification
    (plain text: the HTML a notice may carry is stripped). A failed answer
    (e.g. a query older than Telegram's window) never blocks the reply."""
    answer = getattr(query, "answer", None)
    if answer is None:
        return
    try:
        if toast:
            text = _strip_html(toast).strip()[:TOAST_MAX_CHARS]
            await answer(text=text, show_alert=False)
        else:
            await answer()
    except Exception as exc:  # noqa: BLE001 — the ack is best-effort
        logger.debug("telegram: answerCallbackQuery failed (%s)", exc)


async def _handle_update(tg: Any, core: TelegramBot, update: Any) -> None:
    """Allowlist-check and dispatch one Telegram update."""
    user = update.effective_user
    if not core.is_allowed(getattr(user, "id", None)):
        logger.info("telegram: rejected update from user id %s", getattr(user, "id", "?"))
        record_event(
            "telegram",
            EVENT_REJECTED_USER,
            ok=False,
            detail={"user_id": getattr(user, "id", None)},
        )
        return  # never reply to strangers
    if update.callback_query is not None:
        query = update.callback_query
        try:
            reply = core.handle_callback(query.data or "", user.id)
        except Exception:
            await _answer_callback(query)  # stop the button's spinner anyway
            raise
        await _answer_callback(query, reply.toast)
        record_event("telegram", EVENT_CALLBACK, ok=True, detail={})
        message = getattr(query, "message", None)
        chat_id = getattr(message, "chat_id", None)
        if chat_id is not None and reply.text:
            await _send(tg, chat_id, reply, message_id=getattr(message, "message_id", None))
        return
    message = update.effective_message
    if message is None:
        return
    chat_id = message.chat_id
    document = getattr(message, "document", None)
    if document is not None:
        file_name = document.file_name or "document"
        is_ics = (
            file_name.lower().endswith(".ics")
            or getattr(document, "mime_type", "") == "text/calendar"
        )  # noqa: E501
        if is_ics:
            text = await _download_text(tg, document)
            await _send(tg, chat_id, core.capture_document(file_name, text))
        else:
            await _handle_attachment(
                tg,
                chat_id,
                core,
                message,
                filename=file_name,
                mime=getattr(document, "mime_type", None),
                file_id=document.file_id,
            )
        return
    photo = getattr(message, "photo", None)
    if photo:
        largest = photo[-1]  # Telegram lists sizes ascending; take the largest
        await _handle_attachment(
            tg,
            chat_id,
            core,
            message,
            filename="photo.jpg",
            mime="image/jpeg",
            file_id=largest.file_id,
        )
        return
    if getattr(message, "voice", None) or getattr(message, "video_note", None):
        await _send(tg, chat_id, core.unsupported("Voice and video"))
        return
    if message.text:
        command, _, argument = message.text.strip().partition(" ")
        if core.is_question(message.text):
            reply = await _ask_with_typing(tg, chat_id, core, message.text.strip())
        elif command == "/ask" and argument.strip():
            # Same worker-thread path as a "?" question: the LLM client uses
            # asyncio.run, which raises inside this running event loop.
            reply = await _ask_with_typing(tg, chat_id, core, argument.strip())
        else:
            reply = core.route(message.text, user.id, forward=_forward_info(message))
        await _send(tg, chat_id, reply)
    # stickers/other unsupported message types: nothing to capture yet


def _repo_lock_dir() -> Path:
    """<repo>/.dev — repo root from this file's location (editable install ⇒
    the repo; .dev is already gitignored). Fallback lock location."""
    return Path(__file__).resolve().parents[2] / ".dev"


def _cache_lock_dir() -> Path:
    """Machine-wide lock dir: ~/Library/Caches/second-brain (override via
    SECOND_BRAIN_CACHE_DIR for tests).

    NOTE: the env var must be checked for emptiness BEFORE wrapping in Path —
    Path('') is PosixPath('.') (truthy!), which would silently make the CWD
    the lock dir and downgrade the lock to per-checkout again.
    """
    env = os.environ.get("SECOND_BRAIN_CACHE_DIR")
    return Path(env) if env else Path.home() / "Library" / "Caches" / "second-brain"


def _lock_paths(token: str | None) -> list[Path]:
    """Lock locations, most specific first.

    The preferred lock is MACHINE-WIDE and per bot: the unexplained-second-
    poller class was a bot started from ANOTHER checkout with the same token;
    a per-repo lock cannot see it. The name carries only the token's numeric
    bot id (before ':'), never the secret half. The repo-local .dev path is
    the fallback when the cache dir is unwritable.
    """
    bot_id = (token or "").split(":", 1)[0] or "unknown"
    return [
        _cache_lock_dir() / f"telegram-{bot_id}.lock",
        _repo_lock_dir() / "telegram.lock",
    ]


def _acquire_instance_lock(token: str | None) -> tuple[Any, int | None]:
    """Exclusive, non-blocking flock for the single-instance guarantee.

    Tries :func:`_lock_paths` in order; the first usable path wins. Returns
    ``(handle, None)`` when this process holds the lock — keep the handle
    referenced for the process lifetime (closing it releases). Returns
    ``(None, other_pid)`` when another live instance holds it (pid best-effort
    read from the file for the error message; None when unreadable/stale).
    The lock dies with the process: no stale-lock cleanup is ever needed.
    Raises OSError only when NO location is usable (real misconfiguration).
    """
    last_error: OSError | None = None
    for lock_path in _lock_paths(token):
        try:
            lock_path.parent.mkdir(parents=True, exist_ok=True)
            handle = lock_path.open("a")
        except OSError as exc:  # unwritable dir: try the fallback
            last_error = exc
            continue
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            handle.close()
            other: int | None = None
            try:
                other = int(lock_path.read_text().strip() or 0) or None
            except (OSError, ValueError):
                other = None
            return None, other
        except OSError as exc:
            handle.close()
            last_error = exc
            continue
        # Append mode was used at open so a losing instance can still read
        # the holder's pid; now that we own the lock, rewrite it cleanly.
        handle.seek(0)
        handle.truncate()
        handle.write(f"{os.getpid()}\n")
        handle.flush()
        return handle, None
    raise last_error or OSError("no usable single-instance lock location")


def build_bot(settings: Settings) -> tuple[TelegramBot, Any]:
    """Assemble the core + a telegram Bot (requires the telegram extra)."""
    from telegram import Bot  # noqa: PLC0415

    core = TelegramBot(
        services_factory=_live_services_factory(),
        allowed_ids=settings.telegram_allowed_ids,
        timezone=ZoneInfo(settings.timezone),
    )
    token = unwrap_secret(settings.telegram_bot_token)
    assert token is not None, "run() refuses to build a bot without a token"
    return core, Bot(token=token)


def run(settings: Settings) -> int:
    """Run the long-poll loop; returns the process exit code.

    Installs a stderr root logging handler first (launchd jobs start with no
    logging config): without it, the startup line and errors below WARNING
    would vanish from StandardErrorPath.
    """
    # httpx INFO request lines contain the FULL bot token in the URL — they
    # must never reach any log (module security contract), whatever configured
    # logging, so the pin applies even when run() skipped basicConfig.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    if not logging.getLogger().handlers:
        logging.basicConfig(
            level=logging.INFO,
            stream=sys.stderr,
            format="%(asctime)s %(levelname)s %(name)s %(message)s",
        )
    if settings.telegram_bot_token is None:
        logger.error("telegram bot disabled (no token)")
        return EXIT_DISABLED
    token = unwrap_secret(settings.telegram_bot_token)
    try:
        lock, other_pid = _acquire_instance_lock(token)
    except OSError as exc:
        logger.error("telegram: cannot create the single-instance lock (%s)", exc)
        print(f"telegram: cannot create the single-instance lock: {exc}", file=sys.stderr)
        return EXIT_TRANSIENT
    if lock is None:
        message = "another bot instance is running"
        if other_pid is not None:
            message += f" (pid {other_pid})"
        logger.error("telegram: %s — refusing to double-poll (409 ahead)", message)
        print(f"telegram: {message}", file=sys.stderr)
        return EXIT_CONFIG
    try:
        core, tg = build_bot(settings)
        logger.info("telegram: long polling started")
        try:
            asyncio.run(poll_loop(tg, core, poll_timeout_s=settings.telegram_poll_timeout_s))
        except KeyboardInterrupt:
            logger.info("telegram: stopped")
            return EXIT_DISABLED
        except Exception as exc:  # noqa: BLE001  (CLI: map to the launchd contract)
            # PTB v22 renamed Unauthorized -> InvalidToken; treat both + Forbidden
            # (bot blocked) as configuration errors so launchd does not restart-loop.
            if type(exc).__name__ in ("Conflict", "Unauthorized", "InvalidToken", "Forbidden"):
                print(f"telegram: {type(exc).__name__}: {exc}", file=sys.stderr)
                return EXIT_CONFIG
            logger.exception("telegram: polling stopped with an error")
            print(f"telegram: stopped: {type(exc).__name__}: {exc}", file=sys.stderr)
            return EXIT_TRANSIENT
    except ImportError:
        print(
            "telegram: python-telegram-bot is not installed — run `pip install -e '.[telegram]'`",
            file=sys.stderr,
        )
        return EXIT_CONFIG
    finally:
        lock.close()  # release the single-instance lock on EVERY exit path
        from brain.api import deps  # noqa: PLC0415

        deps.close_shared_clients()
    return EXIT_TRANSIENT
