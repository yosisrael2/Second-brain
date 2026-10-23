"""``brain`` command line interface (entry point: ``brain = brain.cli:main``)."""

from __future__ import annotations

import argparse
import logging
import sys
import tempfile
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from brain.ai.embedder import Embedder, EmbedderError
from brain.ai.gmail import GMAIL_SCOPE, RestGmailClient
from brain.ai.vectorstore import QdrantVectorStore, VectorStore, VectorStoreError
from brain.config import Settings, get_settings, unwrap_secret
from brain.db.session import SessionLocal
from brain.factories import build_embedder
from brain.repositories.items import ItemsRepository
from brain.services.dedupe import content_fingerprint
from brain.services.email_poller import EmailPoller, heartbeat_email
from brain.services.eval_extraction import (
    DEFAULT_TITLE_THRESHOLD,
    EvalError,
    evaluate,
    format_aggregate,
    load_email_texts,
    load_jsonl,
    write_report,
)
from brain.services.indexing import QdrantItemIndexer
from brain.services.ingest import IngestService
from brain.services.items import ItemsRepositoryProto
from brain.services.keychain import GMAIL_ACCOUNT, Keychain
from brain.services.paste import _calendar_one_liner

logger = logging.getLogger(__name__)


def main(argv: Sequence[str] | None = None) -> int:
    """Run the brain CLI; returns a process exit code."""
    parser = argparse.ArgumentParser(prog="brain", description="Second Brain maintenance commands")
    subparsers = parser.add_subparsers(dest="command", required=True)
    reindex = subparsers.add_parser("reindex", help="Rebuild Qdrant vectors from Postgres")
    reindex.add_argument("--batch-size", type=int, default=500, help="Items per DB page")
    telegram = subparsers.add_parser(
        "telegram", help="Run the Telegram bot (long polling; no webhook)"
    )
    telegram.add_argument(
        "--whoami-hint",
        action="store_true",
        help="Print how to find your Telegram user id for TELEGRAM_ALLOWED_USER_IDS",
    )
    whatsapp = subparsers.add_parser(
        "whatsapp", help="Run the WhatsApp poller (bridge store -> conversations)"
    )
    whatsapp.add_argument(
        "--interval",
        type=float,
        default=None,
        help="Seconds between polls (default: WHATSAPP_POLL_INTERVAL_S, 30)",
    )
    whatsapp.add_argument(
        "--once",
        action="store_true",
        help="Run ONE poll cycle and exit 0 (test seam; also useful to drain once)",
    )
    whatsapp.add_argument(
        "--since",
        type=str,
        default=None,
        help="ISO instant to backfill from on first sight of a channel "
        "(default: the live edge — history is NOT backfilled)",
    )
    subparsers.add_parser(
        "normalize-calendar",
        help="Rewrite legacy calendar items to the human one-liner raw_text",
    )
    enrich = subparsers.add_parser("enrich", help="Drain the enrichment queue (extraction PR A1)")
    enrich.add_argument(
        "--since",
        default=None,
        help="Backfill mode: enqueue every qualifying capture created since "
        "this ISO date (D6: its own budget, does not consume today's cap)",
    )
    enrich.add_argument("--limit", type=int, default=20, help="Max parents to process this run")
    enrich.add_argument("--budget", type=int, default=None, help="Override the cap for this run")
    memory = subparsers.add_parser("memory", help="Memory layer commands")
    memory_sub = memory.add_subparsers(dest="memory_command", required=True)
    seed_kid = memory_sub.add_parser(
        "seed-kid", help="Create/update a kid's profile facts (user-confirmed seed)"
    )
    seed_kid.add_argument("--name", required=True)
    seed_kid.add_argument("--gender", required=True, help="'girl' or 'boy' (explicit fact)")
    seed_kid.add_argument("--school", required=True)
    seed_kid.add_argument("--grade", required=True)
    seed_kid.add_argument("--class", dest="class_", required=True)
    seed_kid.add_argument("--teacher", required=True)
    seed_channel = memory_sub.add_parser(
        "seed-channel",
        help="Register a channel (WhatsApp group etc.) and who it is for",
    )
    seed_channel.add_argument("--name", required=True, help="Exact channel name")
    seed_channel.add_argument(
        "--kind",
        required=True,
        choices=["teacher", "parents", "kids", "club", "committee"],
    )
    seed_channel.add_argument(
        "--applies-to",
        required=True,
        help="<kid name> | me | family (values handed over at seed time)",
    )
    eval_extraction = subparsers.add_parser(
        "eval-extraction",
        help="Score predicted extraction children against gold labels",
    )
    eval_extraction.add_argument(
        "--gold", required=True, help="Gold children JSONL (manager-owned labels)"
    )
    eval_extraction.add_argument(
        "--pred", required=True, help="Predicted children JSONL (same schema)"
    )
    eval_extraction.add_argument(
        "--emails",
        default=None,
        help="Optional sidecar JSONL of {email_id, email_text} for grounding checks",
    )
    eval_extraction.add_argument(
        "--aggregate-only",
        action="store_true",
        help="Print ONLY the numbers (no titles/quotes/ids) — for the extraction developer",
    )
    eval_extraction.add_argument(
        "--report",
        default=None,
        help="Full per-child report path (full mode; defaults to a temp-dir file)",
    )
    eval_extraction.add_argument(
        "--title-threshold",
        type=float,
        default=DEFAULT_TITLE_THRESHOLD,
        help="Minimum title similarity for a match (default 0.3)",
    )
    eval_extraction.add_argument(
        "--timezone",
        default=None,
        help="Zone for converting offset-bearing start/due values "
        "(default: Settings.timezone from TIMEZONE)",
    )
    gmail_auth = subparsers.add_parser(
        "gmail-auth",
        help="One-time Google OAuth consent; stores ONLY the refresh token "
        "in the macOS login keychain (never in the repo or .env)",
    )
    gmail_auth.add_argument(
        "--client-secret",
        default=None,
        help="Path to the Desktop-client secret JSON "
        "(default: GMAIL_CREDENTIALS_PATH; keep it OUTSIDE the repo)",
    )
    gmail_auth.add_argument(
        "--account",
        default=GMAIL_ACCOUNT,
        help=f"Keychain account name (default: {GMAIL_ACCOUNT})",
    )
    gmail_poll = subparsers.add_parser(
        "gmail-poll",
        help="Poll the configured Gmail label (GMAIL_LABEL_ID) and capture "
        "new messages through the standard paste path",
    )
    gmail_poll.add_argument(
        "--once",
        action="store_true",
        help="Run one poll cycle and exit (also used by tests)",
    )
    args = parser.parse_args(argv)

    if args.command == "reindex":
        return _reindex(batch_size=args.batch_size)
    if args.command == "telegram":
        return _telegram(whoami_hint=args.whoami_hint)
    if args.command == "whatsapp":
        return _whatsapp(interval_s=args.interval, since=args.since, once=args.once)
    if args.command == "normalize-calendar":
        return _normalize_calendar()
    if args.command == "enrich":
        return _enrich(since=args.since, limit=args.limit, budget=args.budget)
    if args.command == "memory":
        if args.memory_command == "seed-kid":
            return _memory_seed_kid(
                name=args.name,
                gender=args.gender,
                school=args.school,
                grade=args.grade,
                class_=args.class_,
                teacher=args.teacher,
            )
        if args.memory_command == "seed-channel":
            return _memory_seed_channel(name=args.name, kind=args.kind, applies_to=args.applies_to)
        parser.error(f"unknown memory command {args.memory_command!r}")
        return 2
    if args.command == "eval-extraction":
        return _eval_extraction(args)
    if args.command == "gmail-auth":
        return _gmail_auth(args)
    if args.command == "gmail-poll":
        return _gmail_poll(args)
    parser.error(f"unknown command {args.command!r}")  # pragma: no cover - argparse guards
    return 2


