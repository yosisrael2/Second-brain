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

Exit codes (launchd service contract): token unset → 0 ("disabled"); 409
conflict or invalid token (Unauthorized) → 78 (EX_CONFIG, no restart loop);
transient failures → 1 (the service restarts).
"""

from __future__ import annotations

import asyncio
import html
import importlib
import logging
import secrets
import sys
import time
import uuid as uuid_mod
from collections.abc import Callable, Generator
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass
from typing import Any
from zoneinfo import ZoneInfo

from brain.config import Settings, unwrap_secret
from brain.schemas import ItemUpdate
from brain.services.ingest import IngestService
from brain.services.paste import parse as parse_paste

logger = logging.getLogger(__name__)

TELEGRAM_REPLY_MAX_CHARS = 4000  # Telegram messages cap at 4096
PENDING_TTL_S = 600  # inline buttons / WhatsApp choice stay valid 10 minutes
TYPING_REFRESH_S = 4

KIND_EMOJI = {"email": "📧", "whatsapp": "💬", "calendar": "📅", "note": "📝"}

# Compact callback-data prefixes (kept < 64 bytes; ids live in the registry).
CB_UNDO = "u"  # u:<token>              -> delete the items created by that message
CB_FILE = "f"  # f:<token>              -> status inbox -> filed
CB_DELETE = "d"  # d:<token>            -> delete items
CB_TYPE = "t"  # t:<token>              -> type submenu
CB_TYPE_APPLY = "ty"  # ty:<token>:<type>
CB_NEXT = "n"  # n:<offset>            -> next inbox card
CB_OPEN = "o"  # o:<token>:<index>     -> show a cited item card
CB_WA_JOIN = "w1"  # w1:<token>        -> save WhatsApp thread as one item
CB_WA_SPLIT = "wm"  # wm:<token>      -> save one item per message

BOT_COMMANDS = (
    ("ask", "Ask your memory a question"),
    ("search", "Semantic search"),
    ("review", "Review inbox items"),
    ("recent", "Show the last 10 items"),
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

    Assumes ``brain.ops`` exposes ``component_status()`` returning either a
    list of strings or ``(name, ok, summary)`` tuples — the cockpit contract
    behind GET /ops/health. Anything else counts as "not installed".
    """
    try:
        ops = importlib.import_module("brain.ops")
    except ImportError:
        return None
    fn = getattr(ops, "component_status", None)
    if fn is None:
        return None
    try:
        raw = fn()
    except Exception:  # noqa: BLE001  (a broken cockpit must not crash /status)
        logger.debug("telegram: ops.component_status raised", exc_info=True)
        return None
    lines: list[str] = []
    for entry in raw or []:
        if isinstance(entry, str):
            lines.append(entry)
        elif isinstance(entry, (tuple, list)) and len(entry) == 3:
            name, ok, summary = entry
            lines.append(f"{'🟢' if ok else '🔴'} {name}: {summary}")
    return lines or None


# --- core data model ----------------------------------------------------------


@dataclass(frozen=True)
class Reply:
    """What the adapter should send: HTML text + inline keyboard rows.

    ``keyboard`` rows are tuples of ``(label, callback_data)``; the adapter
    turns them into a PTB InlineKeyboardMarkup. Empty keyboard = plain text.
    """

    text: str
    keyboard: list[list[tuple[str, str]]] | None = None


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
        from brain.services.ask import AskService  # noqa: PLC0415
        from brain.services.items import ItemsService  # noqa: PLC0415
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
            yield BotServices(
                ingest=IngestService(repo, deps.get_classifier(), deps.get_item_indexer()),
                items=ItemsService(repo, deps.get_item_indexer()),
                search=search,
                ask=AskService(search, deps.get_llm_client()),
            )
        finally:
            session.close()

    return factory


def _esc(value: Any) -> str:
    """HTML-escape anything user- or parser-derived before it lands in a card."""
    return html.escape(str(value), quote=False)


