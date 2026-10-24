"""Repository for MemoryItem persistence (Repository pattern)."""

from __future__ import annotations

import re
import uuid
from collections.abc import Sequence
from datetime import datetime
from typing import Any

from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import Session

from brain.db.enums import FTS_LANGUAGE, ItemStatus, ItemType
from brain.db.models import ItemRelation, ItemSource, MemoryItem

SOURCE_WHATSAPP = "whatsapp"


def default_hidden_conditions() -> list[Any]:
    """PR B (item 20/D4) + D-recon (D4): items hidden by default —

    superseded (struck-through) and confirmed-cancelled
    (extraction_meta.cancelled: a truthy stamp — a dict or true — never
    hides; an explicit false or an absent key stays visible). ONE shared
    predicate for every SQL leg; Python legs use is_hidden_by_default()
    below so the two layers cannot drift.
    """
    cancelled_json = MemoryItem.extraction_meta.op("->")("cancelled")
    cancelled_text = func.jsonb_extract_path_text(MemoryItem.extraction_meta, "cancelled")
    return [
        MemoryItem.superseded_by.is_(None),
        or_(cancelled_json.is_(None), func.coalesce(cancelled_text, "") == "false"),
    ]


def is_hidden_by_default(item: MemoryItem) -> bool:
    """The Python twin of default_hidden_conditions() (search's vector
    post-filter): superseded or confirmed-cancelled -> hidden by default."""
    if item.superseded_by is not None:
        return True
    meta = item.extraction_meta if isinstance(item.extraction_meta, dict) else {}
    cancelled = meta.get("cancelled")
    return cancelled is not None and cancelled is not False


def _escape_like(needle: str) -> str:
    """Neutralise LIKE wildcards in user/entity-derived needles."""
    return re.sub(r"([\\%_])", r"\\\1", needle)