WHOAMI_HINT = """\
How to find your Telegram user id (for TELEGRAM_ALLOWED_USER_IDS):

  1. Set TELEGRAM_BOT_TOKEN (get a token from @BotFather).
  2. Start the bot:  brain telegram
  3. Open your bot in Telegram and send /start from YOUR account.
  4. The log prints one line per rejected stranger:
       telegram: rejected update from user id <ID>
  5. Add that id to TELEGRAM_ALLOWED_USER_IDS (comma-separated for several
     people) and restart. Only those ids are ever answered.
"""


def _enrich(*, since: str | None, limit: int, budget: int | None) -> int:
    """Drain the enrichment queue; --since switches to backfill mode (D6)."""
    import os  # noqa: PLC0415
    from datetime import UTC, datetime  # noqa: PLC0415

    from brain.config import assert_not_live_store, get_settings  # noqa: PLC0415
    from brain.repositories.items import ItemsRepository  # noqa: PLC0415
    from brain.services.extraction.service import EnrichmentService  # noqa: PLC0415

    settings = get_settings()
    # Same live-store guard as the API lifespan (PR #31), which covers the
    # app only — the CLI must not silently write the live store either.
    assert_not_live_store(
        settings.database_url,
        settings.qdrant_collection,
        allow_live=os.environ.get("BRAIN_ALLOW_LIVE"),
    )
    with SessionLocal() as session:
        repo = ItemsRepository(session)
        service = EnrichmentService(
            repo,
            session,
            daily_cap=budget if budget is not None else settings.enrichment_daily_cap,
        )
        if since is not None:
            day = datetime.fromisoformat(since).replace(tzinfo=UTC)
            queued = 0
            for item in repo.list(
                item_type=None,
                project=None,
                status=None,
                limit=10_000,
                offset=0,
                include_children=False,
            ):
                if item.created_at < day:
                    continue
                if service.enqueue(item, budget="backfill") is not None:
                    queued += 1
            print(f"backfill: queued {queued} parent(s) since {since}")
        report = service.run_once(datetime.now(tz=UTC), limit=limit, backfill=since is not None)
        summary = " ".join(
            f"{name}={getattr(report, name)}"
            for name in (
                "processed",
                "children_created",
                "children_updated",
                "dropped_ungrounded",
                "deferred_over_cap",
                "failed",
            )
        )
        print(f"enrichment: {summary}")
    return 0


