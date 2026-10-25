"""``brain connectors adopt`` — one-time legacy-item adoption (ledger seeding).

The first live sync of a connector is GUARANTEED to duplicate items that
already exist in the KB from pre-connector capture paths (pasted calendar
one-liners carry ``source='calendar'``, the email bot's forwarded items carry
``source='email'``), because :func:`brain.services.dedupe.content_fingerprint`
includes the SOURCE and the connector records carry the connector's own name.
Adoption teaches the idempotency ledger (``connector_records``) about the
EXISTING items so the first sync sees them as known (same version -> skipped)
instead of capturing second copies.

Design (approved plan, connectors-adopt-plan.md):
- the connector->legacy-source mapping is DATA (``LEGACY_SOURCES``), not
  per-adapter code;
- match rules collect every candidate a record hits, then apply ONE
  decision: zero matches -> NEW, one match -> adopt (the strongest rule is
  reported), 2+ matches -> AMBIGUOUS (never guessed, never adopted). The
  rules, in order: **A** exact ``rfc_message_id`` (both sides carry it in
  meta), **B** ``(normalize_text, UTC minute)`` equality, **C**
  email-only: normalized subject + sender ADDRESS + sent time within
  :data:`RULE_C_WINDOW` (±2 min);
- adopting writes ONLY ledger rows (upsert via the runner's own repo) + ONE
  ``ops_events`` row recording the exact (external_id, version, item_id)
  triples as undo provenance — no migration needed;
- it NEVER creates/edits/deletes items, NEVER touches ``connector_state``
  (no cursor), NEVER indexes or enqueues enrichment, and NEVER overwrites a
  ledger row whose item is gone with ``fail_count == 0`` (the runner's
  USER-DELETED guard);
- it requires a Google fetch to learn external ids, so it runs only AFTER
  consent; ``dry_run=True`` does the same fetch and matching but writes
  NOTHING (no ledger rows, no ops event);
- ``undo_adopt`` removes exactly the rows the last real adopt wrote — and
  refuses any row a sync has since touched (version moved) or that lost its
  item (user deleted since adopt): those rows are load-bearing.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy.orm import Session

from brain.connectors.base import Connector
from brain.connectors.state import ConnectorRecordRepo, ConnectorStateRepo
from brain.services.dedupe import match_key, normalize_text

# connector name -> the legacy item source it adopts (data, not code).
# gdrive/gtasks have no legacy capture path: with zero candidates adopt
# reports "nothing to adopt" and never fetches.
LEGACY_SOURCES: dict[str, str] = {
    "gcal": "calendar",
    "email": "email",
    "gdrive": "gdrive",
    "gtasks": "gtasks",
}

# Rule C: sent-time tolerance (inclusive) between the forward header and the
# Gmail record — clocks drift; ±2 minutes was the approved window.
RULE_C_WINDOW = timedelta(minutes=2)

# Runaway guard for the fetch loop (same cap as `sync --drain`).
_MAX_ADOPT_BATCHES = 500

_RULE_ORDER = {"A": 0, "B": 1, "C": 2}


@dataclass
class AdoptReport:
    """What one adopt run did (or would do, under ``dry_run``)."""

    connector: str
    legacy_source: str
    refused_cursor: bool = False  # a cursor already exists -> refuse
    nothing_to_adopt: bool = False  # zero legacy candidates -> no fetch at all
    fetched: int = 0
    # ((external_id, version), item_id) in record order
    adopted: list[tuple[tuple[str, str], uuid.UUID]] = field(default_factory=list)
    already: list[tuple[str, Any]] = field(default_factory=list)  # (external_id, item_id)
    ambiguous: list[str] = field(default_factory=list)
    user_deleted: list[str] = field(default_factory=list)
    new: list[str] = field(default_factory=list)
    # external_id -> the rule that matched it ("A" | "B" | "C")
    rules: dict[str, str] = field(default_factory=dict)
    wrote: bool = False  # ledger rows + ops event actually written

    def summary_line(self) -> str:
        if self.refused_cursor:
            return (
                f"adopt {self.connector}: REFUSED — connector_state already holds a "
                "cursor (it already synced). Adoption applies before the first sync."
            )
        if self.nothing_to_adopt:
            return (
                f"adopt {self.connector}: no legacy {self.legacy_source} items — "
                "nothing to adopt (no fetch made)"
            )
        return (
            f"adopt {self.connector}: {self.fetched} record(s) fetched, "
            f"{len(self.adopted)} would adopt, {len(self.ambiguous)} ambiguous, "
            f"{len(self.new)} new on first sync, {len(self.user_deleted)} user-deleted, "
            f"{len(self.already)} already adopted"
        )


@dataclass
class UndoReport:
    """What one ``--undo`` run did."""

    connector: str
    removed: list[str] = field(default_factory=list)
    refused: list[str] = field(default_factory=list)

    def summary_line(self) -> str:
        lines = [f"undo adopt {self.connector}: removed {len(self.removed)} ledger row(s)"]
        if self.removed:
            lines.append(f"  removed: {', '.join(self.removed)}")
        if self.refused:
            lines.append(
                f"  REFUSED (a sync touched them, or the user deleted the item since): "
                f"{', '.join(self.refused)}"
            )
        return "\n".join(lines)


def adopt_connector(
    connector: Connector,
    session: Session,
    *,
    dry_run: bool = False,
    limit: int = 200,
) -> AdoptReport:
    """Match one connector's records against its legacy items; write ledger rows.

    Read-only under ``dry_run``. Raises the connector's own errors
    (:class:`ConnectorAuthError` / :class:`ConnectorRetryableError`) untouched —
    the CLI maps them to exit codes exactly like ``sync``.
    """
    name = connector.name
    legacy_source = LEGACY_SOURCES.get(name, name)
    report = AdoptReport(connector=name, legacy_source=legacy_source)

    state = ConnectorStateRepo(session).get(name)
    if state is not None and state.cursor:
        report.refused_cursor = True
        return report

    # Per-run progress set (finding-4 follow-up): during adopt the ledger
    # cannot know records fetched in EARLIER batches (a dry-run writes
    # nothing; a real adopt writes only after matching), so a truncated
    # query batch's re-list (next_cursor=None, more=True) would re-GET the
    # same first `limit` records forever. Connectors that opt in (Gmail:
    # run_seen) skip these ids without a GET; connectors with resumable
    # cursors never consult it.
    seen: set[str] = set()
    if hasattr(connector, "run_seen"):
        connector.run_seen = seen  # type: ignore[attr-defined]

    candidates = _legacy_items(session, legacy_source)
    if not candidates:
        report.nothing_to_adopt = True
        return report

    records = _fetch_all(connector, limit=limit, seen=seen)
    report.fetched = len(records)

    index = _CandidateIndex(candidates)
    candidate_ids = {item.id for item in candidates}

    records_repo = ConnectorRecordRepo(session)
    for record in records:
        row = records_repo.get(name, record.external_id)
        if row is not None and row.item_id is not None:
            # The ledger already tracks this record. Adopt never rewrites a
            # row that points at an item (only a sync writes versions).
            if row.item_id in candidate_ids:
                report.already.append((record.external_id, row.item_id))
            else:  # points at a different item: conflict, never overwrite
                report.ambiguous.append(record.external_id)
            continue
        if row is not None and row.item_id is None and row.fail_count == 0:
            # USER-DELETED (the runner's marker): deleting is a user decision.
            report.user_deleted.append(record.external_id)
            continue
        matches = index.match(record)
        if not matches:
            report.new.append(record.external_id)
            continue
        if len(matches) > 1:
            report.ambiguous.append(record.external_id)
            continue
        item_id, rules_hit = next(iter(matches.items()))
        rule_name = min(rules_hit, key=_RULE_ORDER.get)  # type: ignore[arg-type]
        report.rules[record.external_id] = rule_name
        report.adopted.append(((record.external_id, record.version), item_id))

    if report.adopted and not dry_run:
        try:
            # ONE transaction: ledger rows + baseline stamp + provenance
            # event commit together or not at all. undo reads ONLY the last
            # adopt event — a crash in between would strand ledger rows no
            # undo can ever find.
            prior_shas = _prior_shas(session, report)
            for (external_id, version), item_id in report.adopted:
                records_repo.put(name, external_id, version, item_id, commit=False)
            _stamp_baseline(session, report, commit=False)
            _log_adopt(session, name, report, prior_shas, commit=False)
            session.commit()
            report.wrote = True
        except Exception:
            session.rollback()
            raise
    return report


def undo_adopt(name: str, session: Session) -> UndoReport:
    """Remove exactly the ledger rows the last REAL adopt of ``name`` wrote.

    Also reverses the baseline stamp for every row actually removed (the
    plan's 'byte-identical after a clean undo'); refused rows keep it."""
    report = UndoReport(connector=name)
    event = _last_adopt_event(session, name)
    if event is None:
        return report  # nothing recorded: nothing to undo
    records_repo = ConnectorRecordRepo(session)
    try:
        for quad in event.detail.get("adopted", []):
            external_id, version, item_id = quad[0], quad[1], quad[2]
            prior_sha = quad[3] if len(quad) > 3 else None
            row = records_repo.get(name, external_id)
            if row is None:
                continue  # already gone (undone before, or removed out-of-band)
            if row.item_id is None:
                report.refused.append(external_id)  # user deleted the item since
                continue
            if row.version != (version or None) or str(row.item_id) != item_id:
                report.refused.append(external_id)  # a sync touched it: load-bearing
                continue
            records_repo.delete(name, external_id, commit=False)
            report.removed.append(external_id)
            if prior_sha is None:
                _strip_baseline(session, uuid.UUID(item_id))
            else:
                _restore_baseline(session, uuid.UUID(item_id), prior_sha)
        session.add(
            _ops_event(
                {
                    "connector": name,
                    "undo": True,
                    "removed": report.removed,
                    "refused": report.refused,
                }
            )
        )
        session.commit()
    except Exception:
        session.rollback()
        raise
    return report


# --- matching internals ---------------------------------------------------------


class _CandidateIndex:
    """Indexes the legacy items once, then matches records in memory."""

    def __init__(self, items: list[Any]) -> None:
        self.by_rfc: dict[str, list[Any]] = {}
        self.by_key: dict[tuple[str, str], list[Any]] = {}
        self.by_header: dict[tuple[str, str], list[tuple[Any, datetime | None]]] = {}
        for item in items:
            meta = item.meta if isinstance(item.meta, dict) else {}
            rfc = str(meta.get("rfc_message_id") or "").strip()
            if rfc:
                self.by_rfc.setdefault(rfc, []).append(item)
            self.by_key.setdefault(match_key(item.raw_text, item.occurred_at), []).append(item)
            headers = _legacy_headers(item)
            if headers is not None:
                subject, sender, sent = headers
                self.by_header.setdefault(
                    (normalize_text(subject), _sender_address(sender)), []
                ).append((item, sent))

    def match(self, record: Any) -> dict[uuid.UUID, set[str]]:
        """All candidate items this record matches, with the rules that hit.

        More than one candidate -> the caller reports AMBIGUOUS and adopts
        nothing. One candidate hit by several rules -> the caller keeps the
        strongest rule name for the report.
        """
        matches: dict[uuid.UUID, set[str]] = {}
        meta = record.meta if isinstance(record.meta, dict) else {}
        rfc = str(meta.get("rfc_message_id") or "").strip()
        if rfc:
            for item in self.by_rfc.get(rfc, ()):
                matches.setdefault(item.id, set()).add("A")
        for item in self.by_key.get(match_key(record.text, record.occurred_at), ()):
            matches.setdefault(item.id, set()).add("B")
        subject = str(meta.get("subject") or "").strip()
        sender = str(meta.get("from") or "").strip()
        if subject and sender and record.occurred_at is not None:
            key = (normalize_text(subject), _sender_address(sender))
            for item, sent in self.by_header.get(key, ()):
                if sent is None:
                    continue  # no parseable sent time: rule C cannot fire
                if abs(sent - record.occurred_at) <= RULE_C_WINDOW:
                    matches.setdefault(item.id, set()).add("C")
        return matches


def _legacy_items(session: Session, legacy_source: str) -> list[Any]:
    """All non-hidden legacy items of one source (hidden: superseded)."""
    from brain.db.models import MemoryItem

    return (
        session.query(MemoryItem)
        .filter(MemoryItem.source == legacy_source, MemoryItem.superseded_by.is_(None))
        .all()
    )


def _stamp_baseline(session: Session, report: AdoptReport, *, commit: bool = True) -> None:
    """Declare each adopted item's CURRENT text as the framework baseline.

    The runner's user-edited guard compares ``meta['connector_text_sha']``
    against the item's text; legacy items never carry the hash, so without
    this stamp ANY later source change would be misread as a user edit and
    the item would go stale (a moved event keeping its old time). Stamping
    says: as of adopt, this text is what the framework owns. Changes the
    user makes AFTER adopt keep full protection (hash mismatch); text edited
    before adopt is indistinguishable from the original capture and becomes
    the baseline — the accepted trade-off (a stale event is worse than a
    re-rendered one). Bookkeeping meta only: raw_text/occurred_at/source
    never move here. ``commit=False`` folds the stamp into the caller's
    one-transaction commit (undo reverses it, see undo_adopt).
    """
    from brain.connectors.runner import _text_sha
    from brain.db.models import MemoryItem

    for _, item_id in report.adopted:
        item = session.get(MemoryItem, item_id)
        if item is not None:
            item.meta = {**(item.meta or {}), "connector_text_sha": _text_sha(item.raw_text)}
    if commit:
        session.commit()


def _strip_baseline(session: Session, item_id: uuid.UUID) -> None:
    """Remove the baseline stamp undo wrote (the pre-adopt state had none)."""
    from brain.db.models import MemoryItem

    item = session.get(MemoryItem, item_id)
    if item is not None and isinstance(item.meta, dict):
        item.meta = {k: v for k, v in item.meta.items() if k != "connector_text_sha"}


def _restore_baseline(session: Session, item_id: uuid.UUID, prior_sha: str) -> None:
    """Restore a PRE-EXISTING stamp value (defensive: legacy items have none)."""
    from brain.db.models import MemoryItem

    item = session.get(MemoryItem, item_id)
    if item is not None:
        item.meta = {**(item.meta or {}), "connector_text_sha": prior_sha}


def _prior_shas(session: Session, report: AdoptReport) -> dict[uuid.UUID, str | None]:
    """Each adopted item's connector_text_sha BEFORE adopt (None expected).

    Recorded in the adopt event so undo can restore the exact prior state —
    byte-identical after a clean undo.
    """
    from brain.db.models import MemoryItem

    out: dict[uuid.UUID, str | None] = {}
    for _, item_id in report.adopted:
        item = session.get(MemoryItem, item_id)
        meta = item.meta if (item is not None and isinstance(item.meta, dict)) else {}
        out[item_id] = meta.get("connector_text_sha")
    return out


def _fetch_all(
    connector: Connector, *, limit: int, seen: set[str] | None = None
) -> list[Any]:
    """Drain the connector in memory (batch cap = the sync --drain guard).

    The cursor is NEVER persisted: adopt is outside the sync bookkeeping and
    the next real sync starts from its own state.

    ``more=True`` (the source truncated the batch without a resume cursor —
    the email query path) keeps the loop going even when next_cursor is
    None: the re-fetch re-lists from the same cursor and every collected id
    is added to ``seen`` — the connector consults it to skip already-
    collected records WITHOUT a GET, so each record is fetched exactly once
    and the loop terminates. Bounded by _MAX_ADOPT_BATCHES.
    """
    records: list[Any] = []
    cursor: str | None = None
    for _ in range(_MAX_ADOPT_BATCHES):
        result = connector.fetch(cursor, limit=limit)
        records.extend(result.records)
        if seen is not None:
            seen.update(record.external_id for record in result.records)
        if result.more:
            # Truncated: advance only when the source returned a new cursor;
            # otherwise keep the current one (email's stays None — the
            # re-fetch re-lists from the start and skips what it already saw).
            if result.next_cursor is not None:
                cursor = result.next_cursor
            continue
        nxt = result.next_cursor
        if nxt is None or nxt == cursor:
            break
        cursor = nxt
    return records


def _legacy_headers(item: Any) -> tuple[str, str, datetime | None] | None:
    """(subject, sender, sent time) for rule C, from ONE source of truth.

    Primary: the forward-header fields the paste parser already extracted into
    item.meta (``subject`` / ``from`` / ``date_iso`` — verified present on all
    live email items). Fallback when meta lacks any of them: re-parse the item
    text with the SAME parser (``brain.services.paste._parse_email``) — no
    second parser anywhere.
    """
    meta = item.meta if isinstance(item.meta, dict) else {}
    subject = str(meta.get("subject") or "").strip()
    sender = str(meta.get("from") or "").strip()
    sent = _parse_instant(str(meta.get("date_iso") or "").strip())
    if not (subject and sender and sent is not None):
        try:
            from zoneinfo import ZoneInfo  # noqa: PLC0415

            from brain.config import get_settings  # noqa: PLC0415
            from brain.services.paste import _parse_email  # noqa: PLC0415

            zone = ZoneInfo(get_settings().timezone)
            parsed = _parse_email(item.raw_text or "", zone=zone)
            extracted = parsed.extracted
            subject = subject or str(extracted.get("subject") or "").strip()
            sender = sender or str(extracted.get("from") or "").strip()
            if sent is None:
                sent = _parse_instant(str(extracted.get("date_iso") or "").strip())
        except Exception:  # noqa: BLE001 — fallback only; meta path already worked
            pass
    if not (subject and sender):
        return None
    return subject, sender, sent


def _parse_instant(value: str) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


def _sender_address(value: str) -> str:
    """The bare address of 'Name <addr>' (or the value itself), casefolded."""
    value = value.strip()
    if "<" in value and ">" in value:
        value = value[value.rfind("<") + 1 : value.rfind(">")]
    return value.strip().casefold()


# --- provenance -------------------------------------------------------------------


def _ops_event(detail: dict[str, Any]) -> Any:
    from brain.db.models import OpsEvent

    return OpsEvent(component="connectors", kind="adopt", ok=True, detail=detail)


def _log_adopt(
    session: Session,
    name: str,
    report: AdoptReport,
    prior_shas: dict[uuid.UUID, str | None],
    *,
    commit: bool = True,
) -> None:
    """Provenance: the exact (external_id, version, item_id, prior_sha)
    quadruples written, so undo can remove the rows AND reverse the stamp.
    Commits only when asked (the adopt path commits once at the end)."""
    session.add(
        _ops_event(
            {
                "connector": name,
                "dry_run": False,
                "adopted": [
                    [external_id, version or "", str(item_id), prior_shas.get(item_id)]
                    for (external_id, version), item_id in report.adopted
                ],
                "ambiguous": len(report.ambiguous),
                "new": len(report.new),
                "user_deleted": len(report.user_deleted),
                "already": len(report.already),
            }
        )
    )
    if commit:
        session.commit()


def _last_adopt_event(session: Session, name: str) -> Any:
    from brain.db.models import OpsEvent

    events = (
        session.query(OpsEvent)
        .filter(OpsEvent.component == "connectors", OpsEvent.kind == "adopt")
        .order_by(OpsEvent.ts.desc())
        .all()
    )
    for event in events:
        detail = event.detail if isinstance(event.detail, dict) else {}
        if detail.get("connector") == name and not detail.get("undo") and "adopted" in detail:
            return event
    return None


# --- user-facing rendering ---------------------------------------------------------


def format_report(report: AdoptReport) -> str:
    """The dry-run/real-run table the user reads (stable order, ids shortened)."""
    lines = [report.summary_line()]
    if report.refused_cursor or report.nothing_to_adopt:
        return "\n".join(lines)
    if report.adopted:
        verb = "ADOPTED" if report.wrote else "WOULD ADOPT"
        lines.append(f"  {verb} ({len(report.adopted)})")
        for (external_id, version), item_id in report.adopted:
            rule = report.rules.get(external_id, "?")
            lines.append(
                f"    {external_id} (v={version or '-'}) -> item {str(item_id)[:8]}… [{rule}]"
            )
    if report.already:
        lines.append(f"  ALREADY ADOPTED ({len(report.already)})")
        for external_id, item_id in report.already:
            lines.append(f"    {external_id} -> item {str(item_id)[:8]}…")
    if report.ambiguous:
        lines.append(f"  AMBIGUOUS ({len(report.ambiguous)}) — skipped, never guessed")
        for external_id in report.ambiguous:
            lines.append(f"    {external_id}")
    if report.user_deleted:
        lines.append(f"  USER-DELETED ({len(report.user_deleted)}) — respected, never repointed")
        for external_id in report.user_deleted:
            lines.append(f"    {external_id}")
    if report.new:
        lines.append(f"  NEW ON FIRST SYNC ({len(report.new)})")
        for external_id in report.new:
            lines.append(f"    {external_id}")
    return "\n".join(lines)
