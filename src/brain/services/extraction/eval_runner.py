"""Offline extraction-eval runner (PR A2): emails file -> pred.jsonl.

Runs the REAL extractor + the FULL A1 pipeline (grounding, deterministic
dates, applies_to) over a file of emails split by '=====' separators — never
against the live store (the same guard as `brain enrich`) — and writes pred
rows in the eval schema (docs/eval-extraction.md). The gold files are NEVER
read here; the extraction developer consumes aggregate numbers only.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any

from sqlalchemy import text

from brain.db.models import MemoryItem
from brain.services.extraction.service import EnrichmentService, build_extractor
from brain.services.memory import MemoryRepository, MemoryService

logger = logging.getLogger(__name__)

_SEPARATOR = "====="


def split_emails(raw: str) -> list[dict[str, str]]:
    """Split the dogfood file into {'email_id', 'sent', 'text'} dicts.

    email_id is 'E' + 2-digit position IN FILE ORDER (E01..); sent is the
    email's Date header (raw string, parsed by the caller's pipeline).
    """
    emails: list[dict[str, str]] = []
    for chunk in raw.split(_SEPARATOR):
        body = chunk.strip()
        if not body:
            continue
        sent_line = ""
        from_line = ""
        text_lines: list[str] = []
        for line in body.splitlines():
            if line.startswith("Date:"):
                sent_line = line[len("Date:") :].strip()
                continue
            if line.startswith("From:"):
                from_line = line[len("From:") :].strip()
                continue
            text_lines.append(line)
        emails.append(
            {
                "email_id": f"E{len(emails) + 1:02d}",
                "sent": sent_line,
                "sender": from_line,
                "text": "\n".join(text_lines).strip(),
            }
        )
    return emails


def _parse_sent(raw: str) -> datetime:
    """Email Date header -> aware UTC datetime ('Thu, 22 Oct 2026 13:43')."""
    parsed = parsedate_to_datetime(raw)
    if parsed.tzinfo is None:  # zone-less header: interpret as local via UTC
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


def _iso_local(moment: datetime | None, tz: Any) -> str | None:
    """Aware UTC -> local wall-clock ISO WITHOUT offset (eval schema)."""
    if moment is None:
        return None
    return moment.astimezone(tz).replace(tzinfo=None).isoformat()


def _child_rows(
    session: Any,  # noqa: ANN001 — Session
    source: MemoryItem,
    email_id: str,
    tz: Any,
) -> list[dict[str, Any]]:
    """One email's stored children -> eval pred rows (full pipeline output)."""
    links = session.execute(
        text(
            "SELECT child_id, quote FROM item_sources WHERE source_id = :sid "
            "ORDER BY created_at ASC"
        ),
        {"sid": str(source.id)},
    ).all()
    # v6 Lever A: ONE row per CHILD, not per link — a child carrying several
    # merged/grounded quote rows (item 7, many-to-many item_sources) must
    # still yield a single pred row (the schema requires a unique child_id
    # per file). The quote is the kept link's (first in creation order, i.e.
    # candidate order).
    first_quote: dict[Any, str] = {}
    child_order: list[Any] = []
    for child_id, quote in links:
        if child_id not in first_quote:
            first_quote[child_id] = quote
            child_order.append(child_id)
    rows: list[dict[str, Any]] = []
    for number, child_id in enumerate(child_order):
        quote = first_quote[child_id]
        child = session.get(MemoryItem, child_id)
        assert child is not None
        meta = child.extraction_meta or {}
        row: dict[str, Any] = {
            "email_id": email_id,
            "child_id": f"{email_id}-C{number + 1:02d}",
            "type": child.type.value if child.type else "event",
            "title": child.raw_text,
        }
        when = _iso_local(child.occurred_at, tz)
        if child.type is not None and child.type.value == "task":
            row["due"] = when
        else:
            row["start"] = when
        row["all_day"] = bool(child.occurred_at) and _is_midnight(child.occurred_at, tz)
        row["applies_to"] = list(child.applies_to or ["unknown"])
        row["importance"] = child.importance
        row["actionability"] = meta.get("actionability")
        row["source_quote"] = quote
        if meta.get("derived_from") is not None:
            # relation to the derived-from child, by pred child_id (item 22);
            # the extractor's index refers to candidate order == child order
            target_number = int(meta["derived_from"]) + 1
            row["relation"] = {
                "kind": "derived_from",
                "target": f"{email_id}-C{target_number:02d}",
            }
        row["date_conflict"] = bool(child.date_conflict)
        rows.append(row)
    return rows


