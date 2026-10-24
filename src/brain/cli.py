"""``brain`` command line interface (entry point: ``brain = brain.cli:main``)."""

from __future__ import annotations

import argparse
import logging
import sys
import tempfile
from collections.abc import Callable, Sequence
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from brain.ai.embedder import Embedder, EmbedderError
from brain.ai.gmail import GMAIL_SCOPE
from brain.ai.vectorstore import QdrantVectorStore, VectorStore, VectorStoreError
from brain.config import Settings, get_settings, unwrap_secret
from brain.db.session import SessionLocal
from brain.factories import build_embedder
from brain.repositories.items import ItemsRepository
from brain.services.dedupe import content_fingerprint
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
    whatsapp_triage = subparsers.add_parser(
        "whatsapp-triage",
        help="Backfill relevance triage for untriaged WhatsApp conversations (D2a)",
    )
    whatsapp_triage.add_argument(
        "--limit", type=int, default=500, help="Max conversations to triage this run"
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
    enrich = subparsers.add_parser(
        "enrich",
        help="Drain the enrichment queue (extraction PR A1); manual — does "
        "not send proposal cards (the in-bot worker does)",
    )
    enrich.add_argument(
        "--since",
        default=None,
        help="Backfill mode: enqueue every qualifying capture created since "
        "this ISO date (D6: its own budget, does not consume today's cap)",
    )
    enrich.add_argument("--limit", type=int, default=20, help="Max parents to process this run")
    enrich.add_argument("--budget", type=int, default=None, help="Override the cap for this run")
    recon_eval = subparsers.add_parser(
        "recon-eval",
        help="Score reconciliation verdicts against a cases jsonl (offline, no DB/LLM)",
    )
    recon_eval.add_argument("--cases", required=True, help="Cases jsonl (schema: recon-plan)")
    recon_eval.add_argument(
        "--tz", default=None, help="Timezone for local-day windows (default Settings.timezone)"
    )
    extract_eval = subparsers.add_parser(
        "extract-eval",
        help="Run the LLM extractor over an emails file -> pred.jsonl (offline)",
    )
    extract_eval.add_argument("--input", required=True, help="Emails file (split on =====)")
    extract_eval.add_argument("--out", required=True, help="Output pred.jsonl path")
    extract_eval.add_argument(
        "--limit", type=int, default=None, help="Only the first N emails (smoke runs)"
    )
    extract_eval.add_argument(
        "--memory",
        default=None,
        help="Read-only LIVE memory export (entities+facts JSON) to import "
        "into the TEST DB before running (audience resolution needs it)",
    )
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
    gmail_label = subparsers.add_parser(
        "gmail-label",
        help="Apply SB/ labels by rule (dry-run by default; --apply is "
        "explicit and capped; label-only, undo-able, ops-audited)",
    )
    gmail_label.add_argument(
        "--apply", action="store_true",
        help="Actually write labels (default: dry-run, zero writes)",
    )
    gmail_label.add_argument(
        "--max-apply", type=_max_apply_value, default=20,
        help="Hard per-run cap on applied labels (1..100; default: 20) — "
        "anything outside the range is refused before any call",
    )
    gmail_label.add_argument(
        "--max-scan", type=int, default=100,
        help="Bound on metadata GETs per run (quota hygiene; default: 100)",
    )
    gmail_label.add_argument(
        "--auth", action="store_true",
        help="One-time consent for the LABELER scope set (readonly + "
        "gmail.modify). Stores the token in the labeler's OWN keychain item "
        "(never the sync token's item the poller uses). Requires "
        "GMAIL_ACCOUNT: after consent the consented profile's address is "
        "checked against it and NOTHING is stored on a mismatch",
    )
    gmail_label.add_argument("--undo", metavar="BATCH_ID", default=None,
                             help="Remove exactly the labels ONE batch added")
    gmail_label.add_argument("--audit", nargs="?", const="ALL", metavar="BATCH_ID",
                             help="Print one batch's (or every batch's) applied labels")
    connectors = subparsers.add_parser(
        "connectors", help="Pluggable data sources (connectors ticket)"
    )
    connectors_sub = connectors.add_subparsers(dest="connectors_command", required=True)
    connectors_sub.add_parser(
        "list",
        help="List known connectors: enabled, last ok, cursor present, last error",
    )
    connectors_sync = connectors_sub.add_parser(
        "sync", help="Run one sync cycle for a connector (BRAIN_CONNECTORS must enable it)"
    )
    connectors_sync.add_argument("name", help="Connector name (email|gcal|gdrive|gtasks)")
    connectors_sync.add_argument(
        "--dry-run",
        action="store_true",
        help="Fetch and print the records; write NOTHING (no items, no cursor)",
    )
    connectors_sync.add_argument(
        "--limit", type=int, default=200, help="Max records per batch (default 200)"
    )
    connectors_sync.add_argument(
        "--drain",
        action="store_true",
        help="Loop batch after batch until the source is drained (or a batch "
        "makes no progress). Caps at 500 batches as a runaway guard.",
    )
    connectors_adopt = connectors_sub.add_parser(
        "adopt",
        help="One-time: point the idempotency ledger at EXISTING legacy items "
        "(pasted calendar one-liners / bot emails) so the FIRST sync does not "
        "duplicate them. Requires consent; runs before the first sync only",
    )
    connectors_adopt.add_argument("name", help="Connector name (email|gcal|gdrive|gtasks)")
    connectors_adopt.add_argument(
        "--dry-run",
        action="store_true",
        help="Fetch and print the adoption report; write NOTHING (no ledger rows)",
    )
    connectors_adopt.add_argument(
        "--undo",
        action="store_true",
        help="Remove the ledger rows the last real adopt wrote (refuses rows a "
        "sync has since touched or the user deleted the item through)",
    )
    connectors_adopt.add_argument(
        "--limit", type=int, default=200, help="Max records per fetch batch (default 200)"
    )
    connectors_auth = connectors_sub.add_parser(
        "auth",
        help="Google OAuth consent for the enabled connectors (stores ONLY the "
        "refresh token in the macOS login keychain, same item as gmail-auth)",
    )
    connectors_auth.add_argument("provider", choices=["google"], help="Only google today")
    connectors_auth.add_argument(
        "--client-secret",
        default=None,
        help="Path to the Desktop-client secret JSON "
        "(default: GMAIL_CREDENTIALS_PATH; keep it OUTSIDE the repo)",
    )
    args = parser.parse_args(argv)

    if args.command == "reindex":
        return _reindex(batch_size=args.batch_size)
    if args.command == "telegram":
        return _telegram(whoami_hint=args.whoami_hint)
    if args.command == "whatsapp":
        return _whatsapp(interval_s=args.interval, since=args.since, once=args.once)
    if args.command == "whatsapp-triage":
        return _whatsapp_triage(limit=args.limit)
    if args.command == "normalize-calendar":
        return _normalize_calendar()
    if args.command == "enrich":
        return _enrich(since=args.since, limit=args.limit, budget=args.budget)
    if args.command == "recon-eval":
        return _recon_eval(cases=args.cases, tz_name=args.tz)
    if args.command == "extract-eval":
        return _extract_eval(
            input_path=args.input,
            out_path=args.out,
            limit=args.limit,
            memory_snapshot=args.memory,
        )
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
    if args.command == "gmail-label":
        return _gmail_label(args)
    if args.command == "connectors":
        if args.connectors_command == "list":
            return _connectors_list()
        if args.connectors_command == "sync":
            return _connectors_sync(args)
        if args.connectors_command == "adopt":
            return _connectors_adopt(args)
        if args.connectors_command == "auth":
            return _connectors_auth(args)
        parser.error(f"unknown connectors command {args.connectors_command!r}")
        return 2
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


def _extract_eval(
    *,
    input_path: str,
    out_path: str,
    limit: int | None,
    memory_snapshot: str | None,
) -> int:
    """Offline extraction-eval runner: emails file -> pred.jsonl (never live)."""
    import os  # noqa: PLC0415
    from zoneinfo import ZoneInfo  # noqa: PLC0415

    from brain.api.deps import get_llm_client  # noqa: PLC0415
    from brain.config import assert_not_live_store, get_settings  # noqa: PLC0415
    from brain.services.extraction.eval_runner import run_extraction_eval  # noqa: PLC0415
    from brain.services.extraction.llm_extractor import (  # noqa: PLC0415
        LLMExtractor,
    )

    settings = get_settings()
    assert_not_live_store(
        settings.database_url,
        settings.qdrant_collection,
        allow_live=os.environ.get("BRAIN_ALLOW_LIVE"),
    )
    from brain.db.session import SessionLocal  # noqa: PLC0415

    if limit is not None:
        # --limit: build a truncated copy of the emails file first
        out_dir = Path(out_path).parent
        out_dir.mkdir(parents=True, exist_ok=True)
        chunks = [
            c for c in Path(input_path).read_text(encoding="utf-8").split("=====") if c.strip()
        ]
        temp = out_dir / f".eval-input-{Path(out_path).stem}.txt"
        temp.write_text("=====".join(chunks[:limit]), encoding="utf-8")
        input_path = str(temp)

    llm = get_llm_client()
    try:
        extractor = LLMExtractor(llm)
        with SessionLocal() as session:
            rows = run_extraction_eval(
                Path(input_path),
                Path(out_path),
                session,
                extractor,
                tz=ZoneInfo(settings.timezone),
                memory_snapshot=Path(memory_snapshot) if memory_snapshot else None,
            )
    finally:
        close = getattr(llm, "close", None)
        if close is not None:
            close()
    print(f"extract-eval: {len(rows)} children -> {out_path}")
    return 0


def run_recon_eval(
    cases_path: Path | str,
    *,
    now: datetime | None = None,
    tz_name: str | None = None,
    embedder: Any | None = None,
    judge: Any | None = None,
) -> dict[str, Any]:
    """Score reconciliation verdicts against a cases jsonl (offline).

    One case per line: {case_id, existing_items: [{id, type, title, start,
    applies_to, location}], new_text_quote, candidate: {kind, title, start,
    applies_to, location}, verdict}. The signal comes from new_text_quote
    (code-detected phrases; the same rules the pipeline uses). Returns
    per-verdict precision/recall (tp/fp/fn) and the confusion matrix
    (expected -> computed). NO DB, NO network, NO LLM (D-recon-1).
    """
    import json  # noqa: PLC0415
    from datetime import UTC, datetime  # noqa: PLC0415

    # B1 (final check): the eval matches with the SAME builder as
    # production — no separate construction. Tests inject the embedder.
    from brain.config import get_settings  # noqa: PLC0415
    from brain.services.extraction.reconciliation import (  # noqa: PLC0415
        ExistingItem,
        ReconCandidate,
        classify,
        recon_similarity_from_settings,
        signal_for,
    )

    similarity = recon_similarity_from_settings(get_settings(), embedder=embedder)
    tz_name = tz_name or get_settings().timezone
    now = now or datetime.now(tz=UTC)
    per_verdict: dict[str, dict[str, int]] = {}
    confusion: dict[str, dict[str, int]] = {}
    total = 0
    with open(cases_path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            case = json.loads(line)
            total += 1
            existing = [
                ExistingItem(
                    id=str(row["id"]),
                    type=str(row["type"]),
                    title=str(row.get("title", "")),
                    start=_parse_eval_dt(row.get("start")),
                    end=_parse_eval_dt(row.get("end")),
                    applies_to=tuple(row.get("applies_to") or ()),
                    location=row.get("location"),
                    amount=row.get("amount"),
                    effective=_parse_eval_dt(row.get("effective")),
                )
                for row in case.get("existing_items", [])
            ]
            cand = case["candidate"]
            candidate = ReconCandidate(
                # E5 (round 3): the spec's shape says `type`; `kind` stays as
                # an alias for the files written before this change.
                kind=str(cand.get("type") or cand["kind"]),
                title=str(cand.get("title", "")),
                start=_parse_eval_dt(cand.get("start")),
                applies_to=tuple(cand.get("applies_to") or ()),
                location=cand.get("location"),
                end=_parse_eval_dt(cand.get("end")),
                amount=cand.get("amount"),
            )
            signal = signal_for([str(case.get("new_text_quote", ""))])
            # E5: a dated correction is judged inside ITS OWN window — the
            # optional per-case `now` (the message's timestamp) wins.
            case_now = _parse_eval_dt(case.get("now")) or now
            computed = classify(
                candidate,
                existing,
                similarity=similarity,
                now=case_now,
                tz_name=tz_name,
                signal=signal,
            ).verdict.value
            expected = str(case["verdict"]).strip().lower()
            stats = per_verdict.setdefault(expected, {"tp": 0, "fp": 0, "fn": 0})
            if computed == expected:  # both lowercase (E5)
                stats["tp"] += 1
            else:
                stats["fn"] += 1
                per_verdict.setdefault(computed, {"tp": 0, "fp": 0, "fn": 0})["fp"] += 1
            confusion.setdefault(expected, {}).setdefault(computed, 0)
            confusion[expected][computed] += 1
    return {"cases": total, "per_verdict": per_verdict, "confusion": confusion}


def _parse_eval_dt(value: Any) -> datetime | None:
    if not value:
        return None

    return datetime.fromisoformat(str(value))


def _recon_eval(*, cases: str, tz_name: str | None) -> int:
    """Offline recon scoring (D-recon-1): prints per-verdict P/R + matrix."""
    from datetime import UTC, datetime  # noqa: PLC0415
    from zoneinfo import ZoneInfo  # noqa: PLC0415

    from brain.config import get_settings  # noqa: PLC0415

    tz = ZoneInfo(tz_name or get_settings().timezone)
    report = run_recon_eval(cases, now=datetime.now(tz=UTC), tz_name=tz.key)
    print(f"cases: {report['cases']}")
    for verdict, stats in sorted(report["per_verdict"].items()):
        tp, fp, fn = stats["tp"], stats["fp"], stats["fn"]
        precision = tp / (tp + fp) if tp + fp else 0.0
        recall = tp / (tp + fn) if tp + fn else 0.0
        print(f"{verdict}: tp={tp} fp={fp} fn={fn} P={precision:.4f} R={recall:.4f}")
    print("confusion (expected -> computed):")
    for expected, row in sorted(report["confusion"].items()):
        cells = ", ".join(f"{k}={v}" for k, v in sorted(row.items()))
        print(f"  {expected}: {cells}")
    return 0


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


def _side_process_indexer() -> Any:
    """The real vector indexer for side-process pollers, or None.

    The API and the telegram bot index through the app-lifetime deps
    singletons; a side process builds the same one. When that is impossible
    (no embedder/qdrant), polling still works — indexing defers to
    'brain reindex' (the poller's best-effort policy).
    """
    try:
        from brain.api import deps  # noqa: PLC0415

        return deps.get_item_indexer()
    except Exception as exc:  # noqa: BLE001 — fallback, but never a silent one
        logger.warning(
            "whatsapp: vector indexer unavailable (%s: %s); conversations will be "
            "indexed by 'brain reindex'",
            type(exc).__name__,
            exc,
        )
        try:
            from brain.ops import record_event  # noqa: PLC0415

            record_event(
                "whatsapp",
                "indexer_unavailable",
                ok=False,
                detail={"error": type(exc).__name__, "message": str(exc)},
            )
        except Exception as tele_exc:  # noqa: BLE001 — telemetry must not mask the fallback
            logger.warning(
                "whatsapp: could not record indexer_unavailable (%s: %s)",
                type(tele_exc).__name__,
                tele_exc,
            )
        return None


def _cli_attachment_service(settings: Any, indexer: Any = None, on_read: Any = None) -> Any:
    """F3: the attachments core for side-process pollers (own queue/budget).

    ``on_read`` (the #113 follow-up) is the post-read re-triage hook: called
    on the SAME session _finish uses for every successful read of a
    whatsapp attachment. The poller wires it to the post-read re-triage;
    the backfill and other callers pass nothing.
    """
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
        indexer=indexer,  # attachment items are indexed by the service itself
        session_factory=SessionLocal,
        daily_budget=settings.attachment_daily_budget,
        on_read=on_read,
    )


def _build_whatsapp_triage_hooks(session: Any) -> tuple[Any, Any]:
    """One trier + the post-read re-triage callback (the #113 follow-up).

    The callback MUST run on the session the attachment pipeline hands it
    (read_session): production builds the attachments service with
    session_factory=SessionLocal, so _finish's session is not the poller's,
    and the row it just marked done is uncommitted (autoflush=False) when
    the hook fires. The same trier drives both the poller's triage and the
    post-read re-triage.
    """
    trier = _build_trier(session)

    def _on_attachment_read(read_session: Any, att: Any) -> None:
        from brain.services.whatsapp import retriage_after_attachment_read  # noqa: PLC0415

        retriage_after_attachment_read(read_session, att, trier=trier)

    return trier, _on_attachment_read


def _build_trier(session):
    """The real triage callable: kid override, then Jev, then the LLM (D2a).

    Refreshable kid names: the poller holds this trier for the process
    lifetime, so the confirmed kid names/aliases are re-read ONCE PER POLL
    PASS (RefreshableTrier.refresh, savepoint-scoped) — never per
    conversation. The backfill command builds a fresh trier per invocation,
    which reads them at construction with no refresh call.
    """
    from brain.api import deps  # noqa: PLC0415
    from brain.services.triage import RefreshableTrier  # noqa: PLC0415

    jev = deps.get_jev_client()
    llm = deps.get_llm_client()
    return RefreshableTrier(session, jev=jev, llm=llm)


def _whatsapp_triage(*, limit: int) -> int:
    """Backfill relevance triage for untriaged WhatsApp conversations (D2a).

    Guarded like every live-touching command (BRAIN_ALLOW_LIVE=1 explicitly).
    Only UNTRIAGED conversations are triaged — re-triage happens through the
    update path. Triage never deletes anything; failures are recorded (ops)
    and the backfill continues.
    """
    import os  # noqa: PLC0415

    from brain.config import assert_not_live_store  # noqa: PLC0415
    from brain.repositories.items import ItemsRepository  # noqa: PLC0415
    from brain.services.whatsapp import is_unread_media_only  # noqa: PLC0415

    settings = get_settings()
    assert_not_live_store(
        settings.database_url,
        settings.qdrant_collection,
        allow_live=os.environ.get("BRAIN_ALLOW_LIVE"),
    )
    decided = 0
    failed = 0
    skipped = 0
    attachments = _cli_attachment_service(settings)
    with SessionLocal() as session:
        trier = _build_trier(session)
        if not getattr(trier, "kid_names_loaded", True):
            # Startup tolerance (worker_3's post-merge review of #111): the
            # trier swallowed a failed first kid-names read for the POLLER's
            # sake, but a ONE-SHOT backfill on [] names would silently miss
            # kid mentions. Refuse loudly instead.
            print(
                "whatsapp-triage: initial kid-names read failed — refusing to "
                "run a one-shot backfill with no kid names (kid mentions would "
                "be missed silently). Re-run once the DB is reachable.",
                file=sys.stderr,
            )
            return 1
        repo = ItemsRepository(session)
        items = repo.untriaged_whatsapp(limit=limit)
        for item in items:
            if is_unread_media_only(
                item.raw_text, meta=item.meta, attachment_text=attachments.attachment_text
            ):
                # Same skip the poller's _triage_item applies — enriched with
                # the attachment lookup: a SAVED-but-unread image (marker
                # already swapped, nothing extracted) is still unread media.
                # 0 trier/model calls, columns stay NULL; counted separately
                # in the summary.
                skipped += 1
                continue
            result = trier(item)
            if result is None:
                failed += 1
                continue
            repo.apply_triage(item, result)
            decided += 1
        session.commit()
    print(
        f"whatsapp-triage: decided {decided}, failed {failed}, "
        f"skipped (unread media) {skipped} of limit {limit}"
    )
    return 0


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
    indexer = _side_process_indexer()
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
    trier, on_read = _build_whatsapp_triage_hooks(session)
    attachment_service = _cli_attachment_service(
        settings, indexer=indexer, on_read=on_read
    )

    try:
        poller = WhatsAppPoller(
            bridge=bridge,
            state=WhatsAppStateRepository(session),
            items=ItemsRepository(session),
            memory=MemoryRepository(session),
            indexer=indexer,  # side processes index like the API does (best-effort)
            trier=trier,  # D2a: relevance triage on create + update
            zone_name=settings.timezone,
            gap_minutes=settings.whatsapp_gap_minutes,
            overlap_minutes=settings.whatsapp_overlap_minutes,
            backfill_since=backfill,
            commit=True,
            attachments=attachment_service,
            media_client=WhatsAppMediaClient(
                settings.whatsapp_bridge_url,
                store_root=Path(settings.whatsapp_bridge_db).expanduser().parent,
            ),
            # the media triage skip resolves readability through the
            # attachment's EXTRACTED text (the #113 follow-up)
            attachment_text=attachment_service.attachment_text,
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
        batch = repo.list(
            item_type=None,
            project=None,
            status=None,
            limit=_PAGE,
            offset=offset,
            include_noise=True,  # maintenance passes see EVERY row
        )
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


def _build_oauth_flow(
    client_secret_path: str, scopes: list[str] | None = None
):  # pragma: no cover - needs google libs
    """Build the installed-app OAuth flow (the ONLY google import in the CLI).

    ``scopes=None`` = the SYNC scope (GMAIL_SCOPE, readonly) — the poller/
    connector path is untouched; the labeler passes GMAIL_LABEL_SCOPES.
    """
    try:
        from google_auth_oauthlib.flow import InstalledAppFlow
    except ImportError as exc:
        raise SystemExit(
            "gmail: google libraries not installed — run `pip install -e '.[gmail]'`"
        ) from exc
    return InstalledAppFlow.from_client_secrets_file(
        client_secret_path, scopes=scopes if scopes is not None else [GMAIL_SCOPE]
    )


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


def _gmail_poll_cycle(indexer: Any, attachments: Any) -> Any:
    """One ConnectorRunner cycle for the email connector (the ONE live path)."""
    from brain.api import deps  # noqa: PLC0415 (deferred: shared client singletons)
    from brain.connectors.google.gmail import GmailConnector  # noqa: F401 (registers)
    from brain.connectors.runner import ConnectorRunner
    from brain.connectors.state import ConnectorRecordRepo, ConnectorStateRepo

    with SessionLocal() as session:
        ingest = IngestService(
            ItemsRepository(session),
            deps.get_classifier(),
            deps.get_item_indexer(),
        )
        connector = _build_connector("email", session=session)
        runner = ConnectorRunner(
            connector,
            ingest=ingest,
            repo=ItemsRepository(session),
            state=ConnectorStateRepo(session),
            records=ConnectorRecordRepo(session),
            indexer=indexer,
            attachments=attachments,
        )
        try:
            summary = runner.run(limit=200)
        finally:
            connector.close()
    try:
        attachments.drain()  # F3: one budgeted read per poll cycle
    except Exception as exc:  # noqa: BLE001 — reading never fails the poll
        logger.warning("gmail-poll: attachment drain failed (%s)", exc)
    return summary


def _gmail_poll(
    args: argparse.Namespace,
    *,
    sleep: Callable[[float], None] | None = None,
    max_cycles: int | None = None,
) -> int:
    """The email source loop — the launchd agent path (com.secondbrain.email).

    C5: the F2 EmailPoller is DELETED; this drives the Gmail CONNECTOR through
    the shared ConnectorRunner (historyId cursor, query fallback, ledger
    dedupe). Same external contract as before: 78 = permanent config error (no
    restart), BRAIN_ALLOW_LIVE=1 is the sanctioned live-store opt-in (set by
    scripts/run-email.sh), one cockpit heartbeat per cycle (transitional
    'email' heartbeat until the Sources panel owns it), exponential backoff on
    retryable failures, 5-minute stalls on auth (user action required).
    """
    import os  # noqa: PLC0415
    import time  # noqa: PLC0415

    from brain.config import assert_not_live_store  # noqa: PLC0415
    from brain.connectors.google.gmail import heartbeat_email_summary  # noqa: PLC0415
    from brain.connectors.runner import backoff_seconds  # noqa: PLC0415
    from brain.connectors.state import ConnectorStateRepo  # noqa: PLC0415

    settings: Settings = get_settings()
    if not settings.gmail_label_id:
        print("gmail-poll: GMAIL_LABEL_ID unset — nothing to poll", file=sys.stderr)
        return 78
    if not (Keychain().read(GMAIL_ACCOUNT) or "").strip():
        print(
            "gmail-poll: no refresh token in the keychain — run `brain gmail-auth` first",
            file=sys.stderr,
        )
        return 78
    try:
        assert_not_live_store(
            settings.database_url,
            settings.qdrant_collection,
            allow_live=os.environ.get("BRAIN_ALLOW_LIVE"),
        )
    except RuntimeError as exc:
        print(f"gmail-poll: refusing to start against the live store: {exc}", file=sys.stderr)
        return 78

    indexer = _side_process_indexer()
    attachments = _cli_attachment_service(settings, indexer=indexer)

    if args.once:
        summary = _gmail_poll_cycle(indexer, attachments)
        heartbeat_email_summary(summary)
        print(
            f"gmail-poll: state={summary.state} listed={summary.fetched} "
            f"new={summary.created} skipped={summary.skipped} errors={summary.errors}"
        )
        return 0

    stall_pause_s = 300.0
    do_sleep = sleep if sleep is not None else time.sleep
    cycles = 0
    while True:
        try:
            summary = _gmail_poll_cycle(indexer, attachments)
        except KeyboardInterrupt:
            raise
        except Exception as exc:  # noqa: BLE001 — one bad cycle never kills the agent
            logger.error("gmail-poll: cycle failed: %s", exc, exc_info=True)
            do_sleep(stall_pause_s)
            cycles += 1
            if max_cycles is not None and cycles >= max_cycles:
                return 0  # the test seam bounds this path too
            continue
        heartbeat_email_summary(summary)
        with SessionLocal() as session:
            attempt = ConnectorStateRepo(session).attempt("email")
        if summary.state == "ok":
            pause = float(settings.gmail_poll_interval_s)
        elif summary.state == "backoff":
            # The runner already enforced the persisted backoff (the fetch was
            # skipped); re-applying backoff_seconds here would stack the waits
            # when a clock-edge skip lands mid-window. Sleep the normal
            # interval — the next cycle re-checks the window anyway.
            pause = float(settings.gmail_poll_interval_s)
        elif summary.state == "auth_error":
            pause = stall_pause_s  # user must re-consent; polling cannot help
        else:
            pause = max(float(settings.gmail_poll_interval_s), backoff_seconds(attempt))
        logger.info(
            "gmail-poll: state=%s listed=%s new=%s skipped=%s errors=%s; next poll in %.0fs",
            summary.state,
            summary.fetched,
            summary.created,
            summary.skipped,
            summary.errors,
            pause,
        )
        do_sleep(pause)
        cycles += 1
        if max_cycles is not None and cycles >= max_cycles:
            return 0  # test seam only (like run-connectors' PASSES); never set in production


# --- connectors (connectors ticket) -------------------------------------------


def _connectors_list() -> int:
    """Known connectors + their persisted state (name, enabled, cursor, health)."""
    import os  # noqa: PLC0415

    from brain.config import assert_not_live_store, get_settings  # noqa: F401 (guard below)
    from brain.connectors import all_names, ensure_registered, get
    from brain.connectors.state import ConnectorRecordRepo, ConnectorStateRepo

    settings = get_settings()
    assert_not_live_store(
        settings.database_url,
        settings.qdrant_collection,
        allow_live=os.environ.get("BRAIN_ALLOW_LIVE"),
    )
    enabled = set(settings.connectors_enabled)
    ensure_registered()
    names = sorted(set(all_names()) | {"email", "gcal", "gdrive", "gtasks"})
    print(
        f"{'name':<8}  {'enabled':<7}  {'registered':<10}  {'last ok':<20}  cursor  "
        "quarantined  last error"
    )
    with SessionLocal() as session:
        states = {row.name: row for row in ConnectorStateRepo(session).all()}
        counts = ConnectorRecordRepo(session).counts()
        quarantined = ConnectorRecordRepo(session).quarantined_counts()
        for name in names:
            state = states.get(name)
            try:
                get(name)  # registered on this install (adapters register on import)
                registered = "yes"
            except KeyError:
                registered = "no"
            last_ok_at = state.last_ok_at if state else None
            last_ok = last_ok_at.strftime("%Y-%m-%d %H:%M") if last_ok_at else "never"
            has_cursor = "yes" if state and state.cursor else "no"
            last_error = (state.last_error if state else "") or ""
            quarantined_n = quarantined.get(name, 0)
            print(
                f"{name:<8}  {'yes' if name in enabled else 'no':<8}  {registered:<10}  "
                f"{last_ok:<16}  {has_cursor:<6}  {quarantined_n:<11}  {last_error[:60]}"
            )
    records = counts
    if records:
        print("records: " + ", ".join(f"{source}={n}" for source, n in sorted(records.items())))
    return 0


def _max_apply_value(text_value: str) -> int:
    """--max-apply accepts 1..MAX_APPLY_CEILING only: a cap you can exceed by
    typo (or a stray ``-1``) is no cap. Argparse turns the failure into
    usage + exit 2 before any Gmail call happens. The range shares the
    service's MAX_APPLY_CEILING constant so the two can never drift."""
    from brain.services.gmail_labeler import MAX_APPLY_CEILING

    try:
        value = int(text_value)
    except ValueError:
        raise argparse.ArgumentTypeError(
            f"invalid int value: {text_value!r}"
        ) from None
    if not 1 <= value <= MAX_APPLY_CEILING:
        raise argparse.ArgumentTypeError(
            f"--max-apply must be in 1..{MAX_APPLY_CEILING} (got {value}) — "
            "the per-run cap has no unlimited mode"
        )
    return value


def _gmail_label(args: argparse.Namespace) -> int:
    """The SB/ labeler: rules over headers only (no Jev, no external LLM),
    dry-run by default, label-only writes capped at --max-apply, undo-able,
    ops-audited. Kid names come from CONFIRMED memory at runtime."""
    import os
    import uuid as _uuid

    from brain.config import assert_not_live_store, get_settings

    settings = get_settings()
    assert_not_live_store(
        settings.database_url,
        settings.qdrant_collection,
        allow_live=os.environ.get("BRAIN_ALLOW_LIVE"),
    )

    if args.auth:
        return _labeler_auth(settings)
    if args.undo:
        # Writes: behind the SAME consent gate as --apply (exit 78, zero
        # calls) and a modify-scoped client. NOT guarded by GMAIL_LABEL_ID:
        # undo reads the audit ledger and removes the batch's own label ids
        # — it never scans the label scope, and it must keep working even
        # if the user later unsets or changes the label (the escape hatch).
        if (gate := _labeler_gate(settings)) != 0:
            return gate
        from brain.ai.gmail import GmailAuthError
        from brain.services.gmail_labeler import undo_batch

        try:
            with SessionLocal() as session:
                result = undo_batch(
                    _labeler_client(settings, modify=True), args.undo, session=session
                )
        except GmailAuthError as exc:
            # Review R2: a revoked/403 token mid-undo aborts the whole batch
            # (undo_batch stops calling). Report what was done so far, name
            # --auth, exit 78 — never a traceback, never exit 1-as-success.
            partial = getattr(exc, "partial", None)
            if partial is not None:
                print(
                    f"gmail-label: undone {partial.undone} label(s), already "
                    f"absent {partial.already_absent} before the auth failure"
                )
            print(f"gmail-label: {exc}", file=sys.stderr)
            return 78
        # labeler/followups #2: the three buckets are printed and a partial
        # failure is VISIBLE — exit 1 with the failed ids listed, never a
        # silent 0.
        print(
            f"gmail-label: undone {result.undone} label(s), already absent "
            f"{result.already_absent} for batch {args.undo}"
        )
        if result.failed:
            print(f"gmail-label: FAILED ({len(result.failed)}): "
                  f"{', '.join(result.failed)}", file=sys.stderr)
            return 1
        return 0
    if args.audit:
        _labeler_audit(args.audit)
        return 0
    # The scope guard: the SCANNING paths (dry-run and --apply) walk the ONE
    # label the email connector syncs. With it unset or blank the planning
    # path would send a blank label id to Gmail — an obscure API error or a
    # WHOLE-MAILBOX scan. Refuse BEFORE the token read: exit 78, the env var
    # named, zero Gmail calls. (--undo/--audit are deliberately NOT guarded:
    # they read the ledger / remove a batch's own labels, never scan.)
    if not (settings.gmail_label_id or "").strip():
        print(
            "gmail-label: GMAIL_LABEL_ID is not set — the labeler only scans "
            "inside the label the email connector syncs (its immutable Gmail "
            "label id, e.g. GMAIL_LABEL_ID=Label_1234567890). Set it in .env "
            "and re-run. Nothing was read or written.",
            file=sys.stderr,
        )
        return 78

    from brain.services.gmail_labeler import load_rules

    if args.apply:
        # The consent check FIRST — before ANY Gmail call (reads included):
        # a readonly-only token fails with the re-consent message and zero
        # calls. The live token holds readonly only until `brain gmail-label
        # --auth` is run by the USER.
        if (gate := _labeler_gate(settings)) != 0:
            return gate

    from brain.repositories.memory import MemoryRepository
    from brain.services.gmail_labeler import plan_labels
    from brain.services.memory import MemoryService
    from brain.services.triage import confirmed_kid_names

    rules = load_rules(settings.gmail_labeler_rules_path)
    with SessionLocal() as session:
        kid_names = confirmed_kid_names(
            MemoryService(MemoryRepository(session)).overview(status="confirmed", q=None)
        )
    client = _labeler_client(settings, modify=False)
    plans = plan_labels(
        client, rules=rules, kid_names=kid_names,
        label_id=settings.gmail_label_id, query=settings.gmail_query,
        max_scan=args.max_scan,
    )
    print(f"gmail-label: {len(plans)} message(s) to label "
          f"(kids: {len(kid_names)}, scan cap {args.max_scan})")
    for plan in plans:
        print(f"  {plan.gmail_id} -> {plan.label_path} ({plan.source})")
    if not args.apply:
        print("gmail-label: dry-run — zero writes (pass --apply, capped at "
              f"--max-apply {args.max_apply})")
        return 0
    from brain.services.gmail_labeler import apply_labels

    batch_id = _uuid.uuid4().hex[:12]
    # labeler/followups #3: the undo handle exists BEFORE the first modify
    # call — a crash mid-run still leaves the user a way back.
    print(f"gmail-label: batch {batch_id} — undo with: "
          f"brain gmail-label --undo {batch_id}")
    applied = apply_labels(
        _labeler_client(settings, modify=True), plans,
        max_apply=args.max_apply, batch_id=batch_id,
    )
    print(f"gmail-label: applied {applied} label(s), batch {batch_id} "
          f"(undo: brain gmail-label --undo {batch_id})")
    return 0


def _labeler_gate(settings: Any) -> int:
    """0 when the keychain token's GRANTED scopes include gmail.modify;
    78 (with the re-consent message) otherwise. Runs before ANY Gmail call —
    a revoked/invalid_grant token is a clean exit, never a traceback."""
    from brain.ai.gmail import GmailAuthError
    from brain.services.gmail_labeler import token_grants_modify

    try:
        ok = token_grants_modify(
            _labeler_token(), client_config=_labeler_client_config(settings)
        )
    except GmailAuthError as exc:
        print(f"gmail-label: {exc}", file=sys.stderr)
        return 78
    if ok:
        return 0
    print(
        "gmail-label: the keychain token does not grant gmail.modify — "
        "run `brain gmail-label --auth` to consent (readonly + labels); "
        "nothing was read or written",
        file=sys.stderr,
    )
    return 78


def _labeler_token() -> str:
    """The LABELER'S OWN keychain refresh token (its own keychain item —
    never the sync token's item; a missing item is an error, never a
    fallback)."""
    from brain.services.gmail_labeler import (
        LABELER_KEYCHAIN_ACCOUNT,
        LABELER_KEYCHAIN_SERVICE,
    )
    from brain.services.keychain import Keychain

    token = Keychain(service=LABELER_KEYCHAIN_SERVICE).read(LABELER_KEYCHAIN_ACCOUNT)
    if not token:
        print(
            "gmail-label: no refresh token in the labeler's own keychain item "
            f"(service {LABELER_KEYCHAIN_SERVICE}, account "
            f"{LABELER_KEYCHAIN_ACCOUNT}) — run `brain gmail-label --auth` "
            "first (the sync token's item is never used)",
            file=sys.stderr,
        )
        raise SystemExit(78)
    return token


def _labeler_client_config(settings: Any) -> dict[str, str]:
    """The OAuth client config (id/secret/token_uri) from the secret file."""
    from brain.connectors.google.base import oauth_client_config

    return oauth_client_config(settings)


def _labeler_auth(settings: Any) -> int:
    """The LABELER's consent: GMAIL_LABEL_SCOPES (readonly + gmail.modify).

    The token goes in the LABELER'S OWN keychain item (service
    ``second-brain-gmail-labeler``) — the sync token's item is never read,
    written or overwritten here. Before the browser opens, GMAIL_ACCOUNT
    (``settings.gmail_account``) must name the expected mailbox; after
    consent, the profile's emailAddress must match it (case- and
    whitespace-insensitive) or NOTHING is stored — a consent for the wrong
    Google account must never be able to relabel the wrong mailbox.
    The re-consent is a USER action.
    """
    from brain.ai.gmail import GMAIL_LABEL_SCOPES

    expected = (settings.gmail_account or "").strip()
    if not expected:
        print(
            "gmail-label --auth: GMAIL_ACCOUNT is not set — add the Gmail "
            "address the connector syncs to .env (e.g. "
            "GMAIL_ACCOUNT=you@gmail.com) and re-run. Nothing was consented "
            "or stored.",
            file=sys.stderr,
        )
        return 78
    path = settings.gmail_credentials_path
    if not path:
        print(
            "gmail-label --auth: no client secret — set GMAIL_CREDENTIALS_PATH. "
            "Keep the file OUTSIDE the repo.",
            file=sys.stderr,
        )
        return 78
    flow = _build_oauth_flow(path, scopes=list(GMAIL_LABEL_SCOPES))
    print(
        "gmail-label --auth: opening the browser for Google consent "
        f"(scopes: {', '.join(GMAIL_LABEL_SCOPES)})…"
    )
    try:
        flow.run_local_server(port=0)
        token = flow.credentials.refresh_token
    except Exception as exc:  # noqa: BLE001 — consent windows can be closed
        print(f"gmail-label --auth: consent failed: {exc}", file=sys.stderr)
        return 78
    if not token:
        print("gmail-label --auth: consent returned no refresh token", file=sys.stderr)
        return 78
    # Account check BEFORE anything is stored: the profile of the JUST-
    # consented credentials must be the configured account.
    try:
        profile_email = _labeler_profile_email(flow.credentials)
    except Exception as exc:  # noqa: BLE001 — any profile failure refuses
        print(
            f"gmail-label --auth: could not verify the consented account "
            f"(getProfile failed: {exc}); expected GMAIL_ACCOUNT={expected}. "
            "Nothing was stored.",
            file=sys.stderr,
        )
        return 78
    if profile_email.strip().lower() != expected.strip().lower():
        print(
            f"gmail-label --auth: the consented account is {profile_email!r} "
            f"but GMAIL_ACCOUNT is {expected!r} — refusing to store the "
            "token (re-run with the matching account). Nothing was stored.",
            file=sys.stderr,
        )
        return 78
    from brain.services.gmail_labeler import (
        LABELER_KEYCHAIN_ACCOUNT,
        LABELER_KEYCHAIN_SERVICE,
    )
    from brain.services.keychain import Keychain

    Keychain(service=LABELER_KEYCHAIN_SERVICE).store(LABELER_KEYCHAIN_ACCOUNT, token)
    print(
        f"gmail-label --auth: refresh token stored in the labeler's OWN "
        f"keychain item (service {LABELER_KEYCHAIN_SERVICE}, account "
        f"{LABELER_KEYCHAIN_ACCOUNT}; the sync token's item was not "
        f"touched) for scopes: {', '.join(GMAIL_LABEL_SCOPES)}"
    )
    return 0


def _labeler_profile_email(credentials: Any) -> str:
    """users.getProfile().emailAddress over the JUST-CONSENTED credentials
    (the consent's own grant — never a stored token). Test seam: tests
    monkeypatch this instead of touching Google."""
    import googleapiclient.discovery  # noqa: PLC0415 (lazy, like the flow)

    service = googleapiclient.discovery.build(
        "gmail", "v1", credentials=credentials, cache_discovery=False
    )
    try:
        raw = service.users().getProfile(userId="me").execute()
    finally:
        service.close()
    return str((raw or {}).get("emailAddress") or "")


def _labeler_client(settings: Any, *, modify: bool) -> Any:
    """RestGmailClient over the LABELER'S OWN keychain token (never the
    sync token's item), paced by the same quota-hygiene interval as the
    connector. ``modify=False`` (the PLANNING
    client — dry-run and the plan phase of --apply) requests [GMAIL_SCOPE]
    readonly ONLY: the dry-run is the first thing the user runs and today's
    token holds readonly only, so asking for modify at its refresh would
    break it with invalid_scope. ``modify=True`` (the WRITE client — apply
    and undo, both behind the gate) requests GMAIL_LABEL_SCOPES."""
    from brain.ai.gmail import GMAIL_LABEL_SCOPES, GMAIL_SCOPE, RestGmailClient
    from brain.connectors.google.base import default_service_factory

    token = _labeler_token()
    scopes = list(GMAIL_LABEL_SCOPES) if modify else [GMAIL_SCOPE]
    service = default_service_factory(token, scopes, "gmail", "v1")
    return RestGmailClient(
        service=service,
        min_interval_s=settings.gmail_min_call_interval_s,
    )


def _labeler_audit(batch: str) -> None:
    from sqlalchemy import text

    with SessionLocal() as session:
        if batch == "ALL":
            rows = session.execute(
                text(
                    "SELECT detail->>'batch' AS batch, kind, detail "
                    "FROM ops_events WHERE component = 'gmail-label' "
                    "AND kind IN ('apply', 'undo') ORDER BY ts"
                )
            ).all()
        else:
            rows = session.execute(
                text(
                    "SELECT detail->>'batch' AS batch, kind, detail "
                    "FROM ops_events WHERE component = 'gmail-label' "
                    "AND kind IN ('apply', 'undo') AND detail->>'batch' = :batch "
                    "ORDER BY ts"
                ),
                {"batch": batch},
            ).all()
    for row in rows:
        detail = row.detail or {}
        print(f"  [{row.batch}] {row.kind}: {detail.get('gmail_id')} -> "
              f"{detail.get('label_path', detail.get('label_id'))}")


def _build_connector(name: str, session: Any = None, *, dry_run: bool = False) -> Any:
    """Instantiate the registry's connector with production seams: the shared
    keychain token (same item as gmail-auth — one OAuth client, one token)
    and the default google service factory. Registry aliases resolve.

    For the email source a session wires the C5 legacy-seed + fast-path seams:
    the first sync (and every full resync) copies the old seen-ledger into
    connector_records (idempotent), and known gmail ids skip the GET.

    ``dry_run=True`` (review B1) wires READ-ONLY seams only: no legacy seed
    runs and the Message-ID backstop only REPORTS its decision — --dry-run
    writes nothing (no ledger, no cursor, no state, no items).
    """
    from brain.connectors import get
    from brain.connectors.google.base import default_service_factory
    from brain.services.keychain import GMAIL_ACCOUNT, Keychain

    cls: Any = get(name)
    token = Keychain().read(GMAIL_ACCOUNT)
    if not token:
        print(
            "connectors: no Google refresh token in the keychain — run "
            "`brain connectors auth google` first",
            file=sys.stderr,
        )
        raise SystemExit(78)
    kwargs: dict[str, Any] = {
        "token_loader": lambda: token,
        "service_factory": default_service_factory,
    }
    if session is not None and name in ("email", "gmail"):
        if dry_run:
            # --dry-run writes NOTHING: no seed; the backstop only reports.
            from brain.connectors.google.gmail import rfc_backstop_report

            kwargs["rfc_backstop"] = lambda rfc, gmail_id: rfc_backstop_report(
                session, rfc, gmail_id
            )
        else:
            from brain.connectors.google.gmail import (
                rfc_backstop_put,
                seed_connector_records_from_email_ledger,
            )
            from brain.connectors.state import ConnectorRecordRepo

            records = ConnectorRecordRepo(session)
            kwargs["legacy_seed"] = lambda: seed_connector_records_from_email_ledger(session)
            kwargs["is_known"] = lambda gmail_id: records.get("email", gmail_id) is not None
            kwargs["rfc_backstop"] = lambda rfc, gmail_id: rfc_backstop_put(session, rfc, gmail_id)
    return cls(**kwargs)


def _connectors_sync(args: argparse.Namespace) -> int:
    """One runner cycle (or a dry-run fetch) for one enabled connector."""
    import os  # noqa: PLC0415

    from brain.config import assert_not_live_store, get_settings
    from brain.connectors import all_names, ensure_registered
    from brain.connectors import get as get_connector
    from brain.connectors.base import ConnectorError
    from brain.connectors.runner import ConnectorRunner
    from brain.connectors.state import ConnectorRecordRepo, ConnectorStateRepo

    settings = get_settings()
    assert_not_live_store(
        settings.database_url,
        settings.qdrant_collection,
        allow_live=os.environ.get("BRAIN_ALLOW_LIVE"),
    )
    canonical = "email" if args.name.lower() == "gmail" else args.name.lower()
    if canonical not in settings.connectors_enabled:
        print(
            f"connectors: {args.name!r} is not enabled — add it to BRAIN_CONNECTORS "
            f"(currently: {','.join(settings.connectors_enabled) or 'none'})",
            file=sys.stderr,
        )
        return 78
    ensure_registered()
    try:
        get_connector(args.name)  # resolves aliases; KeyError when absent
    except KeyError:
        print(
            f"connectors: {args.name!r} is enabled but not available on this install "
            f"(registered: {', '.join(all_names()) or 'none'})",
            file=sys.stderr,
        )
        return 78
    with SessionLocal() as session:
        from brain.api import deps  # noqa: PLC0415 (deferred: shared singletons)
        from brain.repositories.items import ItemsRepository  # noqa: PLC0415

        if args.dry_run:
            connector = _build_connector(args.name, session=session, dry_run=True)
            state = ConnectorStateRepo(session).get(connector.name)
            try:
                result = connector.fetch(state.cursor if state else None, limit=args.limit)
            except ConnectorError as exc:
                # The dry-run path has no runner to map connector failures;
                # map them exactly like the sync path's exit codes: auth and
                # not-configured refuse with 78, transient exits 1 — one
                # stderr line, never a traceback (review follow-up 1).
                code, message = _dry_run_failure(args.name, exc)
                print(message, file=sys.stderr)
                try:
                    connector.close()
                except Exception:  # noqa: BLE001 — never mask the fetch failure
                    pass
                return code
            print(
                f"dry-run: {len(result.records)} record(s), next_cursor={result.next_cursor!r}, "
                f"reset={result.reset} (nothing written)"
            )
            for record in result.records:
                when = record.occurred_at.strftime("%Y-%m-%d %H:%M") if record.occurred_at else "-"
                print(
                    f"  {record.source:<8} {record.external_id:<24} v={record.version!s:<12} "
                    f"{when} {'DELETED ' if record.deleted else ''}{record.title[:48]}"
                )
            connector.close()
            return 0

        ingest = IngestService(
            ItemsRepository(session),
            deps.get_classifier(),
            deps.get_item_indexer(),
        )
        connector = _build_connector(args.name, session=session)
        state_repo = ConnectorStateRepo(session)
        runner = ConnectorRunner(
            connector,
            ingest=ingest,
            repo=ItemsRepository(session),
            state=state_repo,
            records=ConnectorRecordRepo(session),
        )
        summary = runner.run(limit=args.limit)
        if args.drain:
            # First sync of a large source (e.g. 3,000 Drive files) needs more
            # than one batch: loop while the cursor keeps moving. A batch that
            # leaves the cursor unchanged means drained (or stuck); a failed/
            # re-authing batch stops the drain. 500 batches is a runaway
            # guard, not a knob.
            # Exception (finding 4): a batch the source TRUNCATED without a
            # resume cursor (email's query path: next_cursor=None, more=True)
            # is not 'drained' — the source said it has more. The loop keeps
            # going while summary.more is set, even when the cursor is
            # None -> None (each re-list skips what the ledger already has).
            batches = 1
            previous_cursor = _cursor_of(state_repo, summary.connector)
            while summary.state == "ok" and summary.cursor_saved and batches < 500:
                nxt = runner.run(limit=args.limit)
                summary = _merge_summary(summary, nxt)
                batches += 1
                current_cursor = _cursor_of(state_repo, summary.connector)
                if current_cursor == previous_cursor and not summary.more:
                    break  # no cursor movement and nothing more: drained
                previous_cursor = current_cursor
        connector.close()
    print(
        f"connectors sync {summary.connector}: state={summary.state} "
        f"created={summary.created} deduped={summary.deduped} updated={summary.updated} "
        f"deleted={summary.deleted} "
        f"skipped={summary.skipped} errors={summary.errors} "
        f"cursor_saved={summary.cursor_saved}"
        + (f" retry_in_s={summary.retry_in_s:.0f}" if summary.state == "backoff" else "")
        + (f" error={summary.error}" if summary.error else "")
    )
    # backoff = the persisted per-connector backoff doing its job (the fetch
    # was deliberately skipped) — success for the scheduler, not a failure.
    return {"ok": 0, "retry_error": 1, "auth_error": 78, "backoff": 0}.get(summary.state, 1)


def _dry_run_failure(name: str, exc: Exception) -> tuple[int, str]:
    """Map a dry-run fetch failure to (exit code, one stderr line).

    Mirrors the sync path's summary-based codes: auth -> the re-auth
    message, 78; transient (retryable) -> 1; any other connector error is
    a not-configured/misconfigured refusal -> 78 (same family as the
    missing-token and not-registered refusals above).
    """
    from brain.connectors.base import ConnectorAuthError, ConnectorRetryableError

    if isinstance(exc, ConnectorAuthError):
        return (
            78,
            f"connectors: {name}: auth (needs re-consent): {exc} — run "
            f"`brain connectors auth google` first",
        )
    if isinstance(exc, ConnectorRetryableError):
        return (1, f"connectors: {name}: retryable: {exc}")
    return (78, f"connectors: {name}: {exc}")


def _cursor_of(state_repo: Any, connector: str) -> str | None:
    """The persisted cursor for a connector (None when it never synced)."""
    state = state_repo.get(connector)
    return state.cursor if state else None


def _merge_summary(acc: Any, nxt: Any) -> Any:
    """Accumulate one more drained batch into the running summary (--drain)."""
    from brain.connectors.runner import SyncSummary

    return SyncSummary(
        connector=nxt.connector,
        state=nxt.state,
        fetched=acc.fetched + nxt.fetched,
        created=acc.created + nxt.created,
        deduped=acc.deduped + nxt.deduped,
        updated=acc.updated + nxt.updated,
        deleted=acc.deleted + nxt.deleted,
        skipped=acc.skipped + nxt.skipped,
        errors=acc.errors + nxt.errors,
        reset=nxt.reset,
        more=nxt.more,
        cursor_saved=nxt.cursor_saved,
        error=nxt.error,
        retry_in_s=nxt.retry_in_s,
    )


# pragma: no cover - needs google libs
def _connectors_adopt(args: argparse.Namespace) -> int:
    """One-time legacy adoption: teach the ledger about EXISTING items.

    Same gates as sync (live-store guard, enabled, registered, consent) plus
    the adopt-specific cursor guard. Exit codes mirror sync: 0 ok, 78
    refused/auth (nothing written), 1 retryable/other error.
    """
    import os  # noqa: PLC0415

    from brain.config import assert_not_live_store, get_settings
    from brain.connectors import all_names, ensure_registered
    from brain.connectors import get as get_connector
    from brain.connectors.adopt import adopt_connector, format_report, undo_adopt

    settings = get_settings()
    assert_not_live_store(
        settings.database_url,
        settings.qdrant_collection,
        allow_live=os.environ.get("BRAIN_ALLOW_LIVE"),
    )
    canonical = "email" if args.name.lower() == "gmail" else args.name.lower()
    if canonical not in settings.connectors_enabled:
        print(
            f"connectors: {args.name!r} is not enabled — add it to BRAIN_CONNECTORS "
            f"(currently: {','.join(settings.connectors_enabled) or 'none'})",
            file=sys.stderr,
        )
        return 78
    ensure_registered()
    try:
        get_connector(args.name)  # resolves aliases; KeyError when absent
    except KeyError:
        print(
            f"connectors: {args.name!r} is enabled but not available on this install "
            f"(registered: {', '.join(all_names()) or 'none'})",
            file=sys.stderr,
        )
        return 78

    if args.undo:
        with SessionLocal() as session:
            report = undo_adopt(canonical, session)
        print(report.summary_line())
        return 1 if report.refused else 0

    from brain.connectors.base import ConnectorAuthError, ConnectorError  # noqa: PLC0415

    connector = _build_connector(args.name)  # SystemExit(78) without a token
    try:
        with SessionLocal() as session:
            report = adopt_connector(connector, session, dry_run=args.dry_run, limit=args.limit)
    except ConnectorAuthError as exc:
        print(
            f"connectors adopt: re-consent needed — run `brain connectors auth google` ({exc})",
            file=sys.stderr,
        )
        return 78
    except ConnectorError as exc:
        print(f"connectors adopt: {exc}", file=sys.stderr)
        return 1
    finally:
        connector.close()
    print(format_report(report))
    if report.wrote:
        print(
            f"wrote {len(report.adopted)} ledger row(s) (ops event recorded). "
            f"Undo: brain connectors adopt {canonical} --undo"
        )
    return 78 if report.refused_cursor else 0


def _build_connectors_oauth_flow(client_secret_path: str, scopes: list[str]) -> Any:
    """Build the installed-app OAuth flow for the union of enabled scopes
    (patched with fakes in tests; never run against a real account there)."""
    try:
        from google_auth_oauthlib.flow import InstalledAppFlow
    except ImportError as exc:
        raise SystemExit(
            "connectors: google libraries not installed — run `pip install -e '.[gmail]'`"
        ) from exc
    return InstalledAppFlow.from_client_secrets_file(client_secret_path, scopes=scopes)


def _connectors_auth(args: argparse.Namespace) -> int:
    """Google consent for the UNION of enabled connectors' scopes; stores ONLY
    the refresh token in the login keychain (same item as gmail-auth: one
    OAuth client, one token, re-consent unions the scopes)."""
    from brain.config import get_settings
    from brain.connectors.google.base import scopes_for
    from brain.services.keychain import GMAIL_ACCOUNT, Keychain

    if args.provider != "google":  # pragma: no cover - argparse choices guard
        print(f"connectors auth: unknown provider {args.provider!r}", file=sys.stderr)
        return 78
    settings = get_settings()
    enabled = settings.connectors_enabled
    if not enabled:
        print(
            "connectors auth: no connectors enabled — set BRAIN_CONNECTORS "
            '(e.g. "email,gcal,gdrive,gtasks") first',
            file=sys.stderr,
        )
        return 78
    scopes = scopes_for(enabled)
    path = args.client_secret or settings.gmail_credentials_path
    if not path:
        print(
            "connectors auth: no client secret — pass --client-secret /path/to/secret.json "
            "(or set GMAIL_CREDENTIALS_PATH). Keep the file OUTSIDE the repo.",
            file=sys.stderr,
        )
        return 78
    if not Path(path).exists():
        print(f"connectors auth: client secret file not found: {path}", file=sys.stderr)
        return 78
    flow = _build_connectors_oauth_flow(path, scopes)
    print(
        "connectors auth: opening the browser for Google consent "
        f"(read-only scopes: {', '.join(scopes)})…"
    )
    try:
        creds = flow.run_local_server(port=0)
    except KeyboardInterrupt:
        print("connectors auth: cancelled", file=sys.stderr)
        return 130
    token = creds.refresh_token
    if not token:
        print(
            "connectors auth: Google did not return a refresh token — revoke the app at "
            "https://myaccount.google.com/permissions and re-run (prompt=consent).",
            file=sys.stderr,
        )
        return 78
    Keychain().store(GMAIL_ACCOUNT, token)
    print(
        f"connectors auth: refresh token stored in the login keychain "
        f"(service second-brain-gmail, account {GMAIL_ACCOUNT}) for scopes: "
        f"{', '.join(scopes)}"
    )
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
