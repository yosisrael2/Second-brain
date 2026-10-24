"""Relations cards: the bot's Undo / Confirm / Keep-both / Restore surface.

Implements PR B (delta items 20a, 20b, D4) on top of the item_relations
model, reusing worker_6's proposal-card patterns (pending registry, <64B
callbacks, HTML escaping, pure render functions over a session):

- item 20a: a card for an item that APPLIED-superseded an older item shows
  "↩︎ replaced: <old> → <new>" with one ↩︎ Undo row per replaced item
  (undo un-strikes the old item, the row stays for audit with
  provenance.state='undone');
- item 20b: a PROPOSED cross-thread supersede renders "❓ maybe replaces"
  with ✅ Confirm replace / ✖ Keep both (confirm applies + strikes;
  keep-both deletes the proposal, nothing is struck);
- D4: a superseded item's card renders its text struck-through
  ("↪︎ replaced by: <new>") with a ♻️ Restore button.

PR D-recon-2a adds the recon side of the same surface: the verdict
relation of one recon child (any of the four recon kinds), the CONFLICT
both-versions block, the MORE_DETAIL 🔔 line, and the cancelled target's
♻️ Restore (cancelled_by_rel).

All card text is HTML-escaped here; callback data stays < 64 bytes because
relation ids ride the bot's pending registry (only the short token + an
index travel in the callback).
"""

from __future__ import annotations

from collections.abc import Sequence

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from brain.db.models import ItemRelation, MemoryItem

# Compact callback-data prefixes (kept < 64 bytes; ids live in the registry).
CB_REL_UNDO = "ru"  # ru:<token>:<index>  undo an applied supersede
CB_REL_CONFIRM = "rc"  # rc:<token>:<index>  confirm a proposed supersede
CB_REL_KEEP_BOTH = "rk"  # rk:<token>:<index>  reject a proposed supersede
CB_REL_RESTORE = "rs"  # rs:<token>          restore a superseded item
CB_RECON_DISCARD = "rd"  # rd:<token>:<index>  keep existing / not cancelled

# The four recon relation kinds (D-recon-1's verdict writes).
RECON_KINDS = ("supersedes", "conflicts", "cancels", "details")


def applied_supersedes_of(session: Session, item_id) -> list[ItemRelation]:
    """Applied supersedes relations FROM this item (it replaced these), oldest first."""
    return [
        rel
        for rel in session.scalars(
            select(ItemRelation)
            .where(
                ItemRelation.from_id == item_id,
                ItemRelation.kind == "supersedes",
            )
            .order_by(ItemRelation.created_at.asc(), ItemRelation.id.asc())
        )
        if (rel.provenance or {}).get("state") == "applied"
    ]


def proposed_supersedes_of(session: Session, item_id) -> list[ItemRelation]:
    """Proposed supersedes relations FROM this item (pending cross-thread matches)."""
    return [
        rel
        for rel in session.scalars(
            select(ItemRelation)
            .where(
                ItemRelation.from_id == item_id,
                ItemRelation.kind == "supersedes",
            )
            .order_by(ItemRelation.created_at.asc(), ItemRelation.id.asc())
        )
        if (rel.provenance or {}).get("state") == "proposed"
    ]


def superseded_by_rel(session: Session, item_id) -> ItemRelation | None:
    """The applied supersedes relation that struck THIS item (D4), if any."""
    for rel in session.scalars(
        select(ItemRelation)
        .where(ItemRelation.to_id == item_id, ItemRelation.kind == "supersedes")
        .order_by(ItemRelation.created_at.desc(), ItemRelation.id.desc())
    ):
        if (rel.provenance or {}).get("state") == "applied":
            return rel
    return None


def _summary(session: Session, item_id) -> str:
    item = session.get(MemoryItem, item_id)
    flat = " ".join((item.raw_text if item else str(item_id)).split())
    if len(flat) > 60:
        flat = flat[:60].rstrip() + "…"
    return flat


def _esc(value: str) -> str:
    import html  # noqa: PLC0415

    return html.escape(str(value), quote=False)


def one_line_escaped(session: Session, item_id) -> str:
    return _esc(_summary(session, item_id))


def summary_of(session: Session, item_id) -> str:
    """Raw (unescaped) one-line summary of one item — for callers that
    escape later (verdict markers render through child_line's _esc)."""
    return _summary(session, item_id)


def correction_notice(session: Session, item_id) -> str | None:
    """'↩︎ replaced: old → new' / '❓ maybe replaces' for the card of item_id.

    None when the item neither applied-superseded nor proposed-supersedes
    anything. All summaries are HTML-escaped one-liners.
    """
    applied = applied_supersedes_of(session, item_id)
    proposed = proposed_supersedes_of(session, item_id)
    if not applied and not proposed:
        return None
    new_summary = one_line_escaped(session, item_id)
    lines: list[str] = []
    for rel in applied:
        lines.append(f"↩︎ replaced: {one_line_escaped(session, rel.to_id)} → {new_summary}")
    for rel in proposed:
        lines.append(
            f"❓ maybe replaces: {one_line_escaped(session, rel.to_id)} — confirm or keep both"
        )
    return "\n".join(lines)