def _is_midnight(moment: datetime, tz: Any) -> bool:
    return moment.astimezone(tz).time().isoformat() == "00:00:00"


def run_extraction_eval(
    input_path: Path,
    out_path: Path,
    session: Any,  # noqa: ANN001 — Session
    extractor: Any | None = None,  # noqa: ANN001 — Extractor; None = the ONE
    #   settings factory (build_extractor), same as every production site.
    *,
    tz: Any,
    memory_snapshot: Path | None = None,
) -> list[dict[str, Any]]:
    """Extract children for every email and write the pred file.

    Returns the rows (also written as JSONL). Runs against the session's DB
    (a TEST database — the CLI guard refuses the live store); cleans its own
    rows before and after.

    ``memory_snapshot``: a read-only export of the LIVE memory layer
    (entities + aliases + current confirmed facts, as produced by the
    export SQL) imported into the TEST database first. Without it the
    audience resolution has nothing to resolve against (run #1 lesson:
    109/109 'unknown' with an empty memory layer) and the R1 sender
    default has no teacher facts to match.
    """
    emails = split_emails(input_path.read_text(encoding="utf-8"))
    memory = MemoryService(MemoryRepository(session))
    if extractor is None:
        from brain.config import get_settings  # noqa: PLC0415 (cheap, local)

        extractor = build_extractor(get_settings())
    from brain.repositories.items import ItemsRepository  # noqa: PLC0415

    service = EnrichmentService(
        ItemsRepository(session), session, extractor=extractor, memory=memory
    )
    _wipe(session)
    if memory_snapshot is not None:
        _import_memory_snapshot(session, memory_snapshot)
    rows: list[dict[str, Any]] = []
    now = datetime.now(tz=UTC)
    for email in emails:
        source = MemoryItem(
            raw_text=email["text"],
            source="email",
            occurred_at=_parse_sent(email["sent"]) if email["sent"] else None,
            meta={
                "email_id": email["email_id"],
                "subject_kind": "school",
                "sender": email.get("sender", ""),
            },
        )
        session.add(source)
        session.commit()
        if service.enqueue(source) is None:
            logger.warning("extract-eval: %s did not qualify (D1b)", email["email_id"])
            continue
        service.run_once(now, limit=1)
        rows.extend(_child_rows(session, source, email["email_id"], tz))
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        "\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n",
        encoding="utf-8",
    )
    return rows


def _import_memory_snapshot(session: Any, snapshot_path: Path) -> None:  # noqa: ANN001
    """Import a read-only LIVE memory export into the TEST database (A2).

    Same ids and fact timestamps as live so teacher/channel matching
    behaves identically. The export contains ONLY the memory layer
    (entities/aliases/facts) — never captures or children.
    """
    snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
    for entity in snapshot.get("entities", []):
        session.execute(
            text(
                "INSERT INTO entities (id, kind, name, created_at) "
                "VALUES (:id, :kind, :name, :created_at) "
                "ON CONFLICT (id) DO NOTHING"
            ),
            entity,
        )
    for alias in snapshot.get("aliases", []):
        session.execute(
            text(
                "INSERT INTO entity_aliases (entity_id, alias) "
                "VALUES (:entity_id, :alias) ON CONFLICT DO NOTHING"
            ),
            alias,
        )
    for fact in snapshot.get("facts", []):
        fact = dict(fact)
        fact["provenance"] = json.dumps(fact.get("provenance") or {})
        session.execute(
            text(
                "INSERT INTO memory_facts (id, subject_id, predicate, object_id, "
                "value, valid_from, valid_to, status, provenance) "
                "VALUES (:id, :subject_id, :predicate, :object_id, :value, "
                ":valid_from, :valid_to, :status, "
                "CAST(:provenance AS jsonb)) ON CONFLICT (id) DO NOTHING"
            ),
            fact,
        )
    session.commit()


def _wipe(session: Any) -> None:  # noqa: ANN001
    session.execute(text("DELETE FROM enrichment_jobs"))
    session.execute(text("DELETE FROM item_sources"))
    session.execute(text("DELETE FROM memory_items"))
    session.commit()