def _memory_seed_kid(
    *, name: str, gender: str, school: str, grade: str, class_: str, teacher: str
) -> int:
    """Store one kid's confirmed seed facts (the /profile 2-minute setup)."""
    from brain.db.session import SessionLocal  # noqa: PLC0415  (deferred: CLI startup)
    from brain.repositories.memory import MemoryRepository  # noqa: PLC0415
    from brain.services.memory import MemoryService  # noqa: PLC0415

    session = SessionLocal()
    try:
        kid = MemoryService(MemoryRepository(session)).set_kid(
            name,
            gender=gender,
            school=school,
            grade=grade,
            class_=class_,
            teacher=teacher,
        )
        attrs = ", ".join(f"{key} {value}" for key, value in kid.items() if key != "name")
        print(f"seeded {name}: {attrs}")
        return 0
    except ValueError as exc:
        print(f"seed-kid: {exc}", file=sys.stderr)
        return 1
    finally:
        session.close()


def _memory_seed_channel(*, name: str, kind: str, applies_to: str) -> int:
    """Register a channel and its audience (values handed over at seed time)."""
    from brain.db.session import SessionLocal  # noqa: PLC0415  (deferred: CLI startup)
    from brain.repositories.memory import MemoryRepository  # noqa: PLC0415
    from brain.services.memory import MemoryService  # noqa: PLC0415

    session = SessionLocal()
    try:
        service = MemoryService(MemoryRepository(session))
        service.set_channel(name, kind=kind, applies_to=applies_to)
        print(f"seeded channel {name}: kind {kind}, applies_to {applies_to}")
        return 0
    except ValueError as exc:
        print(f"seed-channel: {exc}", file=sys.stderr)
        return 1
    finally:
        session.close()


