"""Proposal cards: the bot's Keep / Fix / Drop surface for extracted children.

Implements delta items 19/20/D2/D4/D7 (PR C) on top of the A1 model:

- a source whose enrichment run created/updated children gets ONE card:
  "🧠 found N items in <source short title>", one line per proposed child
  (kind emoji, title, resolved date in the configured local zone,
  applies_to, "⚠️" when the date is conflicted) with per-child
  ✅ Keep / ✏️ Fix / 🗑 Drop buttons and one "Keep all";
- actions set ``child_state`` (kept / dropped); dropped children are
  remembered by the A1 re-run diff and never reappear (item 30);
- Keep makes a child list in /recent and /review (item 19: proposed
  children are unlisted there — the A1 listing rule already implements
  the hiding; this module only flips the state);
- Fix re-validates title + date through the SAME deterministic date rules
  as the A1 pipeline (weekday↔date consistency included) and marks the
  child user-edited — the pipeline never overwrites a user-acted child
  (item 18);
- the 48h ping (D7): proposed tasks due within 48 hours ping ONCE with
  ✅ Keep / 🗑 Drop on the ping itself; kept/dropped/far-off/dateless
  children never ping;
- D4 hook: a superseded child (extraction_meta["superseded_by"], PR B
  will create those) renders struck-through with a restore marker —
  no relation logic here, only the rendering hook.

Kind emoji map by the STORED child type (the spec's mapping: 📅 event,
✔️ task, 🔔 reminder, ℹ️ fact; legacy types keep sensible fallbacks).

All card text is HTML-escaped here; callback data stays < 64 bytes because
child ids ride the bot's pending registry (only the short token + an index
travel in the callback).
"""

from __future__ import annotations

import html
import logging
import re
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from brain import i18n
from brain.db.models import MemoryItem
from brain.services.extraction.dates import combine_local, extract_clock, resolve_when

logger = logging.getLogger(__name__)

# The spec's mapping by the STORED child type (A1 round 1 added
# 'reminder' and 'fact' to ItemType); legacy types keep fallbacks.
CHILD_EMOJI = {
    "event": "📅",
    "task": "✔️",
    "reminder": "🔔",
    "fact": "ℹ️",
    "note": "ℹ️",
    "reference": "ℹ️",
    "decision": "🧭",
    "idea": "💡",
}

# Callback prefixes (compact; ids live in the bot's PendingRegistry).
CB_PROPOSED_KEEP = "pk"  # keep one child:  pk:<token>:<index>
CB_PROPOSED_FIX = "pf"  # fix one child:   pf:<token>:<index>
CB_PROPOSED_DROP = "px"  # drop one child:  px:<token>:<index>
CB_PROPOSED_KEEP_ALL = "pa"  # keep all:        pa:<token>

_PING_WINDOW = timedelta(hours=48)


class FixError(ValueError):
    """A fix the user typed failed validation (bad title or unresolvable date).

    ``code`` names the i18n message (``fixerr_<code>``) the bot shows."""

    def __init__(self, message: str, code: str = "bad_date") -> None:
        super().__init__(message)
        self.code = code


def _esc(value: str) -> str:
    return html.escape(value, quote=False)


# meta.channel names the TRANSPORT for bot/email/manual captures ("telegram")
# but the conversation for WhatsApp ("<group name>"): only the latter is a title.
_TRANSPORT_CHANNELS = frozenset(
    "telegram email gmail imap manual api cli web cockpit whatsapp calendar ics paste".split()
)
_TITLE_MAX = 60
_LINE_PREFIX = re.compile(r"^[\s>*#•·\-–—=_~|]+")
_HAS_WORD = re.compile(r"[^\W_]")


def _trim(value: str) -> str:
    value = " ".join(value.split())
    if len(value) <= _TITLE_MAX:
        return value
    return value[: _TITLE_MAX - 1].rstrip() + "…"


def _first_meaningful_line(text: str) -> str | None:
    """The first line with a letter or digit, list/quote markers stripped."""
    for raw in text.splitlines():
        line = _LINE_PREFIX.sub("", raw).strip()
        if _HAS_WORD.search(line):
            return line
    return None


