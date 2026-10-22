"""``brain`` command line interface (entry point: ``brain = brain.cli:main``)."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from zoneinfo import ZoneInfo

from brain.ai.embedder import Embedder, EmbedderError
from brain.ai.vectorstore import QdrantVectorStore, VectorStore, VectorStoreError
from brain.config import get_settings, unwrap_secret
from brain.db.session import SessionLocal
from brain.factories import build_embedder
from brain.repositories.items import ItemsRepository
from brain.services.dedupe import content_fingerprint
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
    args = parser.parse_args(argv)

    if args.command == "reindex":
        return _reindex(batch_size=args.batch_size)
    if args.command == "telegram":
        return _telegram(whoami_hint=args.whoami_hint)
    if args.command == "normalize-calendar":
        return _normalize_calendar()
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


def _telegram(whoami_hint: bool) -> int:
    """Run the Telegram bot (long polling); --whoami-hint prints setup help."""
    if whoami_hint:
        print(WHOAMI_HINT)
        return 0

    from brain.telegram_bot import run as run_bot  # noqa: PLC0415  (deferred import)

    return run_bot(get_settings())


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
