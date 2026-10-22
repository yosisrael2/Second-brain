"""Ingestion use cases: capture, classify, route, and index (Service layer)."""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Protocol

from brain import ops, schemas
from brain.ai.classify import route
from brain.ai.embedder import EmbedderError
from brain.ai.jev import JevError
from brain.ai.vectorstore import VectorStoreError
from brain.db.models import MemoryItem
from brain.services.dedupe import content_fingerprint
from brain.services.indexing import ItemIndexerProto
from brain.services.items import ItemsRepositoryProto
from brain.services.paste import ParsedPaste

logger = logging.getLogger(__name__)


class ClassifierProto(Protocol):
    """Interface IngestService needs from a classifier (Strategy)."""

    def classify(self, text: str) -> dict[str, Any]: ...


class IngestService:
    """Captures memory items; Jev/index failures never block capture.

    Create persists the item first, then (optionally) fills classification
    fields, then indexes the text in the vector store. Classifier or indexing
    failures (:class:`~brain.ai.jev.JevError`,
    :class:`~brain.ai.embedder.EmbedderError`,
    :class:`~brain.ai.vectorstore.VectorStoreError`) are logged and skipped:
    the item stays in the inbox and is picked up later by ``brain reindex``.
    """

    def __init__(
        self,
        repo: ItemsRepositoryProto,
        classifier: ClassifierProto,
        indexer: ItemIndexerProto,
    ) -> None:
        self._repo = repo
        self._classifier = classifier
        self._indexer = indexer

    def create(self, data: schemas.ItemCreate, classify: bool = True) -> MemoryItem:
        """Create a memory item, classify it on request, and index it.

        Classification (when ``classify`` and the caller did not send ``type``)
        fills ONLY the fields the caller did not explicitly send — an explicit
        ``status``/``importance``/``actionability``/``confidence``/``project``
        is never overwritten (``model_fields_set`` distinguishes sent from
        defaulted fields).
        """
        item = MemoryItem(
            raw_text=data.raw_text,
            source=data.source,
            type=data.type,
            project=data.project,
            importance=data.importance,
            actionability=data.actionability,
            confidence=data.confidence,
            status=data.status,
            occurred_at=data.occurred_at,
            meta=data.meta,
        )
        item.fingerprint = content_fingerprint(
            data.raw_text, data.occurred_at, data.source
        )
        if (existing := self._repo.get_by_fingerprint(item.fingerprint)) is not None:
            # Exact duplicate: keep the original, surface it with a flag, and
            # skip classification/indexing entirely (nothing new to process).
            ops.record_event(
                "ingest",
                "dedupe_hit",
                ok=True,
                detail={"existing_id": str(existing.id)},
            )
            return self._dedupe_snapshot(existing)
        self._repo.add(item)
        if classify:
            with ops.span("ingest", "classify", edge="ingest->jev") as span:
                self._apply_classification(item, explicit=frozenset(data.model_fields_set))
                span.detail = {
                    "item_id": str(item.id),
                    "chars": len(item.raw_text),
                    "confidence": item.confidence,
                }
        self._index(item)
        with ops.span("ingest", "persist", edge="ingest->postgres") as span:
            self._repo.commit()
            span.detail = {"item_id": str(item.id), "chars": len(item.raw_text)}
        return item

    def _dedupe_snapshot(self, existing: MemoryItem) -> MemoryItem:
        """Detached copy of the existing item, flagged ``deduplicated``.

        A copy (not the session object) so the flag reflects THIS call's
        outcome: the same row returned by a later non-duplicate call must
        not carry a stale True.
        """
        snapshot = MemoryItem(
            raw_text=existing.raw_text,
            source=existing.source,
            type=existing.type,
            project=existing.project,
            importance=existing.importance,
            actionability=existing.actionability,
            confidence=existing.confidence,
            status=existing.status,
            occurred_at=existing.occurred_at,
            meta=existing.meta,
            fingerprint=existing.fingerprint,
        )
        snapshot.id = existing.id
        snapshot.created_at = existing.created_at
        snapshot.updated_at = existing.updated_at
        snapshot.deduplicated = True
        return snapshot

    def capture_parsed(
        self,
        parsed: ParsedPaste,
        *,
        source: str | None = None,
        extra_meta: dict[str, Any] | None = None,
        occurred_at: datetime | None = None,
    ) -> list[MemoryItem]:
        """Create one item per parsed chunk (shared capture path).

        Used by POST /capture and the Telegram bot: ``source`` defaults to
        the detected paste kind, ``extra_meta`` is merged over each chunk's
        meta, and ``occurred_at`` (e.g. a Telegram forward date) overrides
        each chunk's parsed instant when given. Classification/indexing
        failures never block creation (same contract as :meth:`create`).
        """
        items: list[MemoryItem] = []
        for chunk in parsed.chunks:
            items.append(
                self.create(
                    schemas.ItemCreate(
                        raw_text=chunk.raw_text,
                        source=source or parsed.kind,
                        occurred_at=occurred_at or chunk.occurred_at,
                        meta={**chunk.meta, **(extra_meta or {})},
                    )
                )
            )
        return items

    def reindex(self, batch_size: int = 500) -> int:
        """Full vector rebuild from Postgres; returns the number indexed.

        Probes the embedder first, so an unreachable embedder raises
        :class:`EmbedderError` before the collection is dropped (a failed
        rebuild never destroys the existing vectors). Then purges the
        collection — removing ghost vectors for items deleted or missing
        from Postgres — and re-embeds + upserts one whole DB batch per
        embed/upsert round-trip. Intended for the ``brain reindex`` CLI;
        raises EmbedderError/VectorStoreError on infrastructure failure (the
        CLI turns that into a clean exit code).
        """
        self._indexer.probe()  # fail before the destructive purge
        self._indexer.purge()
        offset = 0
        indexed = 0
        while True:
            batch = self._repo.list(
                item_type=None,
                project=None,
                status=None,
                limit=batch_size,
                offset=offset,
            )
            if not batch:
                return indexed
            indexed += self._indexer.index_batch(batch)
            offset += len(batch)

    def _apply_classification(self, item: MemoryItem, *, explicit: frozenset[str]) -> None:
        """Fill unclassified fields from the classifier; never raises.

        Fields the caller explicitly sent are left untouched (only ``type``
        absent triggers classification at all); ``status`` is routed only when
        the caller did not send one.
        """
        if "type" in explicit or item.type is not None:
            return  # caller classified the item explicitly; respect that
        try:
            result = self._classifier.classify(item.raw_text)
        except JevError as exc:
            logger.warning("ingest: classifier unavailable (%s); item stays in inbox", exc)
            return
        if result["type"] is None:
            return  # classifier fallback: nothing to apply
        item.type = result["type"]
        if "project" not in explicit:
            item.project = result["project"]
        if "importance" not in explicit:
            item.importance = result["importance"]
        if "actionability" not in explicit:
            item.actionability = result["actionability"]
        if "confidence" not in explicit:
            item.confidence = result["confidence"]
        if "status" not in explicit:
            item.status = route(result["confidence"])

    def _index(self, item: MemoryItem) -> None:
        """Index the item; index failures defer to ``reindex`` (never block)."""
        try:
            self._indexer.index(item)
        except (EmbedderError, VectorStoreError) as exc:
            logger.warning(
                "ingest: indexing failed for item %s (%s); it will be indexed by 'brain reindex'",
                item.id,
                exc,
            )