def _preview(text: str, limit: int = 80) -> str:
    return _esc(" ".join(text.split())[:limit])


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
        token = self.registry.put(Pending(ids=tuple(item.id for item in items)))
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
            keyboard=[
                [("File", f"{CB_FILE}:{token}")],
                [("Type", f"{CB_TYPE}:{token}")],
                [("Delete", f"{CB_DELETE}:{token}")],
                [("Undo", f"{CB_UNDO}:{token}")],
            ],
        )

    def _message_count(self, parsed: Any) -> int:
        """Number of WhatsApp messages across the parsed chunks."""
        total = 0
        for chunk in parsed.chunks:
            total += len(chunk.meta.get("messages", [])) or max(chunk.meta.get("count", 1), 1)
        return total

    # -- cards -------------------------------------------------------------------

    def _capture_card(self, parsed: Any, items: list[Any]) -> str:
        emoji = KIND_EMOJI.get(parsed.kind, "📝")
        lines = [f"{emoji} Captured {len(items)} item(s) ({parsed.kind})"]
        for item in items:
            lines.append(self._item_card(item, indent="• "))
        return "\n".join(lines)

    def _item_card(self, item: Any, indent: str = "") -> str:
        """HTML card: kind emoji, extracted fields, classification, status."""
        emoji = KIND_EMOJI.get(item.source, "📝")
        lines = [
            f"{indent}{emoji} <b>{_esc(item.source)}</b> {str(item.id)[:8]}",
            f"{indent}    {_preview(item.raw_text, 200)}",
        ]
        meta = item.meta or {}
        for label, key in (("From", "from"), ("Subject", "subject"), ("Location", "location")):
            if meta.get(key):
                lines.append(f"{indent}    {label}: {_esc(meta[key])}")
        if meta.get("senders"):
            lines.append(f"{indent}    From: {_esc(', '.join(meta['senders']))}")
        if meta.get("sender"):
            lines.append(f"{indent}    From: {_esc(meta['sender'])}")
        when = self._when_of(item)
        if when:
            lines.append(f"{indent}    When: {_esc(when)}")
        lines.append(
            f"{indent}    type: {_esc(item.type or '—')} · "
            f"importance: {item.importance if item.importance is not None else '—'} · "
            f"actionability: {_esc(item.actionability or '—')} · "
            f"status: {_esc(item.status)}"
        )
        return "\n".join(lines)

    @staticmethod
    def _when_of(item: Any) -> str | None:
        if item.occurred_at is not None:
            return item.occurred_at.strftime("%Y-%m-%d %H:%M %Z").strip()
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
        for hit in hits:
            score = f" ({hit.score:.2f})" if hit.score is not None else ""
            lines.append(
                f"• {str(hit.item.id)[:8]}{_esc(score)} — {_preview(hit.item.raw_text, 80)}"
            )
        record_event(
            "telegram",
            EVENT_SEARCH,
            ok=True,
            detail={"item_ids": [str(hit.item.id) for hit in hits], "chars": len(query)},
        )
        return Reply(text="\n".join(lines))

    def ask(self, question: str) -> Reply:
        if not question:
            record_event("telegram", EVENT_ASK, ok=False, detail={"error": "empty question"})
            return Reply("Usage: /ask <question>")
        from brain.ai.ask import LLMError  # noqa: PLC0415

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
        record_event(
            "telegram",
            EVENT_ASK,
            ok=True,
            detail={"item_ids": list(result.cited_ids), "chars": len(result.answer)},
        )
        text = result.answer
        if result.model:
            text += f"\n\n— {_esc(result.model)}"
        return Reply(text=text, keyboard=self._open_buttons(result.cited_ids))

    def _open_buttons(self, cited_ids: list[str]) -> list[list[tuple[str, str]]]:
        valid: list[uuid_mod.UUID] = []
        for raw in cited_ids or []:
            try:
                valid.append(uuid_mod.UUID(str(raw)))
            except (ValueError, TypeError, AttributeError):
                continue
        if not valid:
            return []
        token = self.registry.put(Pending(ids=tuple(valid)))
        return [
            [(f"Open {str(item_id)[:8]}", f"{CB_OPEN}:{token}:{index}")]
            for index, item_id in enumerate(valid)
        ]

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
        rows = [
            [("File", f"{CB_FILE}:{token}")],
            [("Type", f"{CB_TYPE}:{token}")],
            [("Delete", f"{CB_DELETE}:{token}")],
        ]
        if offset + 1 < len(items):
            rows.append([("Next", f"{CB_NEXT}:{offset + 1}")])
        record_event(
            "telegram", EVENT_REVIEW, ok=True, detail={"item_ids": [str(item.id)], "chars": 0}
        )
        return Reply(
            text=f"📥 Inbox ({offset + 1}/{len(items)})\n{self._item_card(item)}",
            keyboard=rows,
        )

    def recent(self) -> Reply:
        """List the last 10 items as compact cards."""
        with self._services_factory() as services:
            items = list(
                services.items.list(item_type=None, project=None, status=None, limit=10, offset=0)
            )
        if not items:
            return Reply("No items yet — paste something!")
        lines = [f"🕘 Last {len(items)} item(s):"]
        for item in items:
            lines.append(f"• {str(item.id)[:8]} — {_preview(item.raw_text, 60)}")
        return Reply(text="\n".join(lines))

    def status(self) -> Reply:
        """Component health from the cockpit (no event: /status is not data ops)."""
        lines = _ops_status_lines()
        if lines is None:
            return Reply("cockpit not installed yet")
        all_ok = all(line.startswith("🟢") or ":" not in line for line in lines)
        header = "🟢 brain: healthy" if all_ok else "🟠 brain: degraded"
        return Reply(header + "\n" + "\n".join(_esc(line) for line in lines))

    def start(self) -> Reply:
        return Reply(
            "👋 Second Brain bot ready.\n"
            "Paste an email / WhatsApp thread / calendar event — I'll capture it.\n"
            "Any question ending in '?' goes to /ask.\n"
            "Commands: /search, /ask, /review, /recent, /status, /help"
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
            if prefix in (CB_TYPE_APPLY, CB_OPEN):
                token, _, arg = rest.partition(":")
            elif prefix in (CB_UNDO, CB_FILE, CB_DELETE, CB_TYPE, CB_WA_JOIN, CB_WA_SPLIT):
                token, arg = rest, ""
            else:
                return Reply("Unknown action — the button is stale or invalid.")
            if prefix == CB_OPEN:
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
        if prefix == CB_TYPE_APPLY:
            return self._apply_type(pending, arg)
        if prefix == CB_FILE:
            changed = self._update_items(pending, {"status": "filed"})
            return Reply(f"📁 Filed {changed} item(s).")
        if prefix == CB_OPEN:
            return self._open_card(pending, index)
        if prefix == CB_UNDO:
            return self._delete_items(pending, undo=True)
        return self._delete_items(pending, undo=False)  # CB_DELETE

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
        from brain.db.enums import ItemType  # noqa: PLC0415

        rows = [
            [(item_type.value, f"{CB_TYPE_APPLY}:{token}:{item_type.value}")]
            for item_type in ItemType
        ]
        return Reply("Pick a type:", keyboard=rows)

    def _apply_type(self, pending: Pending, type_value: str) -> Reply:
        from brain.db.enums import ItemType  # noqa: PLC0415

        try:
            item_type = ItemType(type_value)
        except ValueError:
            return Reply("Unknown type — the button is stale or invalid.")
        changed = self._update_items(pending, {"type": item_type})
        return Reply(f"✏️ Set type to {item_type.value} on {changed} item(s).")

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
        verb = "Undone — deleted" if undo else "Deleted"
        return Reply(f"{verb} {deleted} item(s).")


# --- polling adapter (needs python-telegram-bot) -----------------------------


async def poll_loop(tg: Any, core: TelegramBot, *, poll_timeout_s: int = 50) -> None:
    """Long-poll getUpdates and process updates until an exception escapes.

    ``tg`` is a python-telegram-bot ``Bot`` (or anything with async
    ``initialize``/``shutdown``/``set_my_commands``/``get_updates``/
    ``send_message``/``send_chat_action``/``get_file`` — fakes in tests).
    Registers bot commands once, heartbeats once per poll cycle; a 409
    conflict (another getUpdates consumer) or an Unauthorized token is
    recorded and stops the bot; per-update failures never stop the loop.
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
    try:
        while True:
            try:
                updates = await tg.get_updates(offset=offset, timeout=poll_timeout_s)
            except Conflict as exc:
                record_event(
                    "telegram",
                    "conflict",
                    ok=False,
                    detail={"error": f"{type(exc).__name__}: another getUpdates is running"[:200]},
                )
                logger.error("telegram: getUpdates conflict — stopping (%s)", exc)
                raise
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


async def _send(tg: Any, chat_id: int, reply: Reply) -> None:
    await tg.send_message(
        chat_id=chat_id,
        text=reply.text[:TELEGRAM_REPLY_MAX_CHARS],
        parse_mode="HTML",
        reply_markup=_inline_keyboard(reply.keyboard),
    )


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
        chat_id = getattr(getattr(query, "message", None), "chat_id", None)
        if chat_id is not None:
            await _send(tg, chat_id, reply)
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
    """Run the long-poll loop; returns the process exit code."""
    if settings.telegram_bot_token is None:
        logger.error("telegram bot disabled (no token)")
        return EXIT_DISABLED
    try:
        core, tg = build_bot(settings)
    except ImportError:
        print(
            "telegram: python-telegram-bot is not installed — run "
            "`pip install -e '.[telegram]'`",
            file=sys.stderr,
        )
        return EXIT_CONFIG
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
    finally:
        from brain.api import deps  # noqa: PLC0415

        deps.close_shared_clients()
    return EXIT_TRANSIENT
