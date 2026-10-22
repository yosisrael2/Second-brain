"""Pydantic schemas: API request/response contracts and validation."""

import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from brain.db.enums import (
    CONFIDENCE_MAX,
    CONFIDENCE_MIN,
    IMPORTANCE_MAX,
    IMPORTANCE_MIN,
    Actionability,
    ItemStatus,
    ItemType,
)

RAW_TEXT_MAX_LENGTH = 10_000
SOURCE_MAX_LENGTH = 64
PROJECT_MAX_LENGTH = 128
DEFAULT_PAGE_SIZE = 50
MAX_PAGE_SIZE = 200


class ItemCreate(BaseModel):
    """Payload for POST /items."""

    raw_text: str = Field(min_length=1, max_length=RAW_TEXT_MAX_LENGTH)
    source: str = Field(default="manual", min_length=1, max_length=SOURCE_MAX_LENGTH)
    type: ItemType | None = None
    project: str | None = Field(default=None, min_length=1, max_length=PROJECT_MAX_LENGTH)
    importance: int | None = Field(default=None, ge=IMPORTANCE_MIN, le=IMPORTANCE_MAX)
    actionability: Actionability | None = None
    confidence: float | None = Field(default=None, ge=CONFIDENCE_MIN, le=CONFIDENCE_MAX)
    status: ItemStatus = ItemStatus.INBOX
    # When the captured thing happened (None = unknown); format-specific
    # extraction result (email headers, WhatsApp messages, event fields).
    occurred_at: datetime | None = None
    meta: dict[str, Any] = Field(default_factory=dict)


class ItemUpdate(BaseModel):
    """Partial payload for PATCH /items/{id}; absent fields stay unchanged."""

    raw_text: str | None = Field(default=None, min_length=1, max_length=RAW_TEXT_MAX_LENGTH)
    source: str | None = Field(default=None, min_length=1, max_length=SOURCE_MAX_LENGTH)
    type: ItemType | None = None
    project: str | None = Field(default=None, min_length=1, max_length=PROJECT_MAX_LENGTH)
    importance: int | None = Field(default=None, ge=IMPORTANCE_MIN, le=IMPORTANCE_MAX)
    actionability: Actionability | None = None
    confidence: float | None = Field(default=None, ge=CONFIDENCE_MIN, le=CONFIDENCE_MAX)
    status: ItemStatus | None = None


class ItemRead(BaseModel):
    """A memory item as returned by the API."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    raw_text: str
    source: str
    type: ItemType | None
    project: str | None
    importance: int | None
    actionability: Actionability | None
    confidence: float | None
    status: ItemStatus
    occurred_at: datetime | None = None
    meta: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime
    updated_at: datetime


class SearchResultRead(ItemRead):
    """A memory item plus its semantic-search score (None for keyword fallback)."""

    score: float | None = None


class AskQuestion(BaseModel):
    """Payload for POST /ask."""

    question: str = Field(min_length=1, max_length=RAW_TEXT_MAX_LENGTH)


class AskAnswer(BaseModel):
    """Response for POST /ask: answer, cited item ids, the sources used, and
    the model that answered (None when the backend does not expose one)."""

    answer: str
    cited_ids: list[str]
    sources: list[SearchResultRead]
    model: str | None = None


KindHint = Literal["auto", "email", "whatsapp", "calendar", "note"]


class CaptureRequest(BaseModel):
    """Payload for POST /capture: a pasted text to parse and capture."""

    text: str = Field(min_length=1, max_length=RAW_TEXT_MAX_LENGTH)
    # Values mirror brain.services.paste.KINDS plus "auto" (detect).
    kind_hint: KindHint = "auto"
    split: bool = Field(
        default=False,
        description="Split multi-message pastes (WhatsApp threads) into one item per message",
    )


class CaptureResponse(BaseModel):
    """Response for POST /capture: detected kind, extraction summary, items."""

    kind: str
    extracted: dict[str, Any]
    items: list[ItemRead]
