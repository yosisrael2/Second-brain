"""WhatsApp relevance triage (D2a; design delta item 36).

Conversations (source='whatsapp') are labelled ``relevant`` | ``noise``.
Deterministic override FIRST: a conversation naming a confirmed kid is
relevant, method 'kid_named' — never asked to the model. Otherwise Jev —
one typed-choice call; the LLM only when Jev's confidence falls below the
band (or Jev is unavailable), ONE call per conversation with a strict,
versioned prompt. Noise stays stored and searchable but is hidden from the
default listings and never enqueued for enrichment. Triage never deletes
anything.

Failures leave the item UNTRIAGED (never guessed) and are recorded as an ops
event — best-effort by contract: the poll must never block on triage.
"""

import json
import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from brain import ops
from brain.ai.jev import JevError

TRIAGE_VERSION = "triage-v3"
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
        "anything with a date/time/place change, a request to parents, a class "
        "activity, a safety/health notice, staff changes/absences/illness in "
        "our kids' class or school, new groups/registrations/sign-ups for a "
        "school program, or naming one of OUR kids"
    ),
    "noise": (
        "pure reactions (thanks, emoji, 'how fun', '+1', 'told her'); media-only "
        "messages ([image]/[document]/[sticker] with no text or a one-word "
        "caption) unless a date/task/event/change is stated; logistics about "
        "OTHER families' children (pick-ups, 'tell X her grandma is coming'); "
        "lost & found / giveaways / marketplace; contact sheets, phone lists, "
        "group admin"
    ),
}
# The strict, versioned LLM prompt: ONE call, JSON-only answer.
_PROMPT_TEMPLATE = """You triage a WhatsApp conversation for a family's memory system.
Our children's names are known to the system; messages about other families'
children (pick-up arrangements, 'tell X her grandma is coming', 'Y is waiting
at the gate') are NOISE.

Label RELEVANT when the conversation carries any of:
- a date, time or place change; a plan or event (even a tentative one)
- a request directed at parents (bring something, sign, volunteer, confirm)
- a class or school activity description worth remembering
- a safety or health notice
- something about one of our kids by name
- staff changes / absences / illness in our kids' class or school (an aide
  or teacher out, substitutes, contact info for them)
- new parents' groups, registrations or sign-ups opening for a school
  program

Label NOISE when the conversation is any of:
- pure reactions: thanks, emoji-only, 'how fun', '+1', 'told her'
- media-only: an [image]/[document]/[sticker] placeholder with no text or only
  a one-word caption, UNLESS the caption or surrounding messages state a date,
  task, event or change (the attachment itself is triaged separately later)
- logistics about other families' children: pick-up arrangements, relays like
  'tell X her grandma is coming', 'Y is waiting at the gate'
- lost & found, giveaways, marketplace offers
- contact sheets, phone lists, group-admin notices

When in doubt between the two, prefer RELEVANT: hiding something real is worse
than showing chatter.

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


_HEBREW_LETTER = "[\u05d0-\u05ea]"
# Hebrew words glue one-letter prefixes onto the word itself ('לאיתמר',
# 'ואיתמר', 'שלאיתמר') — up to 3 of them, directly before the name.
_HEBREW_PREFIX_RUN = "[והבכלמש]{0,3}"


def mentions_name(name: str, text: str) -> bool:
    """Whole-word name match (THE one implementation — ask + triage share it).

    'May' must not fire inside 'mayor', 'שחר' not inside 'שחרור': nothing
    word-like may touch the name (\w is Unicode-aware, so Hebrew letters
    count on both sides). For Hebrew-letter names an optional run of up to
    3 Hebrew prefix letters is allowed right before the name, still with
    nothing word-like before the run and after the name.
    """
    if not name:
        return False
    body = re.escape(name)
    if re.search(_HEBREW_LETTER, name):
        body = rf"{_HEBREW_PREFIX_RUN}{body}"
    return re.search(rf"(?<!\w){body}(?!\w)", text, re.IGNORECASE) is not None


def is_name_token(name: str, token: str) -> bool:
    """Full-TOKEN name match (audience resolution — the conservative sibling).

    Unlike :func:`mentions_name` (a whole-word SEARCH inside longer text),
    here the token must BE the name: an extracted "רונה לוי" or "Rona
    Cohen" must NOT resolve to our Rona — zero wrong-child is the A2 bar,
    and an "unknown" is always safer. Same normalisation as mentions_name:
    case-insensitive, and for Hebrew-letter names an optional run of up to
    3 Hebrew prefix letters directly before the name (ורונה still matches).
    """
    if not name or not token:
        return False
    token = token.strip()
    if not token:
        return False
    body = re.escape(name)
    if re.search(_HEBREW_LETTER, name):
        body = rf"{_HEBREW_PREFIX_RUN}{body}"
    return re.fullmatch(body, token, re.IGNORECASE) is not None


def confirmed_kid_names(entities: Sequence[dict[str, Any]]) -> list[str]:
    """Names + aliases of confirmed kids (kind 'person' with a grade fact).

    The SAME kid definition the kid-recall leg uses (PR 0d); the triage
    override and /ask must never disagree about who 'our kids' are.
    """
    names: list[str] = []
    for entity in entities:
        if entity.get("kind") != "person":
            continue
        if not any(
            fact.get("predicate") == "grade" for fact in entity.get("facts", [])
        ):
            continue
        names.append(entity.get("name", ""))
        names.extend(entity.get("aliases") or [])
    return [name for name in names if name]


def triage_conversation(
    text: str,
    *,
    jev: Any,
    llm: Any,
    kid_names: Sequence[str] | None = None,
) -> TriageResult | None:
    """Label one conversation. None means 'undecided' (ops event recorded)."""
    # Deterministic override (v3): naming a confirmed kid is relevant —
    # no model call, no confidence dance. The asymmetric safe side.
    for name in kid_names or ():
        if mentions_name(name, text):
            return _record(TriageResult(
                label="relevant", confidence=1.0, method="kid_named",
                version=TRIAGE_VERSION,
            ))
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