def _telegram(whoami_hint: bool) -> int:
    """Run the Telegram bot (long polling); --whoami-hint prints setup help."""
    if whoami_hint:
        print(WHOAMI_HINT)
        return 0

    from brain.telegram_bot import run as run_bot  # noqa: PLC0415  (deferred import)

    return run_bot(get_settings())


EX_CONFIG = 78  # sysexits: permanent configuration problem — launchd must NOT restart


def _cli_attachment_service(settings: Any) -> Any:
    """F3: the attachments core for side-process pollers (own queue/budget)."""
    from brain.services.attachments import (  # noqa: PLC0415
        AttachmentFileStore,
        AttachmentService,
        ClaudeFileReader,
        PdfTextLayerReader,
    )

    return AttachmentService(
        store=AttachmentFileStore(root=settings.files_dir),
        pdf_reader=PdfTextLayerReader(),
        claude_reader=ClaudeFileReader(),
        indexer=None,  # vectors come from 'brain reindex' when run as a side process
        session_factory=SessionLocal,
        daily_budget=settings.attachment_daily_budget,
    )


def _whatsapp(interval_s: float | None, since: str | None, once: bool = False) -> int:
    """Run the WhatsApp poller: bridge store -> per-conversation source items.

    Read-only against the bridge; cursor state lives in our DB (see
    brain.services.whatsapp). The SAME live-store guard as the API lifespan
    (PR #31) and 'brain enrich' applies — one definition of 'live store', one
    opt-in flag: a bare worktree run against the live DB is refused with
    EX_CONFIG (78), as are all permanent conditions (unconfigured bridge DB,
    missing store file) so launchd never restart-loops them. Per-poll
    failures are logged and retried on the next tick; Ctrl-C exits cleanly.
    """
    import os  # noqa: PLC0415
    import time  # noqa: PLC0415
    from datetime import UTC, datetime  # noqa: PLC0415

    from brain.config import assert_not_live_store  # noqa: PLC0415
    from brain.ops import heartbeat, record_event  # noqa: PLC0415
    from brain.repositories.memory import MemoryRepository  # noqa: PLC0415
    from brain.repositories.whatsapp import WhatsAppStateRepository  # noqa: PLC0415
    from brain.services.whatsapp import (  # noqa: PLC0415
        BridgeStore,
        PollerError,
        WhatsAppMediaClient,
        WhatsAppPoller,
    )

    settings = get_settings()
    # One definition of 'live store' (brain.config): the CLI must not silently
    # write the live store either; run-whatsapp.sh exports BRAIN_ALLOW_LIVE=1.
    try:
        assert_not_live_store(
            settings.database_url,
            settings.qdrant_collection,
            allow_live=os.environ.get("BRAIN_ALLOW_LIVE"),
        )
    except RuntimeError as exc:
        print(f"whatsapp: refusing to start against the live store: {exc}", file=sys.stderr)
        return EX_CONFIG
    if not settings.whatsapp_bridge_db.strip():
        print("whatsapp: WHATSAPP_BRIDGE_DB is not configured; poller disabled", file=sys.stderr)
        return EX_CONFIG
    bridge_path = Path(settings.whatsapp_bridge_db).expanduser()
    if not bridge_path.is_file():
        print(
            f"whatsapp: bridge store not found: {bridge_path} "
            "(permanent; is the WhatsApp bridge installed?)",
            file=sys.stderr,
        )
        return EX_CONFIG
    try:
        bridge = BridgeStore(settings.whatsapp_bridge_db, naive_zone=settings.timezone)
    except Exception as exc:
        print(f"whatsapp: cannot open bridge store: {exc}", file=sys.stderr)
        return EX_CONFIG

    backfill: datetime | None = None
    if since:
        try:
            backfill = datetime.fromisoformat(since)
        except ValueError as exc:
            print(f"whatsapp: --since must be an ISO instant: {exc}", file=sys.stderr)
            return 1
        if backfill.tzinfo is None:
            backfill = backfill.replace(tzinfo=UTC)

    interval = float(interval_s) if interval_s else settings.whatsapp_poll_interval_s
    session = SessionLocal()
    try:
        poller = WhatsAppPoller(
            bridge=bridge,
            state=WhatsAppStateRepository(session),
            items=ItemsRepository(session),
            memory=MemoryRepository(session),
            indexer=None,  # vectors come from 'brain reindex' when run as a side process
            zone_name=settings.timezone,
            gap_minutes=settings.whatsapp_gap_minutes,
            overlap_minutes=settings.whatsapp_overlap_minutes,
            backfill_since=backfill,
            commit=True,
            attachments=_cli_attachment_service(settings),
            media_client=WhatsAppMediaClient(
                settings.whatsapp_bridge_url,
                store_root=Path(settings.whatsapp_bridge_db).expanduser().parent,
            ),
        )
    except PollerError as exc:
        print(f"whatsapp: {exc}", file=sys.stderr)
        return EX_CONFIG

    if once:
        poller.poll(now=datetime.now(UTC))
        print("whatsapp: --once poll done")
        return 0

    print(f"whatsapp: polling {bridge.path} every {interval:.0f}s (Ctrl-C to stop)")
    try:
        while True:
            started = time.monotonic()
            try:
                results = poller.poll(now=datetime.now(UTC))
                for result in results:
                    if result.seen or result.created or result.updated:
                        print(
                            f"whatsapp: {result.channel}: +{result.seen} msgs, "
                            f"{result.created} new / {result.updated} updated conversations"
                        )
                heartbeat("whatsapp", detail={"channels": len(results)})
            except KeyboardInterrupt:
                raise
            except Exception as exc:  # noqa: BLE001 — the loop outlives poll failures
                logger.warning("whatsapp: poll cycle failed: %s", exc)
                record_event(
                    "whatsapp",
                    "poll_cycle_error",
                    ok=False,
                    detail={"error": type(exc).__name__},
                )
            elapsed = time.monotonic() - started
            time.sleep(max(0.5, interval - elapsed))
    except KeyboardInterrupt:
        print("whatsapp: stopped")
    return 0


