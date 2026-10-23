"""WhatsApp relevance triage (D2a; design delta item 36).

Conversations (source='whatsapp') are labelled ``relevant`` | ``noise``.
Jev FIRST — one typed-choice call; the LLM only when Jev's confidence falls
below the band (or Jev is unavailable), ONE call per conversation with a
strict, versioned prompt. Noise stays stored and searchable but is hidden
from the default listings and never enqueued for enrichment. Triage never
deletes anything.

Failures leave the item UNTRIAGED (never guessed) and are recorded as an ops
event — best-effort by contract: the poll must never block on triage.
"""

import json
from dataclasses import dataclass
from typing import Any, Protocol

from brain import ops
from brain.ai.jev import JevError

TRIAGE_VERSION = "triage-v1"
# Below this Jev confidence the LLM gets the final say (item 36: LLM only
# when Jev is unsure — a band keeps the cheap path dominant).
JEV_CONFIDENT_BAND = 0.9
TRIAGE_LABELS = ("relevant", "noise")

# Conversations can be long; the triage prompt carries the last N chars —
# enough signal, bounded cost.
_MAX_PROMPT_CHARS = 4000

_TRIAGE_QUESTION = (
    "Is this WhatsApp conversation relevant to the family's shared memory "
    "(school, health, logistics that matter, plans, decisions), or is it noise?"
)
_TRIAGE_CRITERIA: dict[str, str] = {
    "relevant": (
        "carries information worth remembering: school/kids logistics that "
        "require action, health, dates, plans, decisions, requests"
    ),
    "noise": (
        "small talk, thanks/emoji-only, stickers, forwarded chain mail, "
        "logistics among other parents that requires nothing from the family"
    ),
}
# The strict, versioned LLM prompt: ONE call, JSON-only answer.
_PROMPT_TEMPLATE = """You triage a WhatsApp conversation for a family's memory system.
Decide whether the conversation is RELEVANT (school/kids logistics requiring
action, health, dates, plans, decisions, requests) or NOISE (small talk,
thanks/emoji-only, stickers, chain mail, other parents' logistics that
require nothing).

Conversation:
{text}

Answer with ONLY this JSON object (no prose, no markdown):
{{"label": "relevant", "confidence": 0.85}}
"label" is exactly "relevant" or "noise"; "confidence" is your 0..1 certainty.
Prompt version: {version}"""


@dataclass(frozen=True)
class TriageResult:
    """One triage decision (immutable; written to the item's triage columns)."""

    label: str
    confidence: float
    method: str  # 'jev' | 'llm'
    version: str


class TrierProto(Protocol):
    """What the poller injects: item -> decision (None = failed this pass)."""

    def __call__(self, item: Any) -> "TriageResult | None": ...


def triage_conversation(
    text: str,
    *,
    jev: Any,
    llm: Any,
) -> TriageResult | None:
    """Label one conversation. None means 'undecided' (ops event recorded)."""
    choice = None
    try:
        choice = jev.choice(_TRIAGE_QUESTION, _TRIAGE_CRITERIA, text)
    except JevError as exc:
        ops.record_event(
            "triage", "jev_unavailable", ok=False,
            detail={"error": str(exc)[:120]},
        )
    if (
        choice is not None
        and choice.option in TRIAGE_LABELS
        and choice.confidence >= JEV_CONFIDENT_BAND
    ):
        return _record(TriageResult(
            label=choice.option, confidence=choice.confidence, method="jev",
            version=TRIAGE_VERSION,
        ))

    # Low-confidence Jev (or Jev down): exactly ONE strict LLM call.
    try:
        completion = llm.complete(_PROMPT_TEMPLATE.format(
            text=text[-_MAX_PROMPT_CHARS:], version=TRIAGE_VERSION,
        ))
        parsed = json.loads(completion.text)
        label = str(parsed["label"])
        confidence = float(parsed["confidence"])
        if label not in TRIAGE_LABELS or not 0.0 <= confidence <= 1.0:
            raise ValueError(f"out-of-contract triage answer: {label!r}")
    except Exception as exc:  # noqa: BLE001 — any LLM failure is 'undecided'
        ops.record_event(
            "triage", "failed", ok=False,
            detail={"error": type(exc).__name__, "reason": str(exc)[:120]},
        )
        return None
    return _record(TriageResult(
        label=label, confidence=confidence, method="llm", version=TRIAGE_VERSION,
    ))


def _record(result: TriageResult) -> TriageResult:
    ops.record_event(
        "triage", "decided", ok=True,
        detail={
            "label": result.label,
            "method": result.method,
            "confidence": round(result.confidence, 4),
            "version": result.version,
        },
    )
    return result
