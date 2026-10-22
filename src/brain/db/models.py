"""ORM models. Importing this module registers every model on Base.metadata."""

import uuid
from collections.abc import Iterable
from datetime import datetime
from typing import Any

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Computed,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy import Enum as SAEnum
from sqlalchemy.dialects.postgresql import JSONB, TSVECTOR, UUID
from sqlalchemy.orm import Mapped, mapped_column

from brain.db.base import Base
from brain.db.enums import (
    CONFIDENCE_MAX,
    CONFIDENCE_MIN,
    FTS_LANGUAGE,
    IMPORTANCE_MAX,
    IMPORTANCE_MIN,
    Actionability,
    ItemStatus,
    ItemType,
)

_TEXT_MAX_LENGTH = 10_000
_SOURCE_MAX_LENGTH = 64
_PROJECT_MAX_LENGTH = 128
_ENUM_MAX_LENGTH = 32


def _in_domain(column: str, values: Iterable[str]) -> str:
    """Build a CHECK expression restricting a column to the given values."""
    quoted = ", ".join(f"'{value}'" for value in values)
    return f"{column} IN ({quoted})"


def _enum_column(enum_cls: type) -> "SAEnum":
    """Build a VARCHAR-backed enum column type persisting enum values."""
    return SAEnum(
        enum_cls,
        native_enum=False,
        values_callable=lambda e: [member.value for member in e],
        length=_ENUM_MAX_LENGTH,
    )


class MemoryItem(Base):
    """A captured piece of knowledge, awaiting classification and filing."""

    __tablename__ = "memory_items"
    __table_args__ = (
        CheckConstraint(
            _in_domain("type", (member.value for member in ItemType)),
            name="type",
        ),
        CheckConstraint(
            _in_domain("actionability", (member.value for member in Actionability)),
            name="actionability",
        ),
        CheckConstraint(
            _in_domain("status", (member.value for member in ItemStatus)),
            name="status",
        ),
        CheckConstraint(
            "child_state IS NULL OR child_state IN ('proposed', 'kept', 'dropped')",
            name="child_state",
        ),
        CheckConstraint(
            f"importance IS NULL OR importance BETWEEN {IMPORTANCE_MIN} AND {IMPORTANCE_MAX}",
            name="importance_range",
        ),
        CheckConstraint(
            f"confidence IS NULL OR confidence BETWEEN {CONFIDENCE_MIN} AND {CONFIDENCE_MAX}",
            name="confidence_range",
        ),
        Index(
            "ix_memory_items_search_vector",
            "search_vector",
            postgresql_using="gin",
        ),
        Index("ix_memory_items_fingerprint", "fingerprint"),
    )

    # Transient (not persisted): set on the RETURNED existing item when ingest
    # detects an exact duplicate, so /capture and the bot can say "Already
    # saved". Plain class attribute — SQLAlchemy does not map it.
    deduplicated = False


    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    raw_text: Mapped[str] = mapped_column(Text, nullable=False)
    source: Mapped[str] = mapped_column(
        String(_SOURCE_MAX_LENGTH), nullable=False, default="manual"
    )
    type: Mapped[ItemType | None] = mapped_column(_enum_column(ItemType), nullable=True)
    project: Mapped[str | None] = mapped_column(String(_PROJECT_MAX_LENGTH), nullable=True)
    importance: Mapped[int | None] = mapped_column(Integer, nullable=True)
    actionability: Mapped[Actionability | None] = mapped_column(
        _enum_column(Actionability), nullable=True
    )
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    status: Mapped[ItemStatus] = mapped_column(
        _enum_column(ItemStatus), nullable=False, default=ItemStatus.INBOX
    )
    # When the captured thing happened (email date, WhatsApp timestamp, event
    # start), as opposed to created_at (when we captured it). Zone-less pasted
    # times are interpreted in the TIMEZONE setting and stored as aware UTC;
    # explicit offsets (Outlook +0300, ICS TZID/Z) are honored as given. The
    # original raw strings stay in meta for audit.
    occurred_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # Exact-duplicate guard: sha256 over (normalized text, occurred_at UTC
    # minute, source kind); computed by brain.services.dedupe at ingest and
    # backfilled by the fingerprint migration. Nullable: rows created before
    # the column existed until the backfill runs (the migration backfills).
    fingerprint: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # --- meaning extraction (PR A1; design delta items 6, 7, 19, 2b) -----
    # child_state: NULL for ordinary top-level captures; otherwise the D2
    # lifecycle of an EXTRACTED CHILD: 'proposed' (searchable, unlisted in
    # /recent & /review until kept), 'kept' (user-approved), 'dropped'
    # (user-rejected; re-runs never recreate it — item 30).
    child_state: Mapped[str | None] = mapped_column(String(16), nullable=True)
    # applies_to: resolved audience (item 17, rule R1-R5) as a JSON list of
    # kid names / 'me' / 'nobody' / 'unknown'; NULL when never resolved.
    applies_to: Mapped[list[str] | None] = mapped_column(JSONB, nullable=True)
    # date_conflict: a weekday named in the text disagrees with the resolved
    # date (item 2b) — flagged, never silently fixed; conflicting children
    # land in needs_review with both readings in extraction_meta.
    date_conflict: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    # extraction_meta: provenance for extracted children (item 7): model,
    # prompt_version, run_id, extracted_at, date-resolution notes.
    extraction_meta: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    # Format-specific extraction result (email headers, WhatsApp messages,
    # event fields). Always a JSON object; empty for plain notes.
    meta: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    search_vector: Mapped[TSVECTOR] = mapped_column(
        TSVECTOR,
        Computed(f"to_tsvector('{FTS_LANGUAGE}', coalesce(raw_text, ''))", persisted=True),
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class OpsEvent(Base):
    """One recorded ops occurrence (request, classify call, hop, error...).

    Framework-free ops layer table: rows carry ids/counts/error types only —
    never secrets and never item bodies (see the cockpit spec).
    """

    __tablename__ = "ops_events"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    ts: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    component: Mapped[str] = mapped_column(String(32), nullable=False)
    kind: Mapped[str] = mapped_column(String(64), nullable=False)
    ok: Mapped[bool] = mapped_column(Boolean, nullable=False)
    latency_ms: Mapped[float | None] = mapped_column(Float, nullable=True)
    edge: Mapped[str | None] = mapped_column(String(64), nullable=True)
    detail: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )

    __table_args__ = (Index("ix_ops_events_component_ts", "component", "ts"),)