def source_short_title(source: MemoryItem) -> str:
    """A short human title for the card header: subject > conversation name
    (a WhatsApp group; never a transport like "telegram") > title > first
    meaningful line > sender."""
    meta = source.meta or {}

    def _meta(key: str) -> str | None:
        value = meta.get(key)
        return value.strip() if isinstance(value, str) and value.strip() else None

    channel = _meta("channel")
    if channel is not None and channel.lower() in _TRANSPORT_CHANNELS:
        channel = None
    candidates = (
        _meta("subject"),
        channel,
        _meta("title"),
        _first_meaningful_line(source.raw_text or ""),
        _meta("sender"),
        _meta("from"),
    )
    for value in candidates:
        if value:
            return _trim(value)
    return "capture"


def _local(child: MemoryItem, tz: ZoneInfo) -> datetime | None:
    if child.occurred_at is None:
        return None
    moment = child.occurred_at
    if moment.tzinfo is None:
        return moment.replace(tzinfo=tz)
    return moment.astimezone(tz)


def _date_part(moment: datetime, lang: str = i18n.DEFAULT) -> str:
    """'Mon 02.11' for an all-day child (local midnight), 'Mon 02.11 13:35'
    for a timed one — the card must not drop a stated pickup time. Hebrew:
    'יום ב׳ 02.11'."""
    return i18n.short_date(moment, lang)


# Types whose card line must say "no date" when none was resolved: for
# them a missing date is information. A reminder is often a standing rule
# ("bring a reading book every day") and a fact has no date at all.
_DATED_TYPES = frozenset({"event", "task"})


def _who(applies_to: list[str] | None, lang: str, names: dict[str, str]) -> str | None:
    """The 'for whom' part: display names (e.g. Hebrew aliases), or None."""
    values = [v for v in (applies_to or []) if v]
    if not values:
        return None
    if values == ["unknown"]:
        return i18n.t(lang, "who_unknown")
    if values == ["nobody"]:
        return i18n.t(lang, "who_nobody")
    special = {"me": i18n.t(lang, "who_me"), "family": i18n.t(lang, "who_family")}
    shown = [special.get(v) or names.get(v) or v for v in values if v not in ("unknown", "nobody")]
    return _esc(", ".join(shown)) if shown else None


def child_line(
    child: MemoryItem,
    tz_name: str,
    *,
    state: str | None = None,
    matched_title: str | None = None,
    lang: str = i18n.DEFAULT,
    names: dict[str, str] | None = None,
    number: int | None = None,
) -> str:
    """One card line for one child (HTML-escaped).

    ``state``/``matched_title`` come from the child's recon relation (the
    card builders look it up): an applied UPDATE renders "↩︎ applied:
    …" and a cancellation names its target. ``number`` prefixes the line
    so it matches its numbered buttons; ``names`` maps stored applies_to
    names to display names (the Hebrew alias when the bot speaks Hebrew)."""
    tz = ZoneInfo(tz_name)
    kind = child.type.value if child.type else ""
    emoji = CHILD_EMOJI.get(kind, "•")
    warning = "⚠️ " if child.date_conflict else ""
    prefix = f"{number}. " if number is not None else ""
    parts = [f"{prefix}{warning}{emoji} {i18n.iso(_esc(child.raw_text), lang)}"]
    moment = _local(child, tz)
    if moment is not None:
        parts.append(_date_part(moment, lang))
    elif kind in _DATED_TYPES:
        parts.append(i18n.t(lang, "no_date"))
    who = _who(child.applies_to, lang, names or {})
    if who:
        parts.append(who)
    marker = superseded_marker(child, lang)
    if marker:
        parts.append(marker)
    recon = verdict_marker(child, state=state, matched_title=matched_title, lang=lang)
    if recon:
        parts.append(_esc(recon))
    return " · ".join(parts)


def superseded_marker(child: MemoryItem, lang: str = i18n.DEFAULT) -> str:
    """D4 rendering hook: non-empty when the child was superseded (PR B)."""
    meta = child.extraction_meta or {}
    if meta.get("superseded_by"):
        return i18n.t(lang, "superseded")
    return ""


