"""Repository for the memory layer (entities, aliases, versioned facts)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from brain.db.models import Entity, EntityAlias, MemoryFact


class MemoryRepository:
    """All data access for entities/aliases/facts; one instance per request.

    Same transaction discipline as :class:`~brain.repositories.items.ItemsRepository`:
    mutators flush, callers commit via :meth:`commit`.
    """

    def __init__(self, db: Session) -> None:
        self._db = db

    # -- entities -------------------------------------------------------------

    def add_entity(self, entity: Entity) -> Entity:
        self._db.add(entity)
        self._flush(entity)
        return entity

    def get_entity(self, entity_id: uuid.UUID) -> Entity | None:
        """Return the entity with the given id, or None if absent."""
        return self._db.get(Entity, entity_id)

    def find_entity(self, *, kind: str, name: str) -> Entity | None:
        """Exact (kind, name) lookup — the seed path's dedupe key."""
        stmt = (
            select(Entity)
            .where(Entity.kind == kind, Entity.name == name)
            .order_by(Entity.created_at.asc(), Entity.id.asc())
            .limit(1)
        )
        return self._db.scalars(stmt).first()

    def list_entities(self, kinds: list[str] | None = None) -> list[Entity]:
        stmt = select(Entity).order_by(Entity.created_at.asc(), Entity.id.asc())
        if kinds:
            stmt = stmt.where(Entity.kind.in_(kinds))
        return list(self._db.scalars(stmt))

    def delete_entity(self, entity: Entity) -> None:
        self._db.delete(entity)
        self._db.flush()  # no refresh: the instance is gone after delete

    # -- aliases ----------------------------------------------------------------

    def add_alias(self, alias: EntityAlias) -> EntityAlias:
        self._db.add(alias)
        self._flush(alias)
        return alias

    def aliases_for(self, entity_id: uuid.UUID) -> list[str]:
        stmt = (
            select(EntityAlias.alias)
            .where(EntityAlias.entity_id == entity_id)
            .order_by(EntityAlias.alias.asc())
        )
        return list(self._db.scalars(stmt))

    # -- facts --------------------------------------------------------------------

    def add_fact(self, fact: MemoryFact) -> MemoryFact:
        self._db.add(fact)
        self._flush(fact)
        return fact

    def get_fact(self, fact_id: uuid.UUID) -> MemoryFact | None:
        return self._db.get(MemoryFact, fact_id)

    def facts_about(self, subject_id: uuid.UUID) -> list[MemoryFact]:
        """Every fact row for a subject, oldest first (full history)."""
        stmt = (
            select(MemoryFact)
            .where(MemoryFact.subject_id == subject_id)
            .order_by(MemoryFact.valid_from.asc(), MemoryFact.created_at.asc())
        )
        return list(self._db.scalars(stmt))

    def current_facts(
        self,
        *,
        subject_ids: list[uuid.UUID] | None = None,
        status: str | None = None,
    ) -> list[MemoryFact]:
        """Facts valid right now: valid_to IS NULL, optional status filter."""
        stmt = select(MemoryFact).where(MemoryFact.valid_to.is_(None))
        if subject_ids is not None:
            stmt = stmt.where(MemoryFact.subject_id.in_(subject_ids))
        if status is not None:
            stmt = stmt.where(MemoryFact.status == status)
        stmt = stmt.order_by(MemoryFact.created_at.asc(), MemoryFact.id.asc())
        return list(self._db.scalars(stmt))

    def search_facts(self, needle: str) -> list[MemoryFact]:
        """Case-insensitive substring over value/predicate (lightweight; FTS later)."""
        pattern = f"%{needle}%"
        stmt = (
            select(MemoryFact)
            .where(MemoryFact.value.ilike(pattern) | MemoryFact.predicate.ilike(pattern))
            .order_by(MemoryFact.created_at.desc())
        )
        return list(self._db.scalars(stmt))

    def search_entities(self, needle: str) -> list[Entity]:
        """Entities whose name or one of whose aliases matches the needle."""
        pattern = f"%{needle}%"
        alias_match = select(EntityAlias.entity_id).where(
            EntityAlias.alias.ilike(pattern)
        )
        stmt = (
            select(Entity)
            .where(Entity.name.ilike(pattern) | Entity.id.in_(alias_match))
            .order_by(Entity.created_at.asc())
        )
        return list(self._db.scalars(stmt))

    def close_fact(self, fact: MemoryFact, *, at: datetime) -> None:
        """Close a fact's validity window (never delete content)."""
        fact.valid_to = at
        self._flush(fact)

    def commit(self) -> None:
        self._db.commit()

    def _flush(self, instance: object) -> None:
        self._db.flush()
        self._db.refresh(instance)


def utcnow() -> datetime:
    """Aware-UTC now (facts are timestamped in UTC, rendered in the user's TZ)."""
    return datetime.now(UTC)
