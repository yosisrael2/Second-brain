"""The recon second-stage judge (approved plan; D-recon-1 follow-up (a)).

The band [RECON_JUDGE_FLOOR, RECON_SIMILARITY_THRESHOLD) — pairs that pass
type + applies_to + window + exclusion and clear the ambiguity margin but
sit below the match threshold — get ONE typed yes/no: "same real-world
thing, not another edition?". Jev first (typed choice, live-configured),
then exactly one strict versioned LLM call, then fall CLOSED (None) —
a judge failure NEVER produces a guessed match. In-process cache keyed by
the canonicalized pair; a hard per-pass cap bounds cost;
RECON_JUDGE_ENABLED defaults FALSE (off = byte-identical behavior).
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from typing import Any

from brain import ops
from brain.services.extraction.reconciliation import (
    RECON_JUDGE_FLOOR,
    RECON_SIMILARITY_THRESHOLD,
    ExistingItem,
    ReconCandidate,
)

logger = logging.getLogger(__name__)

RECON_JUDGE_VERSION = "recon-judge-v1"

# The band the judge serves; kept here for the ops detail payloads.
_BAND = (RECON_JUDGE_FLOOR, RECON_SIMILARITY_THRESHOLD)

_JUDGE_QUESTION = (
    "Are these two mentions the SAME real-world activity/fact (the same "
    "single occurrence or standing arrangement), or two different things?"
)
_JUDGE_OPTIONS = {
    "same": "the same single real-world thing (the same occurrence/arrangement)",
    "different": "two different things (a different activity, a different "
    "edition of a recurring event, or a different audience)",
}

_MAX_STATE_CHARS = 4000

_LLM_PROMPT_TEMPLATE = """You decide whether two mentions are the SAME real-world thing
for a family's memory system. Answer STRICT JSON only:
{{"same": true|false, "confidence": 0.0-1.0}}

Rules:
- Another yearly edition of a recurring event (a different date of a yearly
  trip) is DIFFERENT, not the same thing.
- A different class/kid audience is DIFFERENT.
- The same activity named differently (group name vs kid-facing name, a
  title plus a qualifier) is SAME when it is one occurrence/arrangement.

CANDIDATE (newly extracted):
{candidate}

EXISTING (stored item):
{existing}

