"""ChildCandidate: one proposed child item, before and after validation.

A2's LLM extractor and A1's stub both speak this shape (interface contract,
brief item 3). Date fields stay RAW here — the pipeline resolves them (delta
items 2b, 41) and raises ``date_conflict`` instead of guessing.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# Child kinds (spec section 2). PR B adds relations; A1 only stores children.
CHILD_KINDS = ("event", "task", "reminder", "fact")


@dataclass(frozen=True)
class ReconHint:
    """PR D-recon (item 37): the extractor's per-candidate reconciliation
    hint. A HINT only helps select the matching window — it can never create
    a match (type + applies_to + similarity are always code-checked) and it
    can never silence one (every verdict is still a proposal, D2).
    """

    change: bool = False  # correction/update language or a stated field change
    cancel: bool = False  # cancellation language
    detail_only: bool = False  # pure addition (dress code, what to bring)


@dataclass(frozen=True)
class ChildCandidate:
    """A proposed child extracted from one source item.

    Attributes mirror the spec's child card: kind + title + a raw "when"
    expression + the verbatim quote it came from + audience fields (item 17)
    + importance/actionability. ``quote`` MUST appear verbatim in the source
    text (grounding guard, item 8) or the candidate is dropped.
    """

    kind: str
    title: str
    quote: str
    # Raw date/time expression, e.g. "ז' בחשוון", "יום חמישי 22.10",
    # "this Thursday". Empty for dateless children (facts).
    when_raw: str = ""
    # Raw clock time inside when_raw, e.g. "13:35" (kept separate so the
    # pipeline can combine date + time deterministically).
    time_raw: str = ""
    all_day: bool = False
    # PR D-recon (item 37, F2): where the event takes place, when the source
    # states it for THIS child (feeds the reconciliation location diff).
    location: str | None = None
    # Round 5: the stated END time for THIS child ("עד 18:00", ICS DTEND
    # style) — raw; resolved against the candidate's day in the pipeline.
    end: str | None = None
    # Audience selector per item 17: keys from {school, grades, role, gender,
    # named_kids, channel}; resolved to applies_to by the pipeline (R1-R5).
    audience: dict[str, object] = field(default_factory=dict)
    importance: int | None = None
    actionability: str | None = None
    due: bool = False  # True when when_raw is a DEADLINE (task) not a start
    # Item 22 (derived tasks): this candidate is DERIVED from the candidate
    # whose ``ref`` appears here ("sign the form" -> reminder to actually do
    # it by the due date). Empty quote is allowed ONLY for derived children
    # (they cite the fact they derive from); the pipeline skips their
    # grounding check and records the link in extraction_meta.
    derived_from: str | None = None
    # PR D-recon (item 37): the reconciliation hint (see ReconHint). Absent
    # on every pre-recon candidate; the pipeline treats None as "no signal".
    recon: ReconHint | None = None