def replaced_by_notice(session: Session, item_id) -> str | None:
    """'↪︎ replaced by: <new>' for a superseded item's card (D4), or None."""
    rel = superseded_by_rel(session, item_id)
    if rel is None:
        return None
    return f"↪︎ replaced by: {one_line_escaped(session, rel.from_id)}"


def relations_keyboard(
    token: str,
    *,
    applied: Sequence[ItemRelation],
    proposed: Sequence[ItemRelation],
) -> list[list[tuple[str, str]]]:
    """One ↩︎ Undo row per applied relation; ✅/✖ row per proposed one.

    ``index`` positions into the pending ids AFTER the item id, parked as
    (item_id, *applied_ids, *proposed_ids). The proposed rows' offset is
    computed here from the same lists the caller parks, so the keyboard
    cannot drift out of sync with the registry.
    """
    rows: list[list[tuple[str, str]]] = [
        [("↩︎ Undo", f"{CB_REL_UNDO}:{token}:{index}")] for index in range(len(applied))
    ]
    offset = len(applied)
    rows += [
        [
            ("✅ Confirm replace", f"{CB_REL_CONFIRM}:{token}:{offset + index}"),
            ("✖ Keep both", f"{CB_REL_KEEP_BOTH}:{token}:{offset + index}"),
        ]
        for index in range(len(proposed))
    ]
    return rows


def strike(text: str) -> str:
    """HTML struck-through rendering for a superseded item's text (D4)."""
    return f"<s>{_esc(text)}</s>"


# --- recon verdict helpers (PR D-recon-2a) -----------------------------------


def recon_relation_of(session: Session, child_id) -> ItemRelation | None:
    """The verdict relation parked for one recon child (latest first).

    A recon child carries exactly one relation written by
    ReconciliationService.write() — supersedes / conflicts / cancels /
    details with provenance.how='recon'. None for a non-recon child or
    when the proposal was already discarded (keep-both rows disappear).
    """
    for rel in session.scalars(
        select(ItemRelation)
        .where(ItemRelation.from_id == child_id)
        .where(ItemRelation.kind.in_(RECON_KINDS))
        .order_by(ItemRelation.created_at.desc(), ItemRelation.id.desc())
    ):
        if (rel.provenance or {}).get("how") == "recon":
            return rel
    return None


def conflict_notice(session: Session, rel: ItemRelation) -> str:
    """The CONFLICT card's block: which is right? + both versions escaped."""
    return (
        "which is right?\n"
        f"  existing: {one_line_escaped(session, rel.to_id)}\n"
        f"  new: {one_line_escaped(session, rel.from_id)}"
    )


def details_notice(session: Session, item_id) -> str | None:
    """The MORE_DETAIL 🔔 line for an item's card: every recon details
    relation touching it (either direction) as one escaped line, or None.
    Never adds buttons — the detail itself is the other side's text."""
    lines: list[str] = []
    for rel in session.scalars(
        select(ItemRelation)
        .where(ItemRelation.kind == "details")
        .where(or_(ItemRelation.from_id == item_id, ItemRelation.to_id == item_id))
        .order_by(ItemRelation.created_at.asc(), ItemRelation.id.asc())
    ):
        if (rel.provenance or {}).get("how") != "recon":
            continue
        other = rel.to_id if rel.from_id == item_id else rel.from_id
        lines.append(f"🔔 more detail captured: {one_line_escaped(session, other)}")
    if not lines:
        return None
    return "\n".join(lines)


def cancelled_by_rel(session: Session, item_id) -> ItemRelation | None:
    """The applied cancels relation that struck THIS item (its ♻️ Restore)."""
    for rel in session.scalars(
        select(ItemRelation)
        .where(ItemRelation.to_id == item_id, ItemRelation.kind == "cancels")
        .order_by(ItemRelation.created_at.desc(), ItemRelation.id.desc())
    ):
        if (rel.provenance or {}).get("state") == "applied":
            return rel
    return None


def resolved_conflict_by_rel(session: Session, item_id) -> ItemRelation | None:
    """The applied conflicts relation that struck THIS item keep-new
    (D-recon-2a; the loser's ♻️ Restore)."""
    for rel in session.scalars(
        select(ItemRelation)
        .where(ItemRelation.to_id == item_id, ItemRelation.kind == "conflicts")
        .order_by(ItemRelation.created_at.desc(), ItemRelation.id.desc())
    ):
        if (rel.provenance or {}).get("state") == "applied":
            return rel
    return None