class ItemsRepository:
    """All data access for MemoryItem; one instance per request.

    Each mutating method flushes and refreshes but leaves the transaction
    open; callers commit via :meth:`commit` so multi-step use cases stay
    atomic.
    """

    def __init__(self, db: Session) -> None:
        self._db = db

    def add(self, item: MemoryItem) -> MemoryItem:
        """Persist a new item and return it with server-generated fields."""
        self._db.add(item)
        self._flush_and_refresh(item)
        return item

    def get(self, item_id: uuid.UUID) -> MemoryItem | None:
        """Return the item with the given id, or None if absent."""
        return self._db.get(MemoryItem, item_id)

    def get_by_fingerprint(self, fingerprint: str) -> MemoryItem | None:
        """Return the first item carrying this content fingerprint, or None.

        Oldest first: on (theoretically possible) multiple matches the
        original capture wins.
        """
        stmt = (
            select(MemoryItem)
            .where(MemoryItem.fingerprint == fingerprint)
            .order_by(MemoryItem.created_at.asc(), MemoryItem.id.asc())
            .limit(1)
        )
        return self._db.scalars(stmt).first()

    def get_many(self, item_ids: Sequence[uuid.UUID]) -> Sequence[MemoryItem]:
        """Return the items with the given ids, missing ones silently skipped."""
        if not item_ids:
            return []
        stmt = select(MemoryItem).where(MemoryItem.id.in_(item_ids))
        return list(self._db.scalars(stmt))

    def list(
        self,
        *,
        item_type: ItemType | None,
        project: str | None,
        status: ItemStatus | None,
        limit: int,
        offset: int,
        include_children: bool = False,
        include_noise: bool = False,
        include_superseded: bool = False,
    ) -> Sequence[MemoryItem]:
        """List items newest-first, optionally filtered by type/project/status.

        ``include_children=True`` opts IN to extracted children (used by the
        parent's child list); default listings never show them (item 19).
        ``include_noise=True`` opts IN to triaged-noise WhatsApp conversations
        (D2a, item 36); default listings hide them — noise stays stored and
        searchable, just not listed. Untriaged items are always listed.
        ``include_superseded=True`` opts IN to superseded (struck-through)
        items; default listings never show them (item 20/D4).
        """
        stmt = select(MemoryItem).order_by(MemoryItem.created_at.desc(), MemoryItem.id.desc())
        if item_type is not None:
            stmt = stmt.where(MemoryItem.type == item_type)
        if project is not None:
            stmt = stmt.where(MemoryItem.project == project)
        if status is not None:
            stmt = stmt.where(MemoryItem.status == status)
        # Meaning extraction (item 19): proposed children are SEARCHABLE but
        # UNLISTED in /recent and /review until kept; dropped children stay
        # hidden too. Kept children list normally.
        if not include_children:
            stmt = stmt.where(
                MemoryItem.child_state.is_(None) | (MemoryItem.child_state == "kept")
            )
        if not include_noise:
            stmt = stmt.where(
                or_(
                    MemoryItem.source != SOURCE_WHATSAPP,
                    MemoryItem.triage_label.is_(None),
                    MemoryItem.triage_label != "noise",
                )
            )
        # PR B (item 20/D4) + D-recon (D4): superseded and confirmed-cancelled
        # items are hidden by default (one shared predicate).
        if not include_superseded:
            for cond in default_hidden_conditions():
                stmt = stmt.where(cond)
        return list(self._db.scalars(stmt.limit(limit).offset(offset)))

    def untriaged_whatsapp(self, *, limit: int) -> Sequence[MemoryItem]:
        """WhatsApp conversations without a triage decision, oldest first.

        Drives the `brain whatsapp-triage` backfill (D2a): only UNTRIAGED
        items — a re-triage happens through the update path, never silently
        by the backfill.
        """
        stmt = (
            select(MemoryItem)
            .where(
                MemoryItem.source == SOURCE_WHATSAPP,
                MemoryItem.triage_label.is_(None),
            )
            .order_by(MemoryItem.created_at.asc(), MemoryItem.id.asc())
            .limit(limit)
        )
        return list(self._db.scalars(stmt))

    def apply_triage(self, item: MemoryItem, result: Any) -> None:
        """Write one triage decision onto the item (flushed, not committed)."""
        item.triage_label = result.label
        item.triage_confidence = result.confidence
        item.triage_method = result.method
        item.triage_version = result.version
        self._db.flush()

    def search(
        self, *, query: str, limit: int, offset: int, include_superseded: bool = False
    ) -> Sequence[MemoryItem]:
        """Full-text search over raw_text via Postgres FTS, best match first.

        Superseded (struck-through) items are hidden by default (item 20/D4);
        ``include_superseded=True`` opts back in (the bot's Undo/Restore views).
        Noise WhatsApp conversations (D2a) REMAIN searchable — hiding noise
        from listings must never remove it from search.
        """
        tsquery = func.websearch_to_tsquery(FTS_LANGUAGE, query)
        stmt = (
            select(MemoryItem)
            .where(MemoryItem.search_vector.op("@@")(tsquery))
            .order_by(
                func.ts_rank(MemoryItem.search_vector, tsquery).desc(),
                MemoryItem.created_at.desc(),
                MemoryItem.id.desc(),
            )
            .limit(limit)
            .offset(offset)
        )
        # PR B (item 20/D4) + D-recon (D4): superseded and confirmed-cancelled
        # items are hidden by default (one shared predicate).
        if not include_superseded:
            for cond in default_hidden_conditions():
                stmt = stmt.where(cond)
        return list(self._db.scalars(stmt))

    def by_occurrence(
        self, *, start: datetime, end: datetime, limit: int, include_superseded: bool = False
    ) -> Sequence[MemoryItem]:
        """Every item with occurred_at inside [start, end), soonest first.

        Drives temporal retrieval: day/date questions fetch ALL events in the
        resolved range (top-k semantic search cannot express "everything on
        Tuesday"). Items without occurred_at are naturally excluded.
        Superseded (struck-through) items are hidden by default (item 20/D4);
        ``include_superseded=True`` opts back in.
        """
        stmt = (
            select(MemoryItem)
            .where(MemoryItem.occurred_at >= start)
            .where(MemoryItem.occurred_at < end)
            .order_by(MemoryItem.occurred_at.asc(), MemoryItem.id.desc())
            .limit(limit)
        )
        # PR B (item 20/D4) + D-recon (D4): superseded and confirmed-cancelled
        # items are hidden by default (one shared predicate).
        if not include_superseded:
            for cond in default_hidden_conditions():
                stmt = stmt.where(cond)
        return list(self._db.scalars(stmt))

    def by_text_any(
        self, needles: Sequence[str], *, limit: int, include_superseded: bool = False
    ) -> Sequence[MemoryItem]:
        """Items whose raw_text contains ANY needle (case-insensitive), newest first.

        Drives the kid-recall leg of /ask: a captured WhatsApp message keeps
        its chat name in the raw_text header ('[date, time] <chat> / <sender>:
        body'), so the exact channel name as stored in memory matches the
        items captured from that channel. Needles are user/entity data, so
        LIKE wildcards in them are escaped, never interpreted.

        Extracted children with ``child_state = 'dropped'`` (user-rejected by
        the meaning-extraction lifecycle, item 19) are never recalled;
        ordinary captures, 'proposed' and 'kept' children are.
        Superseded (struck-through) items are hidden by default (item 20/D4);
        ``include_superseded=True`` opts back in.
        """
        if not needles:
            return []
        clauses = [
            MemoryItem.raw_text.ilike(f"%{_escape_like(needle)}%", escape="\\")
            for needle in needles
        ]
        stmt = (
            select(MemoryItem)
            .where(
                or_(*clauses),
                # 'dropped' children are excluded (NULL-safe: NULL != 'dropped')
                or_(MemoryItem.child_state.is_(None), MemoryItem.child_state != "dropped"),
            )
            .order_by(MemoryItem.created_at.desc(), MemoryItem.id.desc())
            .limit(limit)
        )
        # PR B (item 20/D4) + D-recon (D4): superseded and confirmed-cancelled
        # items are hidden by default (one shared predicate).
        if not include_superseded:
            for cond in default_hidden_conditions():
                stmt = stmt.where(cond)
        return list(self._db.scalars(stmt))

    def conflict_counterparts(
        self, item_ids: Sequence[uuid.UUID], *, limit: int = 4
    ) -> Sequence[tuple[ItemRelation, MemoryItem]]:
        """``(relation, other_item)`` for every conflicts relation touching
        the given items (PR-b, item 38's ask leg).

        The counterpart is returned regardless of its state: a PROPOSED
        child that contradicts a captured item is exactly what the user
        needs to see next to it (the answer must quote both versions).
        Only live relations are read — rows are never deleted, but undone
        applies_to/supersedes states do not exist for ``conflicts``.
        Bounded: newest relations first, ``limit`` pairs.
        """
        if not item_ids:
            return []
        ids = list(item_ids)
        stmt = (
            select(ItemRelation, MemoryItem)
            .join(
                MemoryItem,
                or_(
                    and_(
                        ItemRelation.to_id.in_(ids),
                        MemoryItem.id == ItemRelation.from_id,
                    ),
                    and_(
                        ItemRelation.from_id.in_(ids),
                        MemoryItem.id == ItemRelation.to_id,
                    ),
                ),
            )
            .where(ItemRelation.kind == "conflicts")
            .order_by(ItemRelation.created_at.desc(), ItemRelation.id.desc())
            .limit(limit)
        )
        return [(rel, item) for rel, item in self._db.execute(stmt).all()]

    def by_channels(
        self,
        channels: Sequence[str],
        *,
        limit: int,
        ranges: Sequence[tuple[datetime, datetime]] | None = None,
    ) -> Sequence[MemoryItem]:
        """WhatsApp conversation items whose meta.channel is one of ``channels``.

        Drives the kid-recall leg's STRUCTURED channel recall (WA39 run 1:
        the raw-text needle missed items whose header did not carry the
        exact seeded name, and the newest-first cap flooded the pool with
        newer conversations). ``ranges`` (the question's resolved time
        windows, the temporal leg's own parse) bound occurred_at — pass None
        for the recent-N fallback. Noise-triaged conversations are excluded
        exactly as from listings; dropped children and superseded items too.
        """
        if not channels:
            return []
        stmt = select(MemoryItem).where(
            MemoryItem.source == SOURCE_WHATSAPP,
            MemoryItem.meta["channel"].as_string().in_(list(channels)),
            or_(
                MemoryItem.child_state.is_(None),
                MemoryItem.child_state != "dropped",
            ),
            MemoryItem.superseded_by.is_(None),
            or_(
                MemoryItem.triage_label.is_(None),
                MemoryItem.triage_label != "noise",
            ),
        )
        if ranges:
            stmt = stmt.where(
                or_(
                    *[
                        and_(MemoryItem.occurred_at >= start, MemoryItem.occurred_at < end)
                        for start, end in ranges
                    ]
                )
            )
        stmt = stmt.order_by(
            MemoryItem.occurred_at.desc().nullslast(),
            MemoryItem.created_at.desc(),
            MemoryItem.id.desc(),
        ).limit(limit)
        return list(self._db.scalars(stmt))

    def children_of(self, item_id: uuid.UUID) -> Sequence[MemoryItem]:
        """Extraction children quoting this item as a source (item_sources).

        Used by the connector runner: when a source deletes a parent, its
        proposed children must hide with it (review bug 4). Oldest first for
        deterministic order.
        """
        stmt = (
            select(MemoryItem)
            .join(ItemSource, ItemSource.child_id == MemoryItem.id)
            .where(ItemSource.source_id == item_id)
            .order_by(MemoryItem.created_at.asc(), MemoryItem.id.asc())
        )
        return list(self._db.scalars(stmt))

    def delete(self, item: MemoryItem) -> None:
        """Remove the given item."""
        self._db.delete(item)
        self._db.flush()

    def commit(self) -> None:
        """Commit the current transaction."""
        self._db.commit()

    def rollback(self) -> None:
        """Discard the current transaction (used by batch pollers on failure)."""
        self._db.rollback()

    def refresh(self, item: MemoryItem) -> None:
        """Reload server-generated fields (e.g. updated_at) from the DB."""
        self._db.refresh(item)

    def _flush_and_refresh(self, item: MemoryItem) -> None:
        self._db.flush()
        self._db.refresh(item)
