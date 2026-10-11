"""Domain enums and shared constants. Defined here once; import, never duplicate."""

import enum

FTS_LANGUAGE = "english"
"""Postgres text-search configuration used by MemoryItem full-text search."""

IMPORTANCE_MIN = 1
IMPORTANCE_MAX = 5
CONFIDENCE_MIN = 0.0
CONFIDENCE_MAX = 1.0


class ItemType(str, enum.Enum):
    """What kind of knowledge a memory item represents."""

    TASK = "task"
    DECISION = "decision"
    NOTE = "note"
    EVENT = "event"
    REFERENCE = "reference"
    IDEA = "idea"


class Actionability(str, enum.Enum):
    """How urgently an item calls for action."""

    NONE = "none"
    LATER = "later"
    NOW = "now"


class ItemStatus(str, enum.Enum):
    """Workflow state of a memory item."""

    INBOX = "inbox"
    FILED = "filed"
    NEEDS_REVIEW = "needs_review"
