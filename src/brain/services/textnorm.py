"""Shared text normalization — the ONE module for quote grounding and eval matching.

The meaning-extraction pipeline (grounding guard) and the extraction eval
harness must never grow separate normalizers: both answer the question "does
this quote literally appear in this (Hebrew) source text?", and Hebrew text
defeats naive substring matching in several ways. ``normalize`` folds all of
them, in order:

1. Unicode NFKC — folds compatibility forms (fullwidth chars, ligatures).
2. Strip bidi control marks and zero-width/invisible characters that email
   HTML and copy/paste sprinkle into RTL text (LRM/RLM, embeddings/overrides/
   isolates, ZWSP/ZWNJ/ZWJ, soft hyphen, BOM).
3. Strip nikud — Hebrew points and cantillation (U+0591–U+05BD, U+05BF,
   U+05C1, U+05C2, U+05C4, U+05C5, U+05C7). Paseq/sof pasuq stay: they are
   punctuation, not vocalization.
4. Unify quote characters so style differences never matter: ׳ (U+05F3),
   ‘ (U+2018), ’ (U+2019) -> ' ; ״ (U+05F4), “ (U+201C), ” (U+201D) -> ".
5. Unify dash-like separators: Hebrew maqaf ־ (U+05BE), en dash – (U+2013),
   em dash — (U+2014) -> '-', so 'ה-ו' (hyphen) matches 'ה־ו' (maqaf) across
   email vs WhatsApp wording.
6. Collapse all whitespace runs (email HTML line wrapping) to single spaces,
   strip, and casefold.

``quote_in_source`` is the canonical grounding check used by both callers:
the normalized quote must appear as a substring of the normalized source.
"""

from __future__ import annotations

import re
import unicodedata

# Bidi controls + zero-width/invisible characters (paste/HTML artifacts).
_MARKS = (
    "\u00ad",  # soft hyphen
    "\u200b",  # zero-width space
    "\u200c",  # zero-width non-joiner
    "\u200d",  # zero-width joiner
    "\u200e",  # LRM
    "\u200f",  # RLM
    "\u202a",  # LRE
    "\u202b",  # RLE
    "\u202c",  # PDF
    "\u202d",  # LRO
    "\u202e",  # RLO
    "\u2060",  # word joiner
    "\u2061",  # function application
    "\u2062",  # invisible times
    "\u2063",  # invisible separator
    "\u2064",  # invisible plus
    "\u2066",  # LRI
    "\u2067",  # RLI
    "\u2068",  # FSI
    "\u2069",  # PDI
    "\ufeff",  # BOM / zero-width no-break space
)

# Nikud: Hebrew points + cantillation. NOT maqaf/paseq/sof pasuq (punctuation).
_NIKUD_RE = re.compile(r"[\u0591-\u05bd\u05bf\u05c1\u05c2\u05c4\u05c5\u05c7]")

# Quote/dash look-alikes -> ASCII characters.
_PUNCT_MAP = {
    "\u05f3": "'",  # ׳ hebrew geresh -> '
    "\u2018": "'",  # ‘ left single quotation mark -> '
    "\u2019": "'",  # ’ right single quotation mark -> '
    "\u05f4": '"',  # ״ hebrew gershayim -> "
    "\u201c": '"',  # “ left double quotation mark -> "
    "\u201d": '"',  # ” right double quotation mark -> "
    "\u05be": "-",  # ־ hebrew maqaf -> -
    "\u2013": "-",  # – en dash -> -
    "\u2014": "-",  # — em dash -> -
}


def normalize(text: str) -> str:
    """Fold copy artifacts into a canonical comparable form (see module docstring)."""
    text = unicodedata.normalize("NFKC", text)
    for mark in _MARKS:
        text = text.replace(mark, "")
    text = _NIKUD_RE.sub("", text)
    for src, dst in _PUNCT_MAP.items():
        text = text.replace(src, dst)
    return " ".join(text.split()).casefold()


def quote_in_source(quote: str, source: str) -> bool:
    """True iff the normalized quote appears in the normalized source text.

    An empty (or whitespace-only) quote is never grounded, so a missing quote
    can never pass the grounding guard by accident.
    """
    needle = normalize(quote)
    return bool(needle) and needle in normalize(source)


def title_similarity(a: str, b: str) -> float:
    """max(SequenceMatcher ratio, token Jaccard) over normalized titles.

    Shared matcher (extraction re-runs + the eval harness must agree — PR #33
    review round 1): SequenceMatcher handles near-identical strings and small
    edits; token Jaccard handles word reordering. Both operate on this
    module's normalizer output, so geresh/nikud/bidi/whitespace/case
    differences never matter and Hebrew and English titles behave the same.
    """
    import re as _re  # noqa: PLC0415
    from difflib import SequenceMatcher  # noqa: PLC0415

    left, right = normalize(a), normalize(b)
    if not left or not right:
        return 0.0
    if left == right:
        return 1.0
    ratio = SequenceMatcher(None, left, right).ratio()
    left_tokens = set(_re.findall(r"\S+", left))
    right_tokens = set(_re.findall(r"\S+", right))
    jaccard = (
        len(left_tokens & right_tokens) / len(left_tokens | right_tokens)
        if left_tokens and right_tokens
        else 0.0
    )
    return max(ratio, jaccard)