def _eval_extraction(args: argparse.Namespace) -> int:
    """Score predicted children against gold labels; returns a process exit code.

    Exit 0 on a successful run (the scores themselves are not an exit
    condition); exit 1 on unreadable inputs or schema violations. In
    --aggregate-only mode stdout carries ONLY numbers (safe to share with the
    extraction developer); full mode also writes a per-child markdown report
    (titles, quotes, ids — manager only), defaulting to a temp-dir file so a
    sensitive report never silently lands in the repo.
    """
    try:
        gold = load_jsonl(args.gold)
        pred = load_jsonl(args.pred)
        sidecar = load_email_texts(args.emails) if args.emails else None
        zone_name = args.timezone or get_settings().timezone
        try:
            zone = ZoneInfo(zone_name)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise EvalError(f"unknown timezone {zone_name!r}: {exc}") from exc
        result = evaluate(
            gold, pred, email_texts=sidecar, title_threshold=args.title_threshold, tz=zone
        )
    except EvalError as exc:
        print(f"eval-extraction failed: {exc}", file=sys.stderr)
        return 1
    print(format_aggregate(result))
    if args.aggregate_only:
        return 0
    report_path = args.report
    if report_path is None:
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        report_path = str(Path(tempfile.gettempdir()) / f"eval-extraction-report-{stamp}.md")
    written = write_report(result, report_path, gold_path=str(args.gold), pred_path=str(args.pred))
    print(f"full report written to {written}")
    return 0


