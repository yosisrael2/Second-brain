"""Telegram bot: mobile capture channel over the in-process service layer.

``brain telegram`` long-polls Telegram (getUpdates, no webhook) and reuses
the SAME services as the API: paste-aware capture (:mod:`brain.services.paste`
via :meth:`IngestService.capture_parsed`), search, ask and inbox review.

Security: only user ids in ``TELEGRAM_ALLOWED_USER_IDS`` are processed —
anyone else gets no reply plus one INFO log line with their id. The token and
message bodies are never logged.

Ops integration (cockpit): :func:`heartbeat` and :func:`record_event` call
``brain.ops`` when that package exists (feature/cockpit) and degrade to no-ops
otherwise; neither ever raises. ``/status`` uses the same health service when
available and replies "cockpit not installed yet" when it is not.
"""

from __future__ import annotations

import asyncio
import importlib
import logging
import sys
from collections.abc import Callable, Generator
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass
from typing import Any
from zoneinfo import ZoneInfo

from brain.config import Settings, unwrap_secret
from brain.services.ingest import IngestService
from brain.services.paste import parse as parse_paste

logger = logging.getLogger(__name__)

TELEGRAM_REPLY_MAX_CHARS = 4000  # Telegram messages cap at 4096

# Kinds accepted by ops.record_event (cockpit contract).
EVENT_CAPTURE = "capture"
EVENT_ASK = "ask"
EVENT_SEARCH = "search"
EVENT_REVIEW = "review"
EVENT_CALLBACK = "callback"
EVENT_DOCUMENT = "document"
EVENT_REJECTED_USER = "rejected_user"
EVENT_ERROR = "error"


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


class TelegramBot:
    """Update-processing core: allowlist, commands, capture; PTB-agnostic.

    All handlers are synchronous and return the reply text — the polling
    adapter (:func:`poll_loop`) stays thin and the whole core is testable
    without network or python-telegram-bot.
    """

    def __init__(
        self,
        *,
        services_factory: ServicesFactory,
        allowed_ids: frozenset[int],
        timezone: ZoneInfo,
    ) -> None:
        self._services_factory = services_factory
        self._allowed_ids = allowed_ids
        self._timezone = timezone

    def is_allowed(self, user_id: int | None) -> bool:
        return user_id is not None and user_id in self._allowed_ids

    @property
    def allowed_count(self) -> int:
        """Allowlist size (for the ops heartbeat; no ids, no names)."""
        return len(self._allowed_ids)

    # -- commands / message handling -----------------------------------------

    def route(self, text: str, user_id: int) -> str:
        """Dispatch one allowed text message; returns the reply text."""
        stripped = text.strip()
        command, _, argument = stripped.partition(" ")
        argument = argument.strip()
        if command in ("/start",):
            return self.start()
        if command in ("/help",):
            return self.help_text()
        if command == "/status":
            return self.status()
        if command == "/search":
            return self.search(argument)
        if command == "/ask":
            return self.ask(argument)
        if command == "/review":
            return self.review()
        return self.capture(stripped)

    def capture(self, text: str) -> str:
        """Paste-aware capture: one item per parsed chunk (thread joins by default)."""
        parsed = parse_paste(text, tz=self._timezone)
        with self._services_factory() as services:
            items = services.ingest.capture_parsed(parsed, extra_meta={"channel": "telegram"})
        reply = f"✅ Captured {len(items)} item(s) ({parsed.kind}):"
        for item in items:
            preview = " ".join(item.raw_text.split())[:60]
            reply += f"\n• {str(item.id)[:8]} — {preview}"
        record_event(
            "telegram",
            EVENT_CAPTURE,
            ok=True,
            detail={"item_ids": [str(i.id) for i in items], "chars": len(text)},
        )
        return reply

    def capture_document(self, file_name: str, caption: str) -> str:
        """Capture a sent document as a reference item (name + caption)."""
        text = f"Document: {file_name}".strip()
        if caption:
            text += f"\n{caption}"
        parsed = parse_paste(text, tz=self._timezone)
        with self._services_factory() as services:
            items = services.ingest.capture_parsed(parsed, extra_meta={"channel": "telegram"})
        record_event(
            "telegram",
            EVENT_DOCUMENT,
            ok=True,
            detail={"item_ids": [str(i.id) for i in items], "chars": len(text)},
        )
        return f"📎 Saved {file_name} (id {str(items[0].id)[:8]})"

    def search(self, query: str) -> str:
        if not query:
            record_event("telegram", EVENT_SEARCH, ok=False, detail={"error": "empty query"})
            return "Usage: /search <words>"
        with self._services_factory() as services:
            hits = services.search.search(query, limit=5)
        if not hits:
            detail = {"item_ids": [], "chars": len(query)}
            record_event("telegram", EVENT_SEARCH, ok=True, detail=detail)
            return f"🔍 No results for '{query}'."
        reply = f"🔍 {len(hits)} result(s) for '{query}':"
        for hit in hits:
            preview = " ".join(hit.item.raw_text.split())[:80]
            score = f" ({hit.score:.2f})" if hit.score is not None else ""
            reply += f"\n• {str(hit.item.id)[:8]}{score} — {preview}"
        record_event(
            "telegram",
            EVENT_SEARCH,
            ok=True,
            detail={"item_ids": [str(h.item.id) for h in hits], "chars": len(query)},
        )
        return reply

    def ask(self, question: str) -> str:
        if not question:
            record_event("telegram", EVENT_ASK, ok=False, detail={"error": "empty question"})
            return "Usage: /ask <question>"
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
            return "⚠️ LLM unavailable; try again later."
        record_event(
            "telegram",
            EVENT_ASK,
            ok=True,
            detail={"item_ids": result.cited_ids, "chars": len(result.answer)},
        )
        return result.answer

    def review(self) -> str:
        """List inbox items as review candidates."""
        with self._services_factory() as services:
            items = services.items.list(
                item_type=None, project=None, status="inbox", limit=5, offset=0
            )
        if not items:
            record_event("telegram", EVENT_REVIEW, ok=True, detail={"item_ids": [], "chars": 0})
            return "📥 Inbox is empty."
        reply = "📥 Inbox (oldest-last, up to 5):"
        for item in items:
            preview = " ".join(item.raw_text.split())[:80]
            reply += f"\n• {str(item.id)[:8]} — {preview}"
        record_event(
            "telegram",
            EVENT_REVIEW,
            ok=True,
            detail={"item_ids": [str(i.id) for i in items], "chars": 0},
        )
        return reply

    def status(self) -> str:
        """Component health from the cockpit (no event: /status is not data ops)."""
        lines = _ops_status_lines()
        if lines is None:
            return "cockpit not installed yet"
        all_ok = all(line.startswith("🟢") or ":" not in line for line in lines)
        return ("🟢 brain: healthy" if all_ok else "🟠 brain: degraded") + "\n" + "\n".join(lines)

    def start(self) -> str:
        return (
            "👋 Second Brain bot ready.\n"
            "Paste an email / WhatsApp thread / calendar event — I'll capture it.\n"
            "Commands: /search, /ask, /review, /status, /help"
        )

    def help_text(self) -> str:
        return (
            "Commands:\n"
            "— paste any text: captured (email/WhatsApp/calendar detected)\n"
            "/search <words> — semantic search\n"
            "/ask <question> — ask the memory\n"
            "/review — show inbox items\n"
            "/status — component health (cockpit)\n"
            "/start — this greeting"
        )


