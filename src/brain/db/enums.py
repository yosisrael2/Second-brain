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


# The closed domain of ``memory_items.source`` (CHECK ``source``): capture
# paths that exist today (manual default, paste kinds, telegram bot, email
# poller) + the connector names the connectors ticket introduces. The
# migration adds it NOT VALID (the live table predates the constraint; new
# inserts are checked). New sources widen the domain in a migration — never
# silently. Lived in db/models.py until the API schemas needed it too
# (PR #53 review: POST/PATCH /items could send a source outside the domain).
ITEM_SOURCE_DOMAIN = (
    "manual",
    "note",
    "calendar",
    "email",
    "outlook",
    "whatsapp",
    "telegram",
    "gcal",
    "gdrive",
    "gtasks",
)