def _reindex(
    batch_size: int,
    repo: ItemsRepositoryProto | None = None,
    embedder: Embedder | None = None,
    store: VectorStore | None = None,
) -> int:
    """Rebuild the vector collection from Postgres; returns a process exit code.

    Ghost vectors (ids no longer in Postgres) are purged by the full rebuild.
    ``repo``/``embedder``/``store`` are injectable for tests; all
    EmbedderError/VectorStoreError failures print a clean message and exit 1.
    """
    settings = get_settings()
    live_store = store
    live_embedder = embedder
    try:
        if live_store is None:
            live_store = QdrantVectorStore(
                url=settings.qdrant_url,
                api_key=unwrap_secret(settings.qdrant_api_key),
                collection=settings.qdrant_collection,
                timeout_s=settings.qdrant_timeout_s,
            )
        if live_embedder is None:
            live_embedder = build_embedder(settings)
        if repo is None:
            repo = ItemsRepository(SessionLocal())
        indexer = QdrantItemIndexer(live_embedder, live_store)
        service = IngestService(repo, classifier=_NoopClassifier(), indexer=indexer)
        indexed = service.reindex(batch_size=batch_size)
    except (EmbedderError, VectorStoreError) as exc:
        print(
            f"reindex failed: {exc}. If the failure came after the purge, the "
            "collection is PARTIAL; rerun 'brain reindex' to finish rebuilding.",
            file=sys.stderr,
        )
        return 1
    finally:
        if live_store is not None:
            live_store.close()
        if live_embedder is not None:
            live_embedder.close()
    print(f"indexed {indexed} item(s) into collection '{settings.qdrant_collection}'")
    return 0


class _NoopClassifier:
    """Classifier stand-in: reindex never classifies, only embeds."""

    def classify(self, text: str) -> dict[str, object]:
        raise AssertionError("reindex must not classify")  # pragma: no cover


_PAGE = 500


def _normalize_calendar(
    repo: ItemsRepositoryProto | None = None,
) -> int:
    """Rewrite pre-one-liner calendar items; returns a process exit code.

    Old calendar items carry the whole ICS blob as raw_text. New captures
    store a human one-liner ('<title> — <Day DD Mon HH:MM–HH:MM> [@ loc]')
    and keep the ICS in meta['ics_raw']. This one-off rewrites existing rows
    the same way (original text preserved in meta, fingerprint recomputed so
    the dedupe guard keeps matching), and prints a count. Re-embedding is
    deliberately NOT done here — run 'brain reindex' afterwards (the deploy
    sequence does exactly that).
    """
    settings = get_settings()
    zone = ZoneInfo(settings.timezone)
    if repo is None:
        repo = ItemsRepository(SessionLocal())
    normalized = 0
    offset = 0
    while True:
        batch = repo.list(item_type=None, project=None, status=None, limit=_PAGE, offset=offset)
        if not batch:
            break
        for item in batch:
            if item.source != "calendar":
                continue
            meta = dict(item.meta or {})
            if not meta.get("title"):
                continue  # nothing to build from; leave the item untouched
            one_liner = _calendar_one_liner(meta, zone)
            if item.raw_text == one_liner:
                continue  # already normalized (idempotent rerun)
            original = item.raw_text
            if "begin:vevent" in original.lower() and "ics_raw" not in meta:
                meta["ics_raw"] = original
            elif "original_text" not in meta:
                meta["original_text"] = original
            item.raw_text = one_liner
            item.meta = meta  # reassign so SQLAlchemy sees the JSONB change
            item.fingerprint = content_fingerprint(one_liner, item.occurred_at, item.source)
            normalized += 1
        offset += len(batch)
    repo.commit()
    print(f"normalized {normalized} calendar item(s) (run 'brain reindex' to re-embed)")
    return 0


