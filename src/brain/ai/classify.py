"""Classify free text into typed memory-item fields via Jev (Service layer)."""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from brain.db.enums import IMPORTANCE_MAX, IMPORTANCE_MIN, Actionability, ItemStatus, ItemType

from .jev import JevClientProtocol, JevError, parse_choice, parse_score

logger = logging.getLogger(__name__)

ROUTE_FILE_THRESHOLD = 0.9
ROUTE_REVIEW_THRESHOLD = 0.5

TYPE_CRITERIA: dict[str, str] = {
    member.value: description
    for member, description in {
        ItemType.TASK: "Something the user must do or decide to do, with an implied action",
        ItemType.DECISION: "A choice to be made, or a decision already made",
        ItemType.EVENT: "Something happening at a specific date or time",
        ItemType.NOTE: "General information worth keeping but not tied to an action",
        ItemType.REFERENCE: "External material to look up later, such as a link or document",
        ItemType.IDEA: "A creative thought, possibility, or future project seed",
    }.items()
}
IMPORTANCE_RUBRIC: tuple[str, ...] = (
    "Trivial: losing it costs nothing",
    "Minor: nice to keep, rarely needed",
    "Useful: will matter sometime",
    "Important: affects current work or deadlines",
    "Critical: urgent, high-stakes, blocks other work",
)
ACTIONABILITY_RUBRIC: tuple[str, ...] = (
    "Nothing to do: purely informational or archival",
    "Actionable but not urgent: act on it in the future",
    "Needs action immediately or today",
)
_ACTIONABILITY_BY_RANK: dict[int, Actionability] = {
    1: Actionability.NONE,
    2: Actionability.LATER,
    3: Actionability.NOW,
}
PROJECT_NONE = "none"

FALLBACK_CLASSIFICATION: dict[str, Any] = {
    "type": None,
    "project": None,
    "importance": None,
    "actionability": None,
    "confidence": 0.0,
}


def route(confidence: float) -> ItemStatus:
    """Map a classifier confidence to an ItemStatus.

    >=0.9 filed, 0.5-0.9 needs_review, below 0.5 inbox.
    """
    if confidence >= ROUTE_FILE_THRESHOLD:
        return ItemStatus.FILED
    if confidence >= ROUTE_REVIEW_THRESHOLD:
        return ItemStatus.NEEDS_REVIEW
    return ItemStatus.INBOX


def classify(
    text: str,
    jev: JevClientProtocol,
    projects: Sequence[str] | None = None,
) -> dict[str, Any]:
    """Classify one item of text with a single Jev call.

    Returns ``{type: ItemType, project: str|None, importance: int,
    actionability: Actionability, confidence: float}``. When ``projects`` is
    given, one extra question asks Jev to pick the project. Any Jev failure
    (down, timeout, malformed output) returns :data:`FALLBACK_CLASSIFICATION`
    instead of raising; with confidence 0.0, ``route()`` sends that result to
    the inbox.
    """
    try:
        answers = jev.decide(text.strip(), _build_questions(projects))
        return _build_result(answers, projects)
    except JevError as exc:
        logger.warning("classify: jev unavailable (%s); routing to inbox", exc)
        return dict(FALLBACK_CLASSIFICATION)


@dataclass(frozen=True)
class JevClassifier:
    """Adapter exposing :func:`classify` as an injectable Strategy object."""

    jev: JevClientProtocol
    projects: tuple[str, ...] = ()

    def classify(self, text: str) -> dict[str, Any]:
        """Classify ``text``, optionally restricted to configured projects."""
        return classify(text, self.jev, self.projects or None)


def _build_questions(projects: Sequence[str] | None) -> dict[str, dict[str, Any]]:
    questions: dict[str, dict[str, Any]] = {
        "type": {
            "type": "choice",
            "instructions": "What kind of item is this?",
            "criteria": TYPE_CRITERIA,
        },
        "importance": {
            "type": "score",
            "instructions": "How important is this item? Rubric levels are 1 (low) to 5 (high).",
            "criteria": list(IMPORTANCE_RUBRIC),
        },
        "actionability": {
            "type": "score",
            "instructions": "How actionable is this item? Rubric levels are 1 (low) to 3 (high).",
            "criteria": list(ACTIONABILITY_RUBRIC),
        },
    }
    if projects:
        criteria = {name: name for name in projects}
        criteria[PROJECT_NONE] = "No listed project fits this item"
        questions["project"] = {
            "type": "choice",
            "instructions": "Which project does this item belong to?",
            "criteria": criteria,
        }
    return questions


def _build_result(answers: dict[str, Any], projects: Sequence[str] | None) -> dict[str, Any]:
    item_type = parse_choice(answers["type"])
    importance = parse_score(answers["importance"])
    actionability = parse_score(answers["actionability"])
    return {
        "type": _to_item_type(item_type.option),
        "project": _resolve_project(answers, projects),
        "importance": _round_clamp(importance.score, IMPORTANCE_MIN, IMPORTANCE_MAX),
        "actionability": _to_actionability(actionability.score),
        "confidence": item_type.confidence,
    }


def _to_item_type(raw: str) -> ItemType | None:
    try:
        return ItemType(raw)
    except ValueError:
        return None


def _to_actionability(score: float) -> Actionability:
    """Map a Jev score (1-based rubric index) to its Actionability level."""
    rank = _round_clamp(score, 1, len(_ACTIONABILITY_BY_RANK))
    return _ACTIONABILITY_BY_RANK[rank]


def _resolve_project(answers: dict[str, Any], projects: Sequence[str] | None) -> str | None:
    if not projects or "project" not in answers:
        return None
    project = parse_choice(answers["project"]).option
    return project if project in projects else None


def _round_clamp(value: float, low: int, high: int) -> int:
    return min(high, max(low, round(value)))
