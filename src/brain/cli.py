"""``brain`` command line interface (entry point: ``brain = brain.cli:main``)."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence

from brain.ai.embedder import Embedder, EmbedderError
from brain.ai.vectorstore import QdrantVectorStore, VectorStore, VectorStoreError
from brain.config import get_settings, unwrap_secret
from brain.db.session import SessionLocal
from brain.factories import build_embedder
from brain.repositories.items import ItemsRepository
from brain.services.indexing import QdrantItemIndexer
from brain.services.ingest import IngestService
from brain.services.items import ItemsRepositoryProto


def main(argv: Sequence[str] | None = None) -> int:
    """Run the brain CLI; returns a process exit code."""
    parser = argparse.ArgumentParser(prog="brain", description="Second Brain maintenance commands")
    subparsers = parser.add_subparsers(dest="command", required=True)
    reindex = subparsers.add_parser("reindex", help="Rebuild Qdrant vectors from Postgres")
    reindex.add_argument("--batch-size", type=int, default=500, help="Items per DB page")
    args = parser.parse_args(argv)

    if args.command == "reindex":
        return _reindex(batch_size=args.batch_size)
    parser.error(f"unknown command {args.command!r}")  # pragma: no cover - argparse guards
    return 2


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
            f"reindex failed: {exc}. The collection is now PARTIAL; "
            "rerun 'brain reindex' to finish rebuilding.",
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


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