# --- gmail (email source, F2) -------------------------------------------------


def _build_oauth_flow(client_secret_path: str):  # pragma: no cover - needs google libs
    """Build the installed-app OAuth flow (the ONLY google import in the CLI)."""
    try:
        from google_auth_oauthlib.flow import InstalledAppFlow
    except ImportError as exc:
        raise SystemExit(
            "gmail: google libraries not installed — run `pip install -e '.[gmail]'`"
        ) from exc
    return InstalledAppFlow.from_client_secret_file(client_secret_path, scopes=[GMAIL_SCOPE])


def _gmail_auth(args: argparse.Namespace) -> int:
    """One-time consent; stores ONLY the refresh token in the login keychain."""
    settings: Settings = get_settings()
    path = args.client_secret or settings.gmail_credentials_path
    if not path:
        print(
            "gmail-auth: no client secret — pass --client-secret /path/to/secret.json "
            "(or set GMAIL_CREDENTIALS_PATH). Keep the file OUTSIDE the repo.",
            file=sys.stderr,
        )
        return 78
    if not Path(path).exists():
        print(f"gmail-auth: client secret file not found: {path}", file=sys.stderr)
        return 78
    flow = _build_oauth_flow(path)
    print("gmail-auth: opening the browser for Google consent (read-only Gmail scope)…")
    try:
        creds = flow.run_local_server(port=0)
    except KeyboardInterrupt:
        print("gmail-auth: cancelled", file=sys.stderr)
        return 130
    token = creds.refresh_token
    if not token:
        print(
            "gmail-auth: Google did not return a refresh token — revoke the app at "
            "https://myaccount.google.com/permissions and re-run (prompt=consent).",
            file=sys.stderr,
        )
        return 78
    Keychain().store(args.account, token)
    print(
        f"gmail-auth: refresh token stored in the login keychain "
        f"(service second-brain-gmail, account {args.account}). The token is never "
        f"printed or written to disk.\n"
        f"Next: set GMAIL_LABEL_ID to the immutable Gmail label id to poll "
        f"(see docs/email-setup.md), then run `brain gmail-poll`."
    )
    return 0


def _build_email_poller(settings: Settings) -> EmailPoller:
    """Assemble the poller from the shared AI singletons (same path as the bot)."""
    from brain.api import deps  # noqa: PLC0415  (deferred: shared client singletons)
    from brain.repositories.email import EmailMessageRepository  # noqa: PLC0415

    session = SessionLocal()
    ingest = IngestService(
        ItemsRepository(session),
        deps.get_classifier(),
        deps.get_item_indexer(),
    )
    return EmailPoller(
        client=RestGmailClient(token_loader=lambda: Keychain().read(GMAIL_ACCOUNT) or ""),
        ingest=ingest,
        seen_repo=EmailMessageRepository(session),
        settings=settings,
    )


def _gmail_poll(args: argparse.Namespace) -> int:
    settings: Settings = get_settings()
    if not settings.gmail_label_id:
        print("gmail-poll: GMAIL_LABEL_ID unset — nothing to poll", file=sys.stderr)
        return 78
    token = Keychain().read(GMAIL_ACCOUNT)
    if not token:
        print(
            "gmail-poll: no refresh token in the keychain — run `brain gmail-auth` first",
            file=sys.stderr,
        )
        return 78
    poller = _build_email_poller(settings)
    if args.once:
        summary = poller.poll_once()
        heartbeat_email(summary)
        print(
            f"gmail-poll: state={summary.state} listed={summary.listed} new={summary.new} "
            f"skipped={summary.skipped} errors={summary.errors}"
        )
        return 0
    return poller.run_forever()


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
