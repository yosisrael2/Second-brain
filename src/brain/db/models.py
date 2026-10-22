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
    Index,
    Integer,
    String,
    Text,
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
    )

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
