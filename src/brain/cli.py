"""``brain`` command line interface (entry point: ``brain = brain.cli:main``)."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from typing import Any

from brain.ai.embedder import EmbedderError
from brain.ai.vectorstore import QdrantVectorStore, VectorStoreError
from brain.config import get_settings
from brain.db.session import SessionLocal
from brain.repositories.items import ItemsRepository
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
    embedder: Any | None = None,
    store: Any | None = None,
) -> int:
    """Index every stored item into Qdrant; returns a process exit code.

    ``repo``/``embedder``/``store`` are injectable for tests.
    """
    settings = get_settings()
    if store is None:
        store = QdrantVectorStore(
            url=settings.qdrant_url,
            api_key=settings.qdrant_api_key.get_secret_value() if settings.qdrant_api_key else None,
            collection=settings.qdrant_collection,
            timeout_s=settings.qdrant_timeout_s,
        )
    if embedder is None:
        from brain.ai.embedder import OpenAIEmbedder  # late import keeps CLI startup light

        embedder = OpenAIEmbedder(
            api_key=settings.openai_api_key.get_secret_value() if settings.openai_api_key else None,
            model=settings.openai_embedding_model,
            base_url=settings.openai_base_url,
            timeout_s=settings.openai_timeout_s,
        )
    if repo is None:
        repo = ItemsRepository(SessionLocal())
    service = IngestService(repo, classifier=_noop_classifier(), embedder=embedder, store=store)
    try:
        indexed = service.reindex(batch_size=batch_size)
    except (EmbedderError, VectorStoreError) as exc:
        print(f"reindex failed: {exc}", file=sys.stderr)
        return 1
    finally:
        store.close()
        embedder.close()
    print(f"indexed {indexed} item(s) into collection '{settings.qdrant_collection}'")
    return 0


class _noop_classifier:
    """Classifier stand-in: reindex never classifies, only embeds."""

    def classify(self, text: str) -> dict[str, object]:
        raise AssertionError("reindex must not classify")  # pragma: no cover


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
