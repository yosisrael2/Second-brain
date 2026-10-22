"""Domain enums and shared constants. Defined here once; import, never duplicate."""

import enum

FTS_LANGUAGE = "simple"
"""Postgres text-search config for MemoryItem full-text search.

'simple' (not 'english') so HEBREW words match: the english config stems
English only and reduces Hebrew tokens to noise — the user's data is
mostly Hebrew. Stemming quality for English drops slightly; the vector
path carries semantic matching anyway.
"""

IMPORTANCE_MIN = 1
IMPORTANCE_MAX = 5
CONFIDENCE_MIN = 0.0
CONFIDENCE_MAX = 1.0


class ItemType(enum.StrEnum):
    """What kind of knowledge a memory item represents."""

    TASK = "task"
    DECISION = "decision"
    NOTE = "note"
    EVENT = "event"
    REMINDER = "reminder"
    FACT = "fact"
    REFERENCE = "reference"
    IDEA = "idea"


class Actionability(enum.StrEnum):
    """How urgently an item calls for action."""

    NONE = "none"
    LATER = "later"
    NOW = "now"


class ItemStatus(enum.StrEnum):
    """Workflow state of a memory item."""

    INBOX = "inbox"
    FILED = "filed"
    NEEDS_REVIEW = "needs_review"
