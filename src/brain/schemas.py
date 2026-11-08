"""Pydantic schemas: API request/response contracts and validation."""

import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from brain.db.enums import (
    CONFIDENCE_MAX,
    CONFIDENCE_MIN,
    IMPORTANCE_MAX,
    IMPORTANCE_MIN,
    ITEM_SOURCE_DOMAIN,
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
    # When the captured thing happened (None = unknown) as an aware UTC
    # instant; format-specific extraction result (email headers, WhatsApp
    # messages, event fields; raw source strings kept for audit).
    occurred_at: datetime | None = None
    meta: dict[str, Any] = Field(default_factory=dict)

    @field_validator("source")
    @classmethod
    def _source_in_domain(cls, value: str) -> str:
        """The memory_items.source CHECK domain, API side too.

        Every capture path writes a fixed in-domain value (paste kinds,
        telegram, whatsapp, email poller, connectors); a client-supplied
        source must stay inside the same closed domain or the INSERT would
        500 on live the moment the CHECK migration runs (PR #53 review).
        """
        if value not in ITEM_SOURCE_DOMAIN:
            raise ValueError(
                f"source must be one of {', '.join(ITEM_SOURCE_DOMAIN)}"
            )
        return value


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

    @field_validator("source")
    @classmethod
    def _source_in_domain(cls, value: str | None) -> str | None:
        """Same closed domain as ItemCreate (absent stays absent)."""
        if value is not None and value not in ITEM_SOURCE_DOMAIN:
            raise ValueError(
                f"source must be one of {', '.join(ITEM_SOURCE_DOMAIN)}"
            )
        return value


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
    # True only on the item an ingest call RETURNED because it was an exact
    # duplicate of an already-stored row (see brain.services.dedupe); list/get
    # responses always carry False.
    deduplicated: bool = False


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


# --- Memory layer (entities + versioned facts) --------------------------------

ENTITY_NAME_MAX_LENGTH = 200
ENTITY_ALIAS_MAX_LENGTH = 200
FACT_PREDICATE_MAX_LENGTH = 64
FACT_VALUE_MAX_LENGTH = 2_000


class FactRead(BaseModel):
    """One version of a memory fact (versions chain via ``supersedes_id``)."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    subject_id: uuid.UUID
    predicate: str
    object_id: uuid.UUID | None = None
    value: str | None = None
    valid_from: datetime
    valid_to: datetime | None = None
    status: str
    supersedes_id: uuid.UUID | None = None
    provenance: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime
    # Populated only by supersede responses (confirm/add of a single-valued
    # predicate): the rows the server closed, so the client can offer an
    # Undo without deleting anything itself.
    replaced: list["FactRead"] = Field(default_factory=list)


class EntityRead(BaseModel):
    """An entity with its aliases and (current-version) facts."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    kind: str
    name: str
    aliases: list[str] = Field(default_factory=list)
    facts: list[FactRead] = Field(default_factory=list)
    created_at: datetime
    archived: bool = False


class GroupConversationRead(BaseModel):
    """One projected WhatsApp conversation in a person's groups section."""

    item_id: str
    occurred_at: datetime | None = None
    preview: str
    kept_fact_id: str | None = None


class GroupRead(BaseModel):
    """One of the person's groups with its newest relevant conversations."""

    channel: str
    applies_to: str
    conversations: list[GroupConversationRead] = Field(default_factory=list)


class GroupsRead(BaseModel):
    """GET /memory/entities/{id}/groups — a kid's groups projection."""

    groups: list[GroupRead] = Field(default_factory=list)


class GroupKeepCreate(BaseModel):
    """Payload for POST /memory/entities/{id}/groups/keep."""

    item_id: uuid.UUID


class AliasCreate(BaseModel):
    """Payload for POST /memory/entities/{id}/aliases."""

    alias: str = Field(min_length=1, max_length=ENTITY_NAME_MAX_LENGTH)
    lang: str | None = Field(default=None, max_length=8)

    @field_validator("alias")
    @classmethod
    def _alias_not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("alias must not be blank")
        return value


class MemoryOverview(BaseModel):
    """Response for GET /memory: entities with their facts, grouped."""

    entities: list[EntityRead]


class EntityCreate(BaseModel):
    """Payload for POST /memory/entities."""

    kind: Literal["person", "group", "place", "org", "channel"]
    name: str = Field(min_length=1, max_length=ENTITY_NAME_MAX_LENGTH)
    aliases: list[str] = Field(
        default_factory=list,
        max_length=20,
    )

    @field_validator("aliases")
    @classmethod
    def _aliases_not_blank(cls, value: list[str]) -> list[str]:
        for alias in value:
            if not alias.strip():
                raise ValueError("aliases must not be blank")
        return value


class EntityPatch(BaseModel):
    """Partial payload for PATCH /memory/entities/{id}."""

    name: str | None = Field(default=None, min_length=1, max_length=ENTITY_NAME_MAX_LENGTH)


class FactCreate(BaseModel):
    """Payload for POST /memory/facts."""

    subject_id: uuid.UUID
    predicate: str = Field(min_length=1, max_length=FACT_PREDICATE_MAX_LENGTH)
    object_id: uuid.UUID | None = None
    value: str | None = Field(default=None, max_length=FACT_VALUE_MAX_LENGTH)
    valid_from: datetime | None = None
    # Optional display title for memory facts (the composer's Title field).
    # memory_facts has no title column; provenance is a JSONB dict every row
    # already carries (and PATCH copies forward), so no migration is needed.
    title: str | None = Field(default=None, max_length=200)
    # confirmed (default) or proposed (the inbox); rejected is reached via DELETE only
    status: Literal["confirmed", "proposed"] = "confirmed"

    @model_validator(mode="after")
    def _object_or_value_required(self) -> "FactCreate":
        if self.object_id is None and (self.value is None or not self.value.strip()):
            raise ValueError("fact needs an object_id or a non-empty value")
        return self


class FactPatch(BaseModel):
    """Partial payload for PATCH /memory/facts/{id} (= new version)."""

    predicate: str | None = Field(default=None, min_length=1, max_length=FACT_PREDICATE_MAX_LENGTH)
    object_id: uuid.UUID | None = None
    value: str | None = Field(default=None, max_length=FACT_VALUE_MAX_LENGTH)


class RelationCreate(BaseModel):
    """PR B: create a relation between two items."""

    to_id: uuid.UUID
    kind: str
    provenance: dict[str, Any] = Field(default_factory=dict)
    # item 20b: propose=True creates a PROPOSED supersede (cross-thread LLM
    # match); the old item stays visible until POST /relations/{id}/confirm.
    propose: bool = False


class RelationRead(BaseModel):
    """A relation as returned by the API."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    from_id: uuid.UUID
    to_id: uuid.UUID
    kind: str
    provenance: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime


# --- editable prompts + memory file (/prompts) -----------------------------------
# The service (brain.services.prompts) owns the per-key limits and the
# placeholder contract; this cap only bounds the request body.
PROMPT_TEXT_MAX_LENGTH = 50_000


class PromptTextUpdate(BaseModel):
    """Payload for PUT /prompts/{key} and PUT /prompts/memory-file."""

    text: str = Field(max_length=PROMPT_TEXT_MAX_LENGTH)


class PromptSummary(BaseModel):
    """One registry key in the list view."""

    key: str
    title: str
    description: str
    kind: Literal["prompt", "memory_file"]
    is_overridden: bool
    # The current user version (None = on the code default).
    version: int | None = None
    # The newest history row (a reset row counts), None = never edited.
    latest_version: int | None = None
    updated_at: datetime | None = None


class PromptHistoryEntry(BaseModel):
    """One append-only version row; is_default = a reset (text = the default)."""

    version: int
    source: str
    is_default: bool
    text: str
    created_at: datetime


class PromptDetail(PromptSummary):
    """One key: current + default text, the placeholder contract, history."""

    text: str
    default_text: str
    placeholders: list[str]
    max_chars: int
    history: list[PromptHistoryEntry]


class PromptList(BaseModel):
    """GET /prompts."""

    prompts: list[PromptSummary]


# --- scheduled tasks (/schedules) ----------------------------------------------
# brain.services.scheduled_tasks / schedule_spec own the real validation
# (cron syntax, timezone, empty text) and answer 422 with a readable detail;
# these caps only bound the request body.


class ScheduledTaskIn(BaseModel):
    """Payload for POST /schedules and PUT /schedules/{id}."""

    name: str = Field(max_length=500)
    prompt: str = Field(max_length=20_000)
    schedule: str = Field(max_length=500)
    timezone: str = Field(default="Asia/Jerusalem", max_length=64)
    enabled: bool | None = None


class ScheduledTaskOut(BaseModel):
    """One task; schedule_text is the schedule in words (English)."""

    id: uuid.UUID
    name: str
    prompt: str
    schedule: str
    schedule_text: str
    timezone: str
    enabled: bool
    next_run_at: datetime | None = None
    last_run_at: datetime | None = None
    last_status: str | None = None
    run_requested: bool
    created_at: datetime
    updated_at: datetime


class ScheduledTaskList(BaseModel):
    """GET /schedules."""

    tasks: list[ScheduledTaskOut]


class ScheduledTaskRunOut(BaseModel):
    """One run (or a recorded skip) of a task."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    task_id: uuid.UUID
    trigger: str
    scheduled_for: datetime | None = None
    started_at: datetime
    finished_at: datetime | None = None
    status: str
    output: str | None = None
    error: str | None = None
    cited_ids: list[str]


class ScheduledTaskRunList(BaseModel):
    """GET /schedules/{id}/runs (newest first)."""

    runs: list[ScheduledTaskRunOut]


class SchedulePreviewIn(BaseModel):
    """POST /schedules/preview: validate a schedule without saving."""

    schedule: str = Field(max_length=500)
    timezone: str = Field(default="Asia/Jerusalem", max_length=64)


class SchedulePreviewOut(BaseModel):
    """The normalized cron, the schedule in words, and the next fires."""

    schedule: str
    schedule_text: str
    next_runs: list[datetime]
