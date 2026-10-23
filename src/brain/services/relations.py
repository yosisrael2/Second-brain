"""Relations between memory items (PR B; design items 7, 20, 21, 22 + D4).

- ``item_relations`` rows: kind supersedes|corrects|derived_from|same_as,
  UNIQUE per (from_id, to_id, kind), provenance JSONB (how + lifecycle state).
- Supersede safety (item 20): SAME-THREAD corrections are auto-applied
  (old item struck through via ``memory_items.superseded_by``) but always
  visible in the audit + undoable; CROSS-THREAD LLM matches are only
  PROPOSED (old item stays visible until confirmed).
- Dedupe key (item 21): same type + date ±1h + SAME applies_to + summary
  similarity >= 0.85. Never merge items whose applies_to differ (R5).
- D4: superseded children stay in the DB (strike-through), hidden by
  default from /recent, /review and search; restorable; never deleted.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Sequence
from datetime import timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from brain.db.models import ItemRelation, MemoryItem
from brain.db.session import SessionLocal

KNOWN_KINDS = ("supersedes", "corrects", "derived_from", "same_as")
SUPERSEDE_STATES = ("applied", "proposed", "undone")
# Item 21: cos >= 0.85 on the summary text; ±1h on the date.
SIMILARITY_THRESHOLD = 0.85
DATE_TOLERANCE = timedelta(hours=1)


class RelationsError(Exception):
    """A relation request violates the model's rules (bad kind, missing item)."""


def similarity_ratio(a: str, b: str) -> float:
    """Deterministic text similarity in [0, 1] (no deps, stable for tests).

    Production dedupe wires an embedder-based cosine via the injectable
    ``similarity`` callable; this ratio is the offline fallback (item 21's
    threshold applies to whatever callable is wired in).
    """
    import difflib  # noqa: PLC0415

    if not a or not b:
        return 0.0
    return difflib.SequenceMatcher(None, a.casefold(), b.casefold()).ratio()