class OpsHeartbeat(Base):
    """Latest liveness signal per long-running component (bot, service...)."""

    __tablename__ = "ops_heartbeats"

    component: Mapped[str] = mapped_column(String(32), primary_key=True)
    last_seen: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    pid: Mapped[int | None] = mapped_column(Integer, nullable=True)
    version: Mapped[str | None] = mapped_column(String(64), nullable=True)
    detail: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )


class Entity(Base):
    """A durable thing in the user's world: person, group, place, or org.

    Memory layer (design: meaning-extraction §"MEMORY LAYER"): separate from
    the capture stream — notes are WHAT HAPPENED, entities+facts are WHAT IS
    TRUE. ``kind`` is a closed set so applies_to resolution can trust it.
    """

    __tablename__ = "entities"
    __table_args__ = (
        CheckConstraint(
            _in_domain("kind", ("person", "group", "place", "org", "channel")),
            name="kind",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class EntityAlias(Base):
    """An alternate name for an entity (Hebrew/English/nickname).

    PK (entity_id, alias): the same alias may belong to several entities
    (disambiguation happens in the service, not the schema).
    """

    __tablename__ = "entity_aliases"

    entity_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("entities.id", ondelete="CASCADE"),
        primary_key=True,
    )
    alias: Mapped[str] = mapped_column(Text, primary_key=True)
    lang: Mapped[str | None] = mapped_column(String(8), nullable=True)


class MemoryFact(Base):
    """A time-versioned fact about an entity.

    Never updated in place: changing a fact closes the old row (valid_to=now)
    and opens a new one linked via ``supersedes_id``; history stays queryable.
    ``status`` gates usage — ONLY confirmed, current facts drive applies_to
    and the ask prompt block. Provenance records where the fact came from
    (user command or source items + quote), so extraction (PR A) can insert
    proposals without a schema change.
    """

    __tablename__ = "memory_facts"
    __table_args__ = (
        CheckConstraint(
            _in_domain("status", ("proposed", "confirmed", "rejected")),
            name="status",
        ),
        CheckConstraint(
            "object_id IS NOT NULL OR value IS NOT NULL",
            name="has_object_or_value",
        ),
        Index("ix_memory_facts_subject", "subject_id"),
        Index("ix_memory_facts_status", "status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    subject_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("entities.id", ondelete="CASCADE"), nullable=False
    )
    predicate: Mapped[str] = mapped_column(String(64), nullable=False)
    object_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("entities.id", ondelete="SET NULL"), nullable=True
    )
    value: Mapped[str | None] = mapped_column(Text, nullable=True)
    valid_from: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    valid_to: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="proposed")
    supersedes_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("memory_facts.id", ondelete="SET NULL"), nullable=True
    )
    provenance: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class ItemSource(Base):
    """A quote linking an extracted child item to its source item (item 7).

    Many-to-many from day one ("white shirt 22.10" appears in BOTH teachers'
    emails -> one child, two sources). The quote is verbatim source text and
    is the grounding guard's evidence: a child without a grounded quote must
    not exist. Both FKs cascade: deleting either endpoint deletes the link,
    never the other item.
    """

    __tablename__ = "item_sources"
    __table_args__ = (
        # UNIQUE(child_id, source_id): one link per child-source pair; the
        # quote (possibly very long) stays OUT of the btree index — a long
        # TEXT in the UNIQUE index fails with 'index row size exceeds
        # maximum'. Extra quotes live in extraction_meta.extra_quotes.
        UniqueConstraint("child_id", "source_id"),
        Index("ix_item_sources_source_id", "source_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    child_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("memory_items.id", ondelete="CASCADE"), nullable=False
    )
    source_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("memory_items.id", ondelete="CASCADE"), nullable=False
    )
    quote: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class EnrichmentJob(Base):
    """One parent item's pending/finished enrichment pass (D5/D6, item 3).

    Durable queue: survives restarts, drains oldest-first, carries the daily
    cap accounting (``llm_used`` + ``finished_at``) and deferral (``not_before``
    set to tomorrow when the cap is reached). ``budget`` separates the live
    daily budget from the one-off backfill budget (D6). One job per parent
    (unique item_id); re-runs re-open the existing job instead of duplicating.
    """

    __tablename__ = "enrichment_jobs"
    __table_args__ = (
        CheckConstraint(
            "state IN ('queued', 'running', 'done', 'failed')", name="state"
        ),
        CheckConstraint("budget IN ('daily', 'backfill')", name="budget"),
        UniqueConstraint("item_id"),
        Index("ix_enrichment_jobs_state_not_before", "state", "not_before"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    item_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("memory_items.id", ondelete="CASCADE"), nullable=False
    )
    state: Mapped[str] = mapped_column(String(16), nullable=False, default="queued")
    budget: Mapped[str] = mapped_column(
        String(16), nullable=False, default="daily", server_default="daily"
    )
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    llm_used: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    not_before: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    queued_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
