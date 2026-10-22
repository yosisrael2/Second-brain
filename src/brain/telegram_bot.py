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
import time
import uuid as uuid_mod
from collections.abc import Callable, Generator
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from brain.config import Settings, unwrap_secret
from brain.schemas import ItemUpdate
from brain.services.ask import AskResult
from brain.services.ingest import IngestService
from brain.services.paste import parse as parse_paste

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

BOT_COMMANDS = (
    ("ask", "Ask your memory a question"),
    ("search", "Semantic search"),
    ("review", "Review inbox items"),
    ("recent", "Show the last 10 items"),
    ("profile", "Your kids and remembered facts"),
    ("remember", "Remember a fact: /remember <text>"),
    ("status", "Component health"),
    ("help", "How to use the bot"),
)

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
    (in-place card updates) instead of sending a new one.
    """

    text: str
    keyboard: list[list[tuple[str, str]]] | None = None
    edit: bool = False


@dataclass(frozen=True)
class Pending:
    """State parked behind a short callback token (10-minute TTL)."""

    ids: tuple[uuid_mod.UUID, ...] = ()  # target items for the action
    text: str | None = None  # WhatsApp paste waiting for the join/split choice


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
        from brain.services.ask import AskService  # noqa: PLC0415
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
                ask=AskService(
                    search,
                    deps.get_llm_client(),
                    memory_block=memory.compact_block,
                ),
                memory=memory,
            )
        finally:
            session.close()

    return factory


def _esc(value: Any) -> str:
    """HTML-escape anything user- or parser-derived before it lands in a card."""
    return html.escape(str(value), quote=False)


def _one_line(text: str, limit: int) -> str:
    """Collapse to one line, cut at ``limit`` with an ellipsis when cut."""
    flat = " ".join(str(text).split())
    if len(flat) > limit:
        flat = flat[:limit].rstrip() + "…"
    return _esc(flat)

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
    ) -> None:
        self._services_factory = services_factory
        self._allowed_ids = allowed_ids
        self._timezone = timezone
        self.registry = registry or PendingRegistry()

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

    def route(
        self, text: str, user_id: int, *, forward: dict[str, Any] | None = None
    ) -> Reply:
        """Dispatch one allowed text message; returns the reply."""
        stripped = text.strip()
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
        if command == "/review":
            return self.review()
        if command == "/recent":
            return self.recent()
        if command == "/profile":
            return self.profile()
        if command == "/remember":
            return self.remember(argument)
        return self.capture(stripped, forward=forward)

    # -- capture ----------------------------------------------------------------

    def capture(self, text: str, *, forward: dict[str, Any] | None = None) -> Reply:
        """Paste-aware capture; WhatsApp threads ask join-vs-split first."""
        parsed = parse_paste(text, tz=self._timezone)
        if parsed.kind == "whatsapp" and self._message_count(parsed) > 1:
            token = self.registry.put(Pending(text=text))
            count = self._message_count(parsed)
            return Reply(
                text=(
                    f"💬 WhatsApp thread with {count} messages — save as "
                    "one item or separately?"
                ),
                keyboard=[
                    [("Save as 1 item", f"{CB_WA_JOIN}:{token}")],
                    [("Save separately", f"{CB_WA_SPLIT}:{token}")],
                ],
            )
        return self._save_parsed(parsed, forward=forward)

    def capture_document(self, file_name: str, text: str) -> Reply:
        """Capture an .ics document's contents as a calendar item."""
        parsed = parse_paste(text or "", kind_hint="calendar", tz=self._timezone)
        reply = self._save_parsed(parsed)
        record_event(
            "telegram", EVENT_DOCUMENT, ok=True, detail={"chars": len(text or "")}
        )
        return Reply(
            text=f"📎 {reply.text}\nfile: {_esc(file_name)}", keyboard=reply.keyboard
        )

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
        deduped = bool(items) and all(
            getattr(item, "deduplicated", False) for item in items
        )
        row: list[tuple[str, str]] = []
        if not filed:
            row.append(("✅ File", f"{CB_FILE}:{token}"))
        row.append(("🏷 Type", f"{CB_TYPE}:{token}"))
        if deduped:
            row.append(("🗑 Delete original", f"{CB_DELETE}:{token}"))
            return [row]
        row += [
            ("🗑 Delete", f"{CB_DELETE}:{token}"),
            ("↩️ Undo", f"{CB_UNDO}:{token}"),
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
        if items and all(getattr(item, "deduplicated", False) for item in items):
            header = f"ℹ️ <b>Already saved</b> · {self._kind_label(items)}"
        else:
            header = f"✅ <b>Saved</b> · {self._kind_label(items)}"
        if len(items) > 1:
            header += f" · {len(items)} items"
        return f"{notice}\n{header}" if notice else header

    def _capture_card(self, parsed: Any, items: list[Any]) -> str:
        return self._card_header(items) + "\n" + "\n".join(self._item_card(item) for item in items)

    def _kind_label(self, items: list[Any]) -> str:
        """Source kind for email/whatsapp/calendar; classified type otherwise."""
        item = items[0] if items else None
        source = str(getattr(item, "source", "note") or "note")
        if source in ("email", "whatsapp", "calendar"):
            return f"{KIND_EMOJI.get(source, '📝')} {KIND_NAME.get(source, 'Note')}"
        item_type = str(getattr(item, "type", "") or "note")
        return f"{TYPE_EMOJI.get(item_type, '📝')} {KIND_NAME.get(item_type, 'Note')}"

    def _item_card(self, item: Any, indent: str = "") -> str:
        """Compact HTML card: text preview, existing fields, rating, status."""
        lines = [f"{indent}{_one_line(item.raw_text, 200)}"]
        meta = item.meta or {}
        fields: list[str] = []
        who = meta.get("from") or meta.get("sender")
        if not who and meta.get("senders"):
            who = ", ".join(meta["senders"])
        if who:
            fields.append(f"From: {_one_line(who, 60)}")
        if meta.get("subject"):
            fields.append(f"Subject: {_one_line(meta['subject'], 60)}")
        when = self._when_of(item)
        if when:
            fields.append(f"When: {_esc(when)}")
        if meta.get("location"):
            fields.append(f"Where: {_one_line(meta['location'], 60)}")
        lines += [f"{indent}{field}" for field in fields[:3]]
        rating = " · ".join(
            part
            for part in (
                _stars(item.importance),
                ACTIONABILITY_LABEL.get(str(item.actionability or ""), "—"),
            )
            if part
        )
        if rating:
            lines.append(f"{indent}{rating}")
        if item.status == "needs_review":
            percent = int(round((item.confidence or 0) * 100))
            lines.append(f"{indent}🔎 Needs review ({percent}%)")
        return "\n".join(lines)

    def _when_of(self, item: Any) -> str | None:
        """When-line in the USER'S zone (occurred_at is aware UTC)."""
        if item.occurred_at is not None:
            local = item.occurred_at.astimezone(self._timezone)
            return f"{local:%a} {local.day} {local:%b} {local:%H:%M}"
        meta = item.meta or {}
        for key in ("start", "date"):
            if meta.get(key):
                return str(meta[key])
        return None

    # -- commands ------------------------------------------------------------------

    def search(self, query: str) -> Reply:
        if not query:
            record_event("telegram", EVENT_SEARCH, ok=False, detail={"error": "empty query"})
            return Reply("Usage: /search <words>")
        with self._services_factory() as services:
            hits = services.search.search(query, limit=5)
        if not hits:
            detail = {"item_ids": [], "chars": len(query)}
            record_event("telegram", EVENT_SEARCH, ok=True, detail=detail)
            return Reply(f"🔍 No results for '{_esc(query)}'.")
        lines = [f"🔍 {len(hits)} result(s) for '{_esc(query)}':"]
        for position, hit in enumerate(hits, start=1):
            emoji = KIND_EMOJI.get(hit.item.source, "📝")
            when = self._short_date(hit.item)
            suffix = f" · {when}" if when else ""
            lines.append(f"{position}. {emoji} {_one_line(hit.item.raw_text, 60)}{suffix}")
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

    def ask(self, question: str) -> Reply:
        if not question:
            record_event("telegram", EVENT_ASK, ok=False, detail={"error": "empty question"})
            return Reply("Usage: /ask <question>")
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
            return Reply("⚠️ LLM unavailable; try again later.")
        elapsed = time.monotonic() - started
        record_event(
            "telegram",
            EVENT_ASK,
            ok=True,
            detail={"item_ids": list(result.cited_ids), "chars": len(result.answer)},
        )
        return self._ask_card(result, elapsed)

    def _ask_card(self, result: AskResult, elapsed_s: float) -> Reply:
        """Format an answer: 🧠 header, refs instead of raw uuids, 📚 sources."""
        order = {item_id: n for n, item_id in enumerate(result.cited_ids, start=1)}
        body = _esc(result.answer)
        for item_id, n in order.items():
            body = body.replace(f"[{item_id}]", _ref(n))
        lines = ["🧠 <b>Answer</b>", body]
        if order:  # nothing cited -> plain answer, no sources block
            by_id = {str(hit.item.id): hit for hit in result.sources}
            source_lines: list[str] = []
            for item_id, n in order.items():
                hit = by_id.get(item_id)
                if hit is None:
                    continue
                emoji = KIND_EMOJI.get(hit.item.source, "📝")
                when = self._short_date(hit.item)
                suffix = f" · {when}" if when else ""
                source_lines.append(f"{n}. {emoji} {_one_line(hit.item.raw_text, 60)}{suffix}")
            if source_lines:
                lines += ["", "📚 <b>Sources</b>", *source_lines]
        if result.model:
            lines += ["", f"<i>{_esc(result.model)} · {elapsed_s:.1f}s</i>"]
        return Reply(text="\n".join(lines), keyboard=self._open_buttons(result.cited_ids))

    def _short_date(self, item: Any) -> str | None:
        """Day + short month in the bot's timezone (source-line suffix)."""
        instant = item.occurred_at or item.created_at
        if instant is None:
            return None
        local = instant.astimezone(self._timezone)
        return f"{local.day} {local.strftime('%b')}"

    def _open_buttons(self, cited_ids: list[str]) -> list[list[tuple[str, str]]]:
        """One row of 📎 buttons (max 5), numbered like the source list."""
        valid: list[uuid_mod.UUID] = []
        for raw in cited_ids or []:
            try:
                valid.append(uuid_mod.UUID(str(raw)))
            except (ValueError, TypeError, AttributeError):
                continue
        if not valid:
            return []
        valid = valid[:5]
        token = self.registry.put(Pending(ids=tuple(valid)))
        return [[(f"📎 {n}", f"{CB_OPEN}:{token}:{n - 1}") for n in range(1, len(valid) + 1)]]

    def review(self, offset: int = 0) -> Reply:
        """Walk the inbox one card at a time (File / Type / Delete / Next)."""
        with self._services_factory() as services:
            items = list(
                services.items.list(
                    item_type=None, project=None, status="inbox", limit=10, offset=0
                )
            )
        if not items:
            record_event("telegram", EVENT_REVIEW, ok=True, detail={"item_ids": [], "chars": 0})
            return Reply("📥 Inbox is empty.")
        if not 0 <= offset < len(items):
            return Reply("📥 End of inbox.")
        item = items[offset]
        token = self.registry.put(Pending(ids=(item.id,)))
        row = [
            ("✅ File", f"{CB_FILE}:{token}"),
            ("🏷 Type", f"{CB_TYPE}:{token}"),
            ("🗑 Delete", f"{CB_DELETE}:{token}"),
        ]
        rows = [row]
        if offset + 1 < len(items):
            rows.append([("Next ▶️", f"{CB_NEXT}:{offset + 1}")])
        record_event(
            "telegram", EVENT_REVIEW, ok=True, detail={"item_ids": [str(item.id)], "chars": 0}
        )
        return Reply(
            text=f"📥 Inbox ({offset + 1}/{len(items)})\n{self._item_card(item)}",
            keyboard=rows,
            edit=offset > 0,
        )

    def recent(self) -> Reply:
        """List the last 10 items as compact rows with an Open button each."""
        with self._services_factory() as services:
            items = list(
                services.items.list(item_type=None, project=None, status=None, limit=10, offset=0)
            )
        if not items:
            return Reply("No items yet — paste something!")
        lines = [f"🕘 Last {len(items)} item(s):"]
        for position, item in enumerate(items, start=1):
            emoji = KIND_EMOJI.get(item.source, "📝")
            when = self._short_date(item)
            suffix = f" · {when}" if when else ""
            lines.append(f"{position}. {emoji} {_one_line(item.raw_text, 60)}{suffix}")
        return Reply(
            text="\n".join(lines),
            keyboard=self._open_buttons([str(item.id) for item in items]),
        )

    # -- memory (profile + remember) -------------------------------------------

    _KID_FACT_ORDER = ("gender", "school", "grade", "class", "teacher")

    def _memory_service(self) -> Any:
        """The MemoryService for one update, or None when PR 0 is absent."""
        with self._services_factory() as services:
            return getattr(services, "memory", None)

    def profile(self) -> Reply:
        """Kids first (gender/class/grade/teacher), then remembered facts.

        Every fact line gets a numbered ✖ button (soft delete; history kept).
        Fact ids ride in the pending registry so callback data stays < 64B.
        """
        memory = self._memory_service()
        if memory is None:
            return Reply("Memory layer is not installed yet.")
        overview = memory.overview(status="confirmed", q=None)
        persons = [e for e in overview.get("entities", []) if e.get("kind") == "person"]
        # kids (persons carrying a kid-fact) first, then the rest by name
        persons.sort(key=lambda e: (0 if self._is_kid(e) else 1, e.get("name", "")))
        lines = ["👤 <b>Profile</b>"]
        fact_ids: list[str] = []
        for entity in persons:
            facts = entity.get("facts", [])
            if entity.get("name") == "me":
                if facts:
                    lines.append("")
                    lines.append("🧠 <b>Remembered</b>")
                for fact in facts:
                    fact_ids.append(str(fact["id"]))
                    lines.append(f"• {_esc(fact.get('predicate', 'note'))}: {_one_line(fact.get('value'), 80)}")
                continue
            lines.append("")
            lines.append(f"🧒 <b>{_esc(entity.get('name', '?'))}</b>")
            by_predicate = {f["predicate"]: f for f in facts}
            summary = " · ".join(
                str(by_predicate[p]["value"])
                for p in self._KID_FACT_ORDER
                if p in by_predicate and by_predicate[p].get("value")
            )
            if summary:
                lines.append(_esc(summary))
            for predicate, fact in by_predicate.items():
                fact_ids.append(str(fact["id"]))
                lines.append(f"{_esc(predicate)}: {_one_line(fact.get('value'), 80)}")
        if not fact_ids:
            return Reply(
                "👤 Profile is empty.\nTeach me about your kids, e.g.\n"
                "/remember Rona is a girl in ה' ספיר (grade 5, teacher Rinat)"
            )
        return Reply(text="\n".join(lines), keyboard=self._memory_remove_buttons(fact_ids))

    @staticmethod
    def _is_kid(entity: dict) -> bool:
        """A person carrying at least one kid-fact (grade/class/gender/teacher)."""
        kid_predicates = {"gender", "grade", "class", "teacher"}
        return any(f.get("predicate") in kid_predicates for f in entity.get("facts", []))

    def _memory_remove_buttons(self, fact_ids: list[str]) -> list[list[tuple[str, str]]]:
        """Numbered ✖ buttons (rows of 4), numbered like the profile lines."""
        token = self.registry.put(
            Pending(ids=tuple(uuid_mod.UUID(fid) for fid in fact_ids))
        )
        buttons = [
            (f"✖ {n}", f"{CB_MEM_DEL}:{token}:{n - 1}")
            for n in range(1, len(fact_ids) + 1)
        ]
        return [buttons[i : i + 4] for i in range(0, len(buttons), 4)]

    def _remove_profile_fact(self, pending: Pending, index: int) -> Reply:
        """Soft-delete one profile fact, then re-render the card in place."""
        from brain.services.memory import FactNotFound  # noqa: PLC0415

        if not 0 <= index < len(pending.ids):
            return Reply("Unknown action — the button is stale or invalid.")
        with self._services_factory() as services:
            memory = getattr(services, "memory", None)
            if memory is None:
                return Reply("Memory layer is not installed yet.")
            try:
                memory.delete_fact(pending.ids[index])
            except FactNotFound:
                return Reply("⌛ That fact was already removed.")
        return self.profile_reply(notice="🗑 Removed (history kept).")

    def profile_reply(self, notice: str | None = None) -> Reply:
        """Re-render the profile card (edit-in-place after a removal)."""
        reply = self.profile()
        text = f"{notice}\n{reply.text}" if notice else reply.text
        return Reply(text=text, keyboard=reply.keyboard, edit=True)

    def remember(self, text: str) -> Reply:
        """Store a free-text fact on the 'me' entity (confirmed)."""
        if not text:
            return Reply("Usage: /remember <fact> — e.g. /remember Itamar plays basketball")
        memory = self._memory_service()
        if memory is None:
            return Reply("Memory layer is not installed yet.")
        with self._services_factory() as services:
            fact = services.memory.remember(text)
        record_event(
            "telegram",
            EVENT_MEMORY,
            ok=True,
            detail={"fact_id": str((fact or {}).get("id", "")), "chars": len(text)},
        )
        return Reply(f"🧠 <b>Remembered</b>\n{_one_line(text, 300)}")

    def status(self) -> Reply:
        """Component health from the cockpit (no event: /status is not data ops)."""
        lines = _ops_status_lines()
        if lines is None:
            return Reply("cockpit not installed yet")
        emoji_lines = [line for line in lines if line.startswith(("🟢", "🟡", "🔴", "⚪"))]
        if emoji_lines and len(emoji_lines) == len(lines):
            if any(line.startswith("🔴") for line in lines):
                header = "🔴 brain: down"
            elif any(not line.startswith(("🟢", "⚪")) for line in lines):
                header = "🟠 brain: degraded"
            else:
                header = "🟢 brain: healthy"
        else:  # legacy plain-string component_status: only ok/not-ok known
            all_ok = all(line.startswith("🟢") or ":" not in line for line in lines)
            header = "🟢 brain: healthy" if all_ok else "🟠 brain: degraded"
        return Reply(header + "\n" + "\n".join(lines))

    def start(self) -> Reply:
        return Reply(
            "👋 Second Brain bot ready.\n"
            "Paste an email / WhatsApp thread / calendar event — I'll capture it.\n"
            "Any question ending in '?' goes to /ask.\n"
            "Commands: /search, /ask, /review, /recent, /profile, /remember, /status, /help"
        )

    def help_text(self) -> Reply:
        return Reply(
            "Commands:\n"
            "— paste any text: captured (email/WhatsApp/calendar detected)\n"
            "— any question ending in '?': answered from the memory\n"
            "/search <words> — semantic search\n"
            "/ask <question> — ask the memory\n"
            "/review — walk the inbox one card at a time\n"
            "/recent — last 10 items\n"
            "/profile — your kids and remembered facts (with remove buttons)\n"
            "/remember <fact> — remember something about your world\n"
            "/status — component health (cockpit)\n"
            "/start — this greeting"
        )

    def unsupported(self, what: str) -> Reply:
        return Reply(f"😕 {_esc(what)} are not supported yet — paste the text instead.")

    # -- callbacks -----------------------------------------------------------------

    def handle_callback(self, data: str) -> Reply:
        """Dispatch an inline-button press; stale/unknown → graceful reply."""
        prefix, _, rest = data.partition(":")
        try:
            if prefix == CB_NEXT:
                return self.review(int(rest))
            if prefix in (CB_TYPE_APPLY, CB_OPEN, CB_MEM_DEL):
                token, _, arg = rest.partition(":")
            elif prefix in (CB_UNDO, CB_FILE, CB_DELETE, CB_TYPE, CB_WA_JOIN, CB_WA_SPLIT):
                token, arg = rest, ""
            else:
                return Reply("Unknown action — the button is stale or invalid.")
            if prefix in (CB_OPEN, CB_MEM_DEL):
                index = int(arg)
        except ValueError:
            return Reply("Unknown action — the button is stale or invalid.")
        pending = self.registry.get(token)
        if pending is None:
            return Reply("⌛ That button expired (valid 10 minutes) — send the message again.")
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
            return self._card_reply(pending, notice=f"📁 Filed {changed} item(s).")
        if prefix == CB_OPEN:
            return self._open_card(pending, index)
        if prefix == CB_MEM_DEL:
            return self._remove_profile_fact(pending, index)
        if prefix == CB_UNDO:
            return self._delete_items(pending, undo=True)
        return self._delete_items(pending, undo=False)  # CB_DELETE

    def _card_reply(self, pending: Pending, notice: str | None = None) -> Reply:
        """Re-render the capture card for the pending items (edit-in-place)."""
        from brain.services.items import ItemNotFound  # noqa: PLC0415

        items = []
        with self._services_factory() as services:
            for item_id in pending.ids:
                try:
                    items.append(services.items.get(item_id))
                except ItemNotFound:
                    continue
        if not items:
            return Reply("🗑 Deleted", edit=True)
        text = self._card_header(items, notice=notice) + "\n" + "\n".join(
            self._item_card(item) for item in items
        )
        return Reply(text=text, keyboard=self._capture_buttons(items), edit=True)

    def _open_card(self, pending: Pending, index: int) -> Reply:
        """Show one cited item's card (the Open button under /ask answers)."""
        from brain.services.items import ItemNotFound  # noqa: PLC0415

        if not 0 <= index < len(pending.ids):
            return Reply("Unknown action — the button is stale or invalid.")
        with self._services_factory() as services:
            try:
                item = services.items.get(pending.ids[index])
            except ItemNotFound:
                return Reply("⌛ That item was deleted.")
        return Reply(self._item_card(item))

    def _save_whatsapp_choice(self, pending: Pending, *, split: bool) -> Reply:
        parsed = parse_paste(pending.text or "", split=split, tz=self._timezone)
        return self._save_parsed(parsed)

    def _type_menu(self, token: str) -> Reply:
        """Type picker: emoji labels in rows of up to 3 + a back row."""
        from brain.db.enums import ItemType  # noqa: PLC0415

        buttons = [
            (
                f"{TYPE_EMOJI.get(t.value, '🏷')} {KIND_NAME.get(t.value, t.value)}",
                f"{CB_TYPE_APPLY}:{token}:{t.value}",
            )
            for t in ItemType
        ]
        grid = [buttons[i : i + 3] for i in range(0, len(buttons), 3)]
        return Reply("Pick a type:", keyboard=grid + [[("« Back", f"{CB_BACK}:{token}")]])

    def _apply_type(self, pending: Pending, type_value: str) -> Reply:
        from brain.db.enums import ItemType  # noqa: PLC0415

        try:
            item_type = ItemType(type_value)
        except ValueError:
            return Reply("Unknown type — the button is stale or invalid.")
        self._update_items(pending, {"type": item_type})
        label = KIND_NAME.get(item_type.value, item_type.value)
        return self._card_reply(pending, notice=f"✏️ Type: {label}")

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
        verb = "↩️ Undone" if undo else "🗑 Deleted"
        return Reply(f"{verb} {deleted} item(s).", edit=True)


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
    commands = [BotCommand(name, description) for name, description in BOT_COMMANDS]
    try:
        await tg.set_my_commands(commands)
    except Exception:  # noqa: BLE001  (command registration is cosmetic)
        logger.debug("telegram: set_my_commands failed", exc_info=True)
    offset: int | None = None
    last_update_id = 0
    conflict_until: float | None = None
    backoff = conflict_backoff_s
    try:
        while True:
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
                await asyncio.sleep(3)
                continue
            # A successful poll means the other poller is gone: fresh window.
            conflict_until = None
            backoff = conflict_backoff_s
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
    finally:
        shutdown = getattr(tg, "shutdown", None)
        if shutdown is not None:
            await shutdown()


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
    text = reply.text[:TELEGRAM_REPLY_MAX_CHARS]
    markup = _inline_keyboard(reply.keyboard)
    if getattr(reply, "edit", False) and message_id is not None:
        edit_message = getattr(tg, "edit_message_text", None)
        if edit_message is not None:
            try:
                await edit_message(
                    chat_id=chat_id, message_id=message_id, text=text,
                    parse_mode="HTML", reply_markup=markup,
                )
                return
            except Exception as exc:  # noqa: BLE001
                if _is_parse_entity_error(exc):
                    record_event(
                        "telegram", EVENT_ERROR, ok=False,
                        detail={"error": f"edit fallback: {type(exc).__name__}: {exc!s}"[:200]},
                    )
                    await edit_message(
                        chat_id=chat_id, message_id=message_id,
                        text=_strip_html(text), parse_mode=None, reply_markup=markup,
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
        answer = getattr(query, "answer", None)
        if answer is not None:
            await answer()
        reply = core.handle_callback(query.data or "")
        record_event("telegram", EVENT_CALLBACK, ok=True, detail={})
        message = getattr(query, "message", None)
        chat_id = getattr(message, "chat_id", None)
        if chat_id is not None:
            await _send(tg, chat_id, reply, message_id=getattr(message, "message_id", None))
        return
    message = update.effective_message
    if message is None:
        return
    chat_id = message.chat_id
    document = getattr(message, "document", None)
    if document is not None:
        file_name = document.file_name or "document"
        is_ics = file_name.lower().endswith(".ics") or getattr(document, "mime_type", "") == "text/calendar"  # noqa: E501
        if is_ics:
            text = await _download_text(tg, document)
            await _send(tg, chat_id, core.capture_document(file_name, text))
        else:
            await _send(tg, chat_id, core.unsupported("Files other than .ics"))
        return
    if (
        getattr(message, "photo", None)
        or getattr(message, "voice", None)
        or getattr(message, "video_note", None)
    ):
        await _send(tg, chat_id, core.unsupported("Photos, voice and video"))
        return
    if message.text:
        if core.is_question(message.text):
            reply = await _ask_with_typing(tg, chat_id, core, message.text.strip())
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
            asyncio.run(poll_loop(tg, core))
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
            "telegram: python-telegram-bot is not installed — run "
            "`pip install -e '.[telegram]'`",
            file=sys.stderr,
        )
        return EXIT_CONFIG
    finally:
        lock.close()  # release the single-instance lock on EVERY exit path
        from brain.api import deps  # noqa: PLC0415

        deps.close_shared_clients()
    return EXIT_TRANSIENT