class RelationsService:
    """Add/remove relations, resolve current-version chains, supersede tiers."""

    def __init__(self, session_factory: Callable[[], Session] = SessionLocal) -> None:
        self._sessions = session_factory

    # --- plain links -------------------------------------------------------

    def add(
        self,
        from_id: uuid.UUID,
        to_id: uuid.UUID,
        *,
        kind: str,
        provenance: dict[str, Any] | None = None,
        state: str | None = None,
    ) -> ItemRelation:
        """Create (or return the existing) relation; UNIQUE per triple."""
        if kind not in KNOWN_KINDS:
            raise RelationsError(f"unknown relation kind: {kind!r}")
        if from_id == to_id:
            raise RelationsError("a relation needs two different items")
        self._require_items(from_id, to_id)
        with self._sessions() as session:
            existing = session.execute(
                select(ItemRelation).where(
                    ItemRelation.from_id == from_id,
                    ItemRelation.to_id == to_id,
                    ItemRelation.kind == kind,
                )
            ).scalar_one_or_none()
            if existing is not None:
                session.expunge(existing)
                return existing
            rel = ItemRelation(
                from_id=from_id,
                to_id=to_id,
                kind=kind,
                provenance={**(provenance or {}), **({"state": state} if state else {})},
            )
            session.add(rel)
            session.commit()
            session.expunge(rel)
            return rel

    def get(self, relation_id: uuid.UUID) -> ItemRelation | None:
        with self._sessions() as session:
            rel = session.get(ItemRelation, relation_id)
            if rel is not None:
                session.expunge(rel)
            return rel

    def discard_proposal(self, relation_id: uuid.UUID) -> None:
        """✖ Keep-both: delete a PROPOSED supersede, and ONLY that.

        Review round 1 state guard: a stale card's keep-both must never
        delete an APPLIED relation (that would leave the old item struck
        with no relation pointing at it — unfindable by Restore/Undo).
        One transaction; anything not 'proposed' is refused.
        """
        with self._sessions() as session:
            rel = session.get(ItemRelation, relation_id)
            if rel is None:
                raise RelationsError(f"no such relation: {relation_id}")
            if rel.kind != "supersedes":
                raise RelationsError(f"not a supersedes relation: {relation_id}")
            if (rel.provenance or {}).get("state") != "proposed":
                raise RelationsError("already applied — use Undo")
            session.delete(rel)
            session.commit()

    def remove(self, relation_id: uuid.UUID) -> None:
        """Delete a plain link (derived_from/same_as corrections).

        Supersede links go through :meth:`undo_supersede` instead — their rows
        are never silently deleted (audit, item 20a).
        """
        with self._sessions() as session:
            rel = session.get(ItemRelation, relation_id)
            if rel is None:
                raise RelationsError(f"no such relation: {relation_id}")
            session.delete(rel)
            session.commit()

    def relations_of(self, item_id: uuid.UUID) -> list[ItemRelation]:
        """Every relation touching the item (outgoing and incoming)."""
        with self._sessions() as session:
            rows = (
                session.execute(
                    select(ItemRelation)
                    .where((ItemRelation.from_id == item_id) | (ItemRelation.to_id == item_id))
                    .order_by(ItemRelation.created_at.asc(), ItemRelation.id)
                )
                .scalars()
                .all()
            )
            for row in rows:
                session.expunge(row)
            return list(rows)

    def facts_of(self, derived_id: uuid.UUID) -> list[uuid.UUID]:
        """Item 22: the facts a derived task cites (its derived_from targets)."""
        with self._sessions() as session:
            rows = (
                session.execute(
                    select(ItemRelation.to_id).where(
                        ItemRelation.from_id == derived_id,
                        ItemRelation.kind == "derived_from",
                    )
                )
                .scalars()
                .all()
            )
            return list(rows)

    # --- supersede tiers (item 20) -----------------------------------------

    def apply_supersede(
        self,
        *,
        new_id: uuid.UUID,
        old_id: uuid.UUID,
        provenance: dict[str, Any] | None = None,
    ) -> ItemRelation:
        """Same-thread correction: applied immediately, visible + undoable.

        The relation row and the strike (``superseded_by``) share ONE
        transaction: if the strike fails, no half-applied correction is
        left behind (review round 1, fix 2).
        """
        with self._sessions() as session:
            found = {
                row
                for (row,) in session.execute(
                    select(MemoryItem.id).where(MemoryItem.id.in_([new_id, old_id]))
                )
            }
            missing = [str(i) for i in (new_id, old_id) if i not in found]
            if missing:
                raise RelationsError(f"no such item(s): {', '.join(missing)}")
            rel = ItemRelation(
                from_id=new_id,
                to_id=old_id,
                kind="supersedes",
                provenance={"state": "applied", **(provenance or {})},
            )
            session.add(rel)
            old = session.get(MemoryItem, old_id)
            old.superseded_by = new_id
            session.commit()
            session.refresh(rel)
            return rel

    def propose_supersede(
        self,
        *,
        new_id: uuid.UUID,
        old_id: uuid.UUID,
        provenance: dict[str, Any] | None = None,
    ) -> ItemRelation:
        """Cross-thread LLM match: PROPOSED only; the old item stays visible."""
        return self.add(new_id, old_id, kind="supersedes", provenance=provenance, state="proposed")

    def confirm_supersede(self, relation_id: uuid.UUID) -> ItemRelation:
        """The user tapped ✅ confirm-replace: proposed -> applied."""
        return self._transition(relation_id, to_state="applied", strike=True)

    def undo_supersede(self, relation_id: uuid.UUID) -> ItemRelation:
        """Undo/restore: the old item becomes current again (D4)."""
        return self._transition(relation_id, to_state="undone", strike=False)

    def _transition(self, relation_id: uuid.UUID, *, to_state: str, strike: bool) -> ItemRelation:
        with self._sessions() as session:
            rel = session.get(ItemRelation, relation_id)
            if rel is None or rel.kind != "supersedes":
                raise RelationsError(f"not a supersedes relation: {relation_id}")
            # Review round 1 state guards: confirm only proposed -> applied;
            # undo only applied -> undone. Repeats are honest no-ops.
            state = (rel.provenance or {}).get("state")
            if to_state == "applied":
                if state == "applied":
                    session.expunge(rel)
                    return rel  # double confirm: no-op
                if state == "undone":
                    raise RelationsError("relation was undone — apply the supersede again instead")
            else:  # undo
                if state == "undone":
                    session.expunge(rel)
                    return rel  # double undo: no-op, already restored
                if state != "applied":
                    raise RelationsError("not applied yet — discard the proposal instead")
            rel.provenance = {**rel.provenance, "state": to_state}
            if strike:
                self._strike(rel.to_id, by=rel.from_id, session=session)
            else:
                current = session.get(MemoryItem, rel.to_id)
                if current is not None and current.superseded_by == rel.from_id:
                    current.superseded_by = None
            session.commit()
            session.expunge(rel)
            return rel

    def _strike(self, old_id: uuid.UUID, *, by: uuid.UUID, session: Session | None = None) -> None:
        if session is not None:
            old = session.get(MemoryItem, old_id)
            if old is None:
                raise RelationsError(f"no such item: {old_id}")
            old.superseded_by = by
            return
        with self._sessions() as session:
            old = session.get(MemoryItem, old_id)
            if old is None:
                raise RelationsError(f"no such item: {old_id}")
            old.superseded_by = by
            session.commit()

    # --- current-version chain ---------------------------------------------

    def current_version(self, item_id: uuid.UUID) -> uuid.UUID:
        """Follow the supersedes chain forward to the CURRENT version."""
        seen: set[uuid.UUID] = set()
        current = item_id
        while True:
            if current in seen:
                raise RelationsError(f"supersede cycle at {current}")
            seen.add(current)
            with self._sessions() as session:
                item = session.get(MemoryItem, current)
                if item is None or item.superseded_by is None:
                    return current
                current = item.superseded_by

    # --- dedupe key (item 21) ----------------------------------------------

    def find_duplicate(
        self,
        candidate: dict[str, Any],
        existing_ids: Sequence[uuid.UUID],
        *,
        similarity: Callable[[str, str], float],
    ) -> uuid.UUID | None:
        """Return the existing item the candidate duplicates, else None.

        Item 21's key: same type + date ±1h + SAME applies_to + summary
        similarity >= 0.85. NEVER merges differing applies_to (R5: grade 2 vs
        grade 5 same day stay separate). Dateless children (facts) match on
        type + applies_to + text only; a dated and a dateless never merge.
        """
        with self._sessions() as session:
            rows = (
                session.execute(select(MemoryItem).where(MemoryItem.id.in_(list(existing_ids))))
                .scalars()
                .all()
            )
            by_id = {row.id: row for row in rows}
        candidate_applies = self._normalise_applies(candidate.get("applies_to"))
        candidate_date = candidate.get("occurred_at")
        for item_id in existing_ids:
            item = by_id.get(item_id)
            if item is None:
                continue
            if candidate.get("type") != item.type:
                continue
            if self._normalise_applies(item.applies_to) != candidate_applies:
                continue  # R5: different applies_to NEVER merge
            item_date = item.occurred_at
            if (candidate_date is None) != (item_date is None):
                continue  # a dated and a dateless child never merge
            if (
                candidate_date is not None
                and item_date is not None
                and abs(candidate_date - item_date) > DATE_TOLERANCE
            ):
                continue
            if similarity(candidate.get("text", ""), item.raw_text) < SIMILARITY_THRESHOLD:
                continue
            return item_id
        return None

    @staticmethod
    def _normalise_applies(applies_to: Any) -> list[str]:
        if not applies_to:
            return []
        return sorted(str(a) for a in applies_to)

    # --- internals -----------------------------------------------------------

    def _require_items(self, *item_ids: uuid.UUID) -> None:
        with self._sessions() as session:
            found = {
                row
                for (row,) in session.execute(
                    select(MemoryItem.id).where(MemoryItem.id.in_(list(item_ids)))
                )
            }
        missing = [str(i) for i in item_ids if i not in found]
        if missing:
            raise RelationsError(f"no such item(s): {', '.join(missing)}")
