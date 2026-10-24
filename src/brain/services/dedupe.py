"""Exact-duplicate detection at ingest (content fingerprint).

A capture is an exact duplicate of an existing item when the normalized text,
the event minute (occurred_at in UTC), and the source kind all match. The
fingerprint is a sha256 over those three components; it lives in an indexed
``memory_items.fingerprint`` column so matching is a single indexed lookup.

Normalization folds away the differences copies pick up along the way:
Unicode NFKC, bidi control marks (Hebrew/RTL pastes), whitespace runs, and
case (casefold). Distinct real events stay distinct: a weekly recurring
meeting has the same title and source but a different ``occurred_at`` minute,
so it gets its own fingerprint.
"""

from __future__ import annotations

import hashlib
import unicodedata
from datetime import UTC, datetime

from brain.services.paste import _RTL_MARKS

_SEPARATOR = "\x1f"  # unit separator: never appears in normalized text


def normalize_text(text: str) -> str:
    """Fold copy artifacts: NFKC, strip bidi marks, collapse whitespace, casefold."""
    text = unicodedata.normalize("NFKC", text)
    for mark in _RTL_MARKS:
        text = text.replace(mark, "")
    return " ".join(text.split()).casefold()


def match_key(raw_text: str, occurred_at: datetime | None) -> tuple[str, str]:
    """(normalized text, occurred_at as UTC minute) — the SOURCE-BLIND key.

    ``content_fingerprint`` deliberately includes the source, so it can never
    match a pasted item against a connector record (different source). The
    adopt step (connectors/adopt.py) needs exactly that source-blind
    comparison: same normalization, same minute bucketing, no sha256 (the
    key is compared, not stored).
    """
    minute = ""
    if occurred_at is not None:
        aware = occurred_at if occurred_at.tzinfo is not None else occurred_at.replace(tzinfo=UTC)
        minute = aware.astimezone(UTC).strftime("%Y%m%dT%H%M")
    return normalize_text(raw_text), minute


def content_fingerprint(raw_text: str, occurred_at: datetime | None, source: str) -> str:
    """sha256 of (normalized text, occurred_at as UTC minute, source kind).

    Naive datetimes are interpreted as UTC (the paste layer produces aware
    UTC instants; naive only shows up with hand-built payloads). ``None``
    occurred_at (plain notes) is its own component value, so two identical
    notes still dedupe against each other.
    """
    minute = ""
    if occurred_at is not None:
        aware = occurred_at if occurred_at.tzinfo is not None else occurred_at.replace(tzinfo=UTC)
        minute = aware.astimezone(UTC).strftime("%Y%m%dT%H%M")
    payload = _SEPARATOR.join((normalize_text(raw_text), minute, source))
    return hashlib.sha256(payload.encode()).hexdigest()
