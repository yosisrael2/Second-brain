"""Temporary enum copies of the shared domain contract.

TODO(worker_2): swap these imports for the shared enums once worker_1 lands
them in ``brain.domain`` (or ``brain.db.enums``) and rebase. Keep values in
lockstep with the manager's decision: ItemType = task|decision|note|event|
reference|idea; Actionability = none|later|now; ItemStatus = inbox|filed|
needs_review.
"""

from enum import StrEnum


class ItemType(StrEnum):
    """What kind of memory item this is."""

    TASK = "task"
    DECISION = "decision"
    NOTE = "note"
    EVENT = "event"
    REFERENCE = "reference"
    IDEA = "idea"


class Actionability(StrEnum):
    """How urgently an item calls for action."""

    NONE = "none"
    LATER = "later"
    NOW = "now"


class ItemStatus(StrEnum):
    """Where the item sits in the triage pipeline."""

    INBOX = "inbox"
    NEEDS_REVIEW = "needs_review"
    FILED = "filed"