def verdict_marker(
    child: MemoryItem,
    *,
    state: str | None = None,
    matched_title: str | None = None,
    lang: str = i18n.DEFAULT,
) -> str:
    """PR D-recon (item 37) rendering hook: one short line per non-NEW
    verdict stored on the child. ``state`` is the recon relation's
    provenance.state (an applied UPDATE says "applied", not "proposes");
    ``matched_title`` names a cancellation's target."""
    meta = child.extraction_meta or {}
    verdict = meta.get("recon_verdict")
    if not verdict:
        return ""
    fields = meta.get("recon_fields") or {}
    if verdict == "update":
        changes = "; ".join(
            i18n.t(lang, "v_change", field=i18n.label(lang, f"vf_{k}", k), old=v[0], new=v[1])
            for k, v in fields.items()
        )
        if state == "applied":
            return i18n.t(lang, "v_update_applied", changes=changes)
        return i18n.t(lang, "v_update", changes=changes)
    if verdict == "conflict":
        return i18n.t(lang, "v_conflict")
    if verdict == "cancellation":
        if state == "applied":
            return i18n.t(lang, "v_cancelled")
        if matched_title:
            return i18n.t(lang, "v_cancels", title=i18n.iso(matched_title, lang))
        return i18n.t(lang, "v_proposes_cancel")
    if verdict == "more_detail":
        return i18n.t(lang, "v_more_detail")
    return ""


def render_source_card(
    source: MemoryItem,
    children: list[MemoryItem],
    tz_name: str,
    *,
    recon: dict[int, tuple[str, object, str | None]] | None = None,
    lang: str = i18n.DEFAULT,
    names: dict[str, str] | None = None,
) -> str:
    """The whole card text for one source's proposed children (HTML-escaped).

    ``recon`` (D-recon-2a) maps a child's index to (verdict, relation,
    matched_title): verdict children render their state-aware marker line
    (applied UPDATE, named cancellation target)."""
    recon = recon or {}
    lines = []
    for index, child in enumerate(children):
        entry = recon.get(index)
        if entry is None:
            lines.append(child_line(child, tz_name, lang=lang, names=names, number=index + 1))
            continue
        verdict, rel, matched_title = entry
        state = (rel.provenance or {}).get("state")
        lines.append(
            child_line(
                child,
                tz_name,
                state=state,
                matched_title=matched_title if verdict == "cancellation" else None,
                lang=lang,
                names=names,
                number=index + 1,
            )
        )
    title = _esc(source_short_title(source))
    if len(children) == 1:
        header = i18n.t(lang, "card_header_one", title=title)
    else:
        header = i18n.t(lang, "card_header", n=len(children), title=title)
    return "\n".join([header, i18n.t(lang, "card_legend"), *lines])


