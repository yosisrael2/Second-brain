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
   U+05C1, U+05C2, U+05C4, U+05C5, U+05C7). Maqaf/paseq/sof pasuq stay: they
   are punctuation, not vocalization.
4. Unify geresh/gershayim so quote style differences never matter:
   ׳ (U+05F3), ’ (U+2019) -> ' ; ״ (U+05F4), “ (U+201C), ” (U+201D) -> ".
5. Collapse all whitespace runs (email HTML line wrapping) to single spaces,
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

# Geresh/gershayim look-alikes -> ASCII quote characters.
_QUOTE_MAP = {
    "\u05f3": "'",  # hebrew punctuation geresh
    "\u2019": "'",  # right single quotation mark
    "\u05f4": '"',  # hebrew punctuation gershayim
    "\u201c": '"',  # left double quotation mark
    "\u201d": '"',  # right double quotation mark
}


# Geresh/gershayim look-alikes -> ASCII quote characters.
_QUOTE_MAP = {
    "\u05f3": "'",  # ׳ -> '
    "\u2019": "'",  # ’ -> '
    "\u05f4": '"',  # ״ -> "
    "\u201c": '"',  # “ -> "
    "\u201d": '"',  # ” -> "
}


def normalize(text: str) -> str:
    """Fold copy artifacts into a canonical comparable form (see module docstring)."""
    text = unicodedata.normalize("NFKC", text)
    for mark in _MARKS:
        text = text.replace(mark, "")
    text = _NIKUD_RE.sub("", text)
    for src, dst in _QUOTE_MAP.items():
        text = text.replace(src, dst)
    return " ".join(text.split()).casefold()


def quote_in_source(quote: str, source: str) -> bool:
    """True iff the normalized quote appears in the normalized source text.

    An empty (or whitespace-only) quote is never grounded, so a missing quote
    can never pass the grounding guard by accident.
    """
    needle = normalize(quote)
    return bool(needle) and needle in normalize(source)