# --- polling adapter (needs python-telegram-bot) -----------------------------


async def poll_loop(tg: Any, core: TelegramBot, *, poll_timeout_s: int = 50) -> None:
    """Long-poll getUpdates and process updates until an exception escapes.

    ``tg`` is a python-telegram-bot ``Bot`` (or anything with async
    ``initialize``/``shutdown``/``get_updates``/``send_message`` — fakes in
    tests). Heartbeat once per poll cycle; a 409 conflict (another
    getUpdates consumer) is recorded and stops the bot; per-update failures
    are recorded and never stop the loop.
    """
    from telegram.error import Conflict, NetworkError  # noqa: PLC0415

    offset: int | None = None
    last_update_id = 0
    await tg.initialize()
    try:
        while True:
            try:
                updates = await tg.get_updates(offset=offset, timeout=poll_timeout_s)
            except Conflict as exc:
                detail = {"error": f"{type(exc).__name__}: another getUpdates is running"[:200]}
                record_event("telegram", "conflict", ok=False, detail=detail)
                logger.error("telegram: getUpdates conflict — stopping (%s)", exc)
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
        record_event("telegram", EVENT_CALLBACK, ok=True, detail={})
        await update.callback_query.answer()
        return
    message = update.effective_message
    if message is None:
        return
    if message.document is not None:
        reply = core.capture_document(
            message.document.file_name or "document", message.caption or ""
        )
    elif message.text:
        reply = core.route(message.text, user.id)
    else:
        return  # stickers/photos/voice: nothing to capture yet
    chat_id = message.chat_id
    await tg.send_message(chat_id=chat_id, text=reply[:TELEGRAM_REPLY_MAX_CHARS])


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
    """Run the long-poll loop until Ctrl+C; returns a process exit code."""

    if settings.telegram_bot_token is None:
        print(
            "telegram: TELEGRAM_BOT_TOKEN is not set (get one from @BotFather); "
            "TELEGRAM_ALLOWED_USER_IDS is required with it — "
            "run `brain telegram --whoami-hint`",
            file=sys.stderr,
        )
        return 2
    try:
        core, tg = build_bot(settings)
    except ImportError:
        print(
            "telegram: python-telegram-bot is not installed — run "
            "`pip install -e '.[telegram]'`",
            file=sys.stderr,
        )
        return 2
    logger.info("telegram: long polling started (allowed_users=%d)", core.allowed_count)
    try:
        asyncio.run(poll_loop(tg, core))
    except KeyboardInterrupt:
        logger.info("telegram: stopped")
        return 0
    except Exception as exc:  # noqa: BLE001  (CLI: clean message, non-zero exit)
        logger.exception("telegram: polling stopped with an error")
        print(f"telegram: stopped: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    finally:
        from brain.api import deps  # noqa: PLC0415

        deps.close_shared_clients()
    return 1