def proposal_keyboard(
    token: str,
    children: list[MemoryItem],
    *,
    recon: dict[int, tuple[str, object, str | None]] | None = None,
    lang: str = i18n.DEFAULT,
) -> list[list[tuple[str, str]]]:
    """Per-child ✅/✏️/🗑 rows + one 'Keep all' row (callback data < 64 bytes).

    ``recon`` (D-recon-2a) maps a child's index to (verdict, relation,
    matched_title): the verdict owns that child's row — UPDATE proposes
    ✅ Confirm update / ✖ Keep both (or ↩︎ Undo once auto-applied), CONFLICT
    asks ✅ keep existing / ✅ keep new, CANCELLATION offers ✅ Confirm
    cancellation / ✖ Not cancelled. The verdict rows carry the RELATION ID
    (never a position), so a mixed card cannot misalign plain and verdict
    rows; callback data stays < 64 bytes (prefix + token + uuid). MORE_DETAIL
    children keep the default row (the 🔔 line lives on the matched item's
    card, with no buttons)."""
    from brain.services.relations_view import (  # noqa: PLC0415
        CB_RECON_DISCARD,
        CB_REL_CONFIRM,
        CB_REL_KEEP_BOTH,
        CB_REL_UNDO,
    )

    recon = recon or {}
    rows: list[list[tuple[str, str]]] = []
    for index, _child in enumerate(children):
        n = index + 1  # the line number on the card
        entry = recon.get(index)
        if entry is not None:
            verdict, rel, _matched_title = entry
            state = (rel.provenance or {}).get("state")
            if verdict == "update" and state == "applied":
                rows.append([(i18n.t(lang, "btn_undo", n=n), f"{CB_REL_UNDO}:{token}:{rel.id}")])
                continue
            if verdict == "update":
                rows.append([
                    (i18n.t(lang, "btn_confirm_update", n=n), f"{CB_REL_CONFIRM}:{token}:{rel.id}"),
                    (i18n.t(lang, "btn_keep_both", n=n), f"{CB_REL_KEEP_BOTH}:{token}:{rel.id}"),
                ])
                continue
            if verdict == "conflict":
                discard = f"{CB_RECON_DISCARD}:{token}:{rel.id}"
                confirm = f"{CB_REL_CONFIRM}:{token}:{rel.id}"
                rows.append([
                    (i18n.t(lang, "btn_keep_existing", n=n), discard),
                    (i18n.t(lang, "btn_keep_new", n=n), confirm),
                ])
                continue
            if verdict == "cancellation":
                confirm = f"{CB_REL_CONFIRM}:{token}:{rel.id}"
                discard = f"{CB_RECON_DISCARD}:{token}:{rel.id}"
                rows.append([
                    (i18n.t(lang, "btn_confirm_cancel", n=n), confirm),
                    (i18n.t(lang, "btn_not_cancelled", n=n), discard),
                ])
                continue
        rows.append(
            [
                (i18n.t(lang, "btn_keep", n=n), f"{CB_PROPOSED_KEEP}:{token}:{index}"),
                (i18n.t(lang, "btn_fix", n=n), f"{CB_PROPOSED_FIX}:{token}:{index}"),
                (i18n.t(lang, "btn_drop", n=n), f"{CB_PROPOSED_DROP}:{token}:{index}"),
            ]
        )
    rows.append([(i18n.t(lang, "btn_keep_all"), f"{CB_PROPOSED_KEEP_ALL}:{token}")])
    return rows


# --- state transitions ------------------------------------------------------------


def apply_keep(repo, child: MemoryItem) -> None:
    """✅ Keep: the child is approved (lists in /recent + /review from now on)."""
    child.child_state = "kept"
    repo.commit()


def apply_drop(repo, child: MemoryItem) -> None:
    """🗑 Drop: remembered rejection — the A1 re-run diff never re-proposes it."""
    child.child_state = "dropped"
    meta = dict(child.extraction_meta or {})
    meta["dropped_at"] = datetime.now(tz=UTC).isoformat()
    child.extraction_meta = meta
    repo.commit()


def apply_keep_all(repo, children: list[MemoryItem]) -> int:
    """✅ Keep all on a source card: every still-proposed child becomes kept."""
    changed = 0
    for child in children:
        if child.child_state == "proposed":
            child.child_state = "kept"
            changed += 1
    if changed:
        repo.commit()
    return changed