version: {version}
"""


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", text).casefold().strip()


def _pair_key(candidate: ReconCandidate, existing: ExistingItem) -> str:
    """The cache key: the CANONICALIZED pair (sorted), so A-vs-B and B-vs-A
    share one entry. Type + audiences + both titles + both start stamps."""
    a = (
        candidate.kind,
        ",".join(sorted(candidate.applies_to)),
        _norm(candidate.title),
        candidate.start.isoformat() if candidate.start else "",
    )
    b = (
        existing.type,
        ",".join(sorted(existing.applies_to)),
        _norm(existing.title),
        existing.start.isoformat() if existing.start else "",
    )
    lo, hi = sorted([a, b])
    payload = json.dumps([lo, hi], ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _state(candidate: ReconCandidate, existing: ExistingItem) -> dict[str, Any]:
    """Both sides, bounded: titles, dates, audience, quotes."""
    return {
        "candidate": {
            "title": candidate.title,
            "start_iso": candidate.start.isoformat() if candidate.start else None,
            "end_iso": candidate.end.isoformat() if candidate.end else None,
            "location": candidate.location,
            "applies_to": list(candidate.applies_to),
        },
        "existing": {
            "title": existing.title,
            "start_iso": existing.start.isoformat() if existing.start else None,
            "end_iso": existing.end.isoformat() if existing.end else None,
            "location": existing.location,
            "applies_to": list(existing.applies_to),
        },
    }


class ReconJudge:
    """The band judge: Jev -> one strict LLM call -> fall CLOSED.

    Call with (candidate, existing); returns True (same thing — the pair
    continues down the normal verdict path), False (different), or None
    (undecided -> CLOSED). All I/O, caching, caps and ops live here;
    classify() stays pure.
    """

    def __init__(
        self,
        *,
        jev: Any | None,
        llm: Any | None,
        enabled: bool = True,
        floor: float = RECON_JUDGE_FLOOR,
        confidence: float = 0.9,
        max_calls: int = 20,
    ) -> None:
        self._jev = jev
        self._llm = llm
        self._confidence = confidence
        self._max_calls = max_calls
        self._cache: dict[str, bool | None] = {}
        self._calls = 0
        self._capped = False
        self._version = RECON_JUDGE_VERSION

    def __call__(self, candidate: ReconCandidate, existing: ExistingItem) -> bool | None:
        key = _pair_key(candidate, existing)
        if key in self._cache:
            return self._cache[key]
        if self._calls >= self._max_calls:
            if not self._capped:
                self._capped = True
                ops.record_event(
                    "recon", "judge_capped", ok=False,
                    detail={"cap": self._max_calls, "calls": self._calls},
                )
            return None
        self._calls += 1
        answer = self._decide(candidate, existing)
        self._cache[key] = answer
        return answer

    def _decide(self, candidate: ReconCandidate, existing: ExistingItem) -> bool | None:
        answer = self._ask_jev(candidate, existing)
        if answer is None and self._llm is not None:
            answer = self._ask_llm(candidate, existing)
        return answer

    def _ask_jev(self, candidate: ReconCandidate, existing: ExistingItem) -> bool | None:
        if self._jev is None:
            return None
        try:
            choice = self._jev.choice(
                _JUDGE_QUESTION, _JUDGE_OPTIONS, _state(candidate, existing)
            )
        except Exception as exc:  # noqa: BLE001 — any Jev failure falls through
            self._ops("judge_error", method="jev",
                      detail={"error": type(exc).__name__, "reason": str(exc)[:120]})
            return None
        if choice.confidence < self._confidence:
            return None  # below the band: one strict LLM call decides
        answer = choice.option == "same"
        self._ops("judge_yes" if answer else "judge_no", method="jev",
                  confidence=choice.confidence)
        return answer

    def _ask_llm(self, candidate: ReconCandidate, existing: ExistingItem) -> bool | None:
        if self._llm is None:
            return None
        try:
            completion = self._llm.complete(_LLM_PROMPT_TEMPLATE.format(
                candidate=json.dumps(_state(candidate, existing)["candidate"],
                                     ensure_ascii=False),
                existing=json.dumps(_state(candidate, existing)["existing"],
                                    ensure_ascii=False),
                version=self._version,
            )[:_MAX_STATE_CHARS])
            parsed = json.loads(completion.text)
            same = parsed["same"]
            confidence = float(parsed["confidence"])
            if not isinstance(same, bool) or not 0.0 <= confidence <= 1.0:
                raise ValueError(f"out-of-contract judge answer: {parsed!r}")
            if confidence < self._confidence:
                self._ops("judge_no", method="llm", confidence=confidence)
                return None  # low confidence -> CLOSED
            self._ops("judge_yes" if same else "judge_no", method="llm",
                      confidence=confidence)
            return same
        except Exception as exc:  # noqa: BLE001 — any LLM failure is CLOSED
            self._ops("judge_error", method="llm",
                      detail={"error": type(exc).__name__, "reason": str(exc)[:120]})
            return None

    def _ops(self, kind: str, *, method: str,
             confidence: float | None = None,
             detail: dict[str, Any] | None = None) -> None:
        ops.record_event(
            "recon", kind, ok=not kind.endswith("error"),
            detail={
                "method": method,
                **({"confidence": confidence} if confidence is not None else {}),
                **(detail or {}),
            },
        )


def build_judge(
    settings: Any, *, jev: Any | None = None, llm: Any | None = None
) -> ReconJudge | None:
    """The ONE builder for the judge; None when disabled (the default) —
    off means byte-identical behavior to the pre-judge tree."""
    if not getattr(settings, "recon_judge_enabled", False):
        return None
    return ReconJudge(
        jev=jev,
        llm=llm,
        enabled=True,
        floor=settings.recon_judge_floor,
        confidence=settings.recon_judge_confidence,
        max_calls=settings.recon_judge_max_calls,
    )
