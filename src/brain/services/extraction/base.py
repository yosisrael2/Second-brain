"""Extractor interface + the A1 deterministic stub (brief item 3).

The interface is the whole point of A1: A2's LLM extractor plugs in without
touching the pipeline, the queue or the storage. The stub is intentionally
dumb and deterministic — one candidate per line that looks self-contained —
so the pipeline around it (grounding, dates, applies_to, diff) is fully
testable without any model call.
"""

from __future__ import annotations

from typing import Protocol

from brain.db.models import MemoryItem
from brain.services.extraction import dates as xdates
from brain.services.extraction.candidates import CHILD_KINDS, ChildCandidate

# The extractor sees the memory layer's compact prompt block (kids, classes,
# channels) so A2 can resolve "Maayan's class" against real names.
MemoryContext = str


class Extractor(Protocol):
    """extract(source_item, memory_context) -> list[ChildCandidate]."""

    def extract(self, source: MemoryItem, memory_context: MemoryContext) -> list[ChildCandidate]:
        """Propose children for one source item; MUST be side-effect free."""
        ...


class StubExtractor:
    """Deterministic placeholder extractor (A1).

    Every non-empty line of the source text becomes one 'event' candidate
    whose quote is the line itself — which is grounded by construction, so
    the stub alone never triggers the grounding guard (a separate test does
    that with a hand-built candidate). Lines shorter than 8 chars or longer
    than 200 are ignored. A line carrying a date expression passes THE WHOLE
    LINE as ``when_raw`` — the pipeline's resolvers scan for their own
    patterns (Hebrew month, numeric date, weekday) and ignore the rest, so
    a weekday and its date stay together for the conflict check (item 2b).
    """

    name = "stub-v0"  # provenance marker in extraction_meta
    prompt_version = None  # the stub has no prompt

    _MIN_CHARS = 8
    _MAX_CHARS = 200
    _MAX_CHILDREN = 6  # spec's worked example: 6 children from one email

    def extract(self, source: MemoryItem, memory_context: MemoryContext) -> list[ChildCandidate]:
        candidates: list[ChildCandidate] = []
        seen: set[str] = set()
        for line in source.raw_text.splitlines():
            text = line.strip()
            if not self._MIN_CHARS <= len(text) <= self._MAX_CHARS or text in seen:
                continue
            seen.add(text)
            candidates.append(
                ChildCandidate(
                    kind="event",
                    title=text,
                    quote=text,
                    when_raw=text if xdates.DATEISH_RE.search(text) else "",
                )
            )
            if len(candidates) >= self._MAX_CHILDREN:
                break
        return candidates


def is_valid_kind(kind: str) -> bool:
    return kind in CHILD_KINDS
