"""``brain`` command line interface (entry point: ``brain = brain.cli:main``)."""

from __future__ import annotations

import argparse
import sys
import tempfile
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from brain.ai.embedder import Embedder, EmbedderError
from brain.ai.vectorstore import QdrantVectorStore, VectorStore, VectorStoreError
from brain.config import get_settings, unwrap_secret
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
from brain.services.paste import _calendar_one_liner


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
    subparsers.add_parser(
        "normalize-calendar",
        help="Rewrite legacy calendar items to the human one-liner raw_text",
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
    args = parser.parse_args(argv)

    if args.command == "reindex":
        return _reindex(batch_size=args.batch_size)
    if args.command == "telegram":
        return _telegram(whoami_hint=args.whoami_hint)
    if args.command == "normalize-calendar":
        return _normalize_calendar()
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
        parser.error(f"unknown memory command {args.memory_command!r}")
        return 2
    if args.command == "eval-extraction":
        return _eval_extraction(args)
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
    finally:
        session.close()


def _telegram(whoami_hint: bool) -> int:
    """Run the Telegram bot (long polling); --whoami-hint prints setup help."""
    if whoami_hint:
        print(WHOAMI_HINT)
        return 0

    from brain.telegram_bot import run as run_bot  # noqa: PLC0415  (deferred import)

    return run_bot(get_settings())


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
            item_type=None, project=None, status=None, limit=_PAGE, offset=offset
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
            item.fingerprint = content_fingerprint(
                one_liner, item.occurred_at, item.source
            )
            normalized += 1
        offset += len(batch)
    repo.commit()
    print(f"normalized {normalized} calendar item(s) (run 'brain reindex' to re-embed)")
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