def apply_fix(
    repo,
    child: MemoryItem,
    *,
    title: str,
    when_raw: str,
    source: MemoryItem | None,
    tz_name: str,
    indexer=None,
) -> MemoryItem:
    """✏️ Fix: minimal edit (title + date) re-validated by the A1 date rules.

    The fixed child counts as user-acted (item 18): ``child_state=kept`` +
    ``extraction_meta["user_edited"]=True``, so a re-run never overwrites it.
    A weekday that contradicts its own date keeps the child ``date_conflict``
    with both readings (item 2b) — never silently "fixed".

    ``indexer`` (optional): the Fix changes ``raw_text`` (the title), so the
    child's vector is re-embedded after the commit — a stale vector would
    miss the corrected title in search. Indexing failure is logged and never
    fails the Fix (the same policy as capture).
    """
    if not title or not title.strip():
        raise FixError("the title cannot be empty", "empty_title")
    title = title.strip()
    if len(title) > 200:
        raise FixError("the title is too long (max 200 characters)", "long_title")
    tz = ZoneInfo(tz_name)
    moment = None
    conflict = False
    note = None
    if when_raw.strip():
        if source is None:
            raise FixError("no source to resolve the date against", "no_source")
        sent_local = _source_sent_local(source, tz)
        resolved = resolve_when(when_raw.strip(), sent_local, tz)
        if resolved is None:
            raise FixError(
                "could not read that date — try dd.mm (e.g. 29.10) or 'יום חמישי 29.10'",
                "bad_date",
            )
        clock = extract_clock(when_raw)
        moment = combine_local(resolved.day, clock, tz)
        conflict = resolved.date_conflict
        note = resolved.conflict_note
        if moment is not None:
            child.occurred_at = moment
    child.raw_text = title
    child.date_conflict = conflict or None
    meta = dict(child.extraction_meta or {})
    meta["user_edited"] = True
    if when_raw.strip():
        meta["when_raw"] = when_raw.strip()
    if note:
        meta["date_conflict_note"] = note
    child.extraction_meta = meta
    child.child_state = "kept"
    from brain.db.enums import ItemStatus  # noqa: PLC0415

    child.status = ItemStatus.NEEDS_REVIEW if conflict else child.status
    repo.commit()
    if indexer is not None:
        try:
            indexer.index(child)
        except Exception as exc:  # noqa: BLE001 — same fallback policy as ingest
            logger.warning(
                "proposals: re-indexing fixed child %s failed (%s); deferred to 'brain reindex'",
                child.id, exc,
            )
    return child


def _source_sent_local(source: MemoryItem, tz: ZoneInfo) -> datetime:
    moment = source.occurred_at or source.created_at
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=tz)
    return moment.astimezone(tz)


# --- the 48h ping (D7) ---------------------------------------------------------------

# D7: tasks AND reminders due within the window (spec); legacy children
# of other types never ping.
_PING_TYPES = ("task", "reminder")


def due_proposed_children(
    session: Session, now: datetime, tz_name: str, *, within: timedelta = _PING_WINDOW
) -> list[MemoryItem]:
    """Proposed task children due within the window, not pinged yet (D7)."""
    stmt = (
        select(MemoryItem)
        .where(MemoryItem.child_state == "proposed")
        .where(MemoryItem.type.in_(_PING_TYPES))
        .where(MemoryItem.occurred_at.is_not(None))
        .order_by(MemoryItem.occurred_at.asc())
    )
    tz = ZoneInfo(tz_name)
    due: list[MemoryItem] = []
    for child in session.scalars(stmt):
        moment = child.occurred_at
        if moment is None:
            continue
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=tz)
        if now < moment <= now + within and not (child.extraction_meta or {}).get("pinged_at"):
            due.append(child)
    return due


_QUIET_PING_AFTER = timedelta(hours=24)


def ping_is_quiet(child: MemoryItem, now: datetime, tz_name: str) -> bool:
    """A 48h ping whose item is still more than a day away goes out silently
    (no sound/vibration): it is a heads-up, not an alarm."""
    moment = child.occurred_at
    if moment is None:
        return False
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=ZoneInfo(tz_name))
    return moment - now > _QUIET_PING_AFTER


def mark_pinged(children: list[MemoryItem], now: datetime) -> None:
    """One ping per child, never repeated (extraction_meta.pinged_at)."""
    for child in children:
        meta = dict(child.extraction_meta or {})
        meta["pinged_at"] = now.isoformat()
        child.extraction_meta = meta


def ping_line(
    child: MemoryItem,
    tz_name: str,
    *,
    lang: str = i18n.DEFAULT,
    names: dict[str, str] | None = None,
) -> str:
    """The 48h ping text (item 19/D7: it doubles as the confirmation moment)."""
    return i18n.t(lang, "due_soon", line=child_line(child, tz_name, lang=lang, names=names))


def proposed_children_of(session: Session, source_id) -> list[MemoryItem]:
    """The still-proposed children linked to one source (the card's lines)."""
    from brain.db.models import ItemSource  # noqa: PLC0415

    stmt = (
        select(MemoryItem)
        .join(ItemSource, ItemSource.child_id == MemoryItem.id)
        .where(ItemSource.source_id == source_id)
        .where(MemoryItem.child_state == "proposed")
        .order_by(MemoryItem.created_at.asc())
    )
    return list(session.scalars(stmt))
