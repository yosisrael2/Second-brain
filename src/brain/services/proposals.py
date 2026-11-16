"""Proposal cards: the bot's Keep / Fix / Drop surface for extracted children.

Implements delta items 19/20/D2/D4/D7 (PR C) on top of the A1 model:

- a source whose enrichment run created/updated children gets ONE card:
  "🧠 found N items in <source short title>", one line per proposed child
  (kind emoji, title, resolved date in the configured local zone,
  applies_to, "⚠️" when the date is conflicted). The card is a list +
  drill-down in ONE message, edited in place: the list has one button per
  undecided item (paged above LIST_PAGING_ABOVE) and one "Keep all N"; a
  tap opens that item's focus view (✅ Keep / ✏️ Fix / 🗑 Drop, or its
  recon verdict buttons, then ↩︎ Back). Decided lines stay with a
  ✅ / ✏️ / 🗑 prefix; once nothing is left the card collapses to a
  one-line summary;
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

All card text is HTML-escaped here; callback data stays < 64 bytes: a
button carries a two-letter prefix + the uuid it acts on (+ a page number),
and the bot re-validates every tap against the DB.
"""

from __future__ import annotations

import html
import logging
import re
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from brain import i18n
from brain.db.enums import Actionability
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

# Callback prefixes. Proposal cards and due pings are read hours or days
# later and the bot restarts on every deploy, so their buttons carry the id
# they act on (no in-memory token): prefix + ":" + canonical uuid = 39 bytes,
# well under Telegram's 64. The bot re-validates every tap against the DB.
CB_PROPOSED_KEEP = "pk"  # keep one child:  pk:<child-uuid>[:p]
CB_PROPOSED_FIX = "pf"  # fix one child:   pf:<child-uuid>
CB_PROPOSED_DROP = "px"  # drop one child:  px:<child-uuid>[:p]
CB_PROPOSED_KEEP_ALL = "pa"  # keep all:        pa:<source-uuid>
CB_PING_FLAG = "p"  # a due ping's button: answer one line, never the whole card
# The list + drill-down card (one message, edited in place): the list shows
# one button per undecided item; a tap opens that item's focus view.
CB_PROPOSED_OPEN = "po"  # open one child's focus view: po:<child-uuid>
CB_PROPOSED_PAGE = "pg"  # a list page:                 pg:<source-uuid>:<page>
CB_PROPOSED_BACK = "pb"  # focus -> back to the list:   pb:<source-uuid>[:<page>]
CB_PROPOSED_NOOP = "pn"  # the page indicator: answers the tap, changes nothing

LIST_PAGE_SIZE = 6  # item buttons per page once the list pages
LIST_PAGING_ABOVE = 8  # up to this many undecided items fit one page
BUTTON_TITLE_MAX = 24  # an item button's title part (ellipsis beyond)
_EXCERPT_MAX = 160  # the focus view's source excerpt
# Card text budget (characters of the HTML we send): the adapter hard-cuts
# replies at 4000, which could land mid-tag; a card stays under this by
# dropping trailing lines ("… +k more"), never by cutting inside one.
CARD_TEXT_MAX = 3800
_FYI_SHARE = CARD_TEXT_MAX // 4  # the collapsed FYI block's own budget
DECIDED_STATES = ("kept", "dropped")

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

# Pure information: nothing to schedule or do unless the child says so.
_INFO_TYPES = frozenset({"fact", "note", "reference"})


def is_fyi(child: MemoryItem, *, has_verdict: bool = False) -> bool:
    """A card line the family needs no decision on: not for us
    (applies_to 'nobody'), or pure information with no date and no action.
    Such lines render collapsed, without buttons. A child carrying a recon
    verdict never is (its row asks a question)."""
    if has_verdict:
        return False
    if (child.applies_to or []) == ["nobody"]:
        return True
    kind = child.type.value if child.type else ""
    no_action = child.actionability in (None, Actionability.NONE)
    return kind in _INFO_TYPES and child.occurred_at is None and no_action


def card_numbers(
    children: list[MemoryItem],
    recon: dict[int, tuple[str, object, str | None]] | None = None,
) -> list[int | None]:
    """The card line number of each child (1, 2, … over the actionable ones;
    None for an FYI line). Shared by the card text and its keyboard so a
    button's number always matches its line."""
    recon = recon or {}
    numbers: list[int | None] = []
    n = 0
    for index, child in enumerate(children):
        # a decided verdict child keeps its number even after its relation
        # is gone (✖ Keep both deletes it): the numbers never shift
        decided_verdict = child.child_state in DECIDED_STATES and bool(
            (child.extraction_meta or {}).get("recon_verdict")
        )
        if is_fyi(child, has_verdict=index in recon or decided_verdict):
            numbers.append(None)
        else:
            n += 1
            numbers.append(n)
    return numbers


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


def _detail_parts(
    child: MemoryItem, tz_name: str, lang: str, names: dict[str, str] | None
) -> list[str]:
    """The date (or an explicit "no date") and the for-whom part of a line."""
    kind = child.type.value if child.type else ""
    parts: list[str] = []
    moment = _local(child, ZoneInfo(tz_name))
    if moment is not None:
        parts.append(_date_part(moment, lang))
    elif kind in _DATED_TYPES:
        parts.append(i18n.t(lang, "no_date"))
    who = _who(child.applies_to, lang, names or {})
    if who:
        parts.append(who)
    return parts


def _marker_parts(
    child: MemoryItem,
    *,
    state: str | None,
    matched_title: str | None,
    lang: str,
    verdict: bool,
) -> list[str]:
    parts: list[str] = []
    marker = superseded_marker(child, lang)
    if marker:
        parts.append(marker)
    recon = (
        verdict_marker(child, state=state, matched_title=matched_title, lang=lang)
        if verdict
        else ""
    )
    if recon:
        parts.append(_esc(recon))
    return parts


def child_line(
    child: MemoryItem,
    tz_name: str,
    *,
    state: str | None = None,
    matched_title: str | None = None,
    lang: str = i18n.DEFAULT,
    names: dict[str, str] | None = None,
    number: int | None = None,
    verdict: bool = True,
) -> str:
    """One card line for one child (HTML-escaped).

    ``state``/``matched_title`` come from the child's recon relation (the
    card builders look it up): an applied UPDATE renders "↩︎ applied:
    …" and a cancellation names its target. ``number`` prefixes the line
    so it matches its numbered buttons; ``names`` maps stored applies_to
    names to display names (the Hebrew alias when the bot speaks Hebrew).
    ``verdict=False`` leaves out the recon marker (a decided child whose
    relation is gone: "proposes …" would no longer be true)."""
    kind = child.type.value if child.type else ""
    emoji = CHILD_EMOJI.get(kind, "•")
    warning = "⚠️ " if child.date_conflict else ""
    prefix = f"{number}. " if number is not None else ""
    parts = [f"{prefix}{warning}{emoji} {i18n.iso(_esc(child.raw_text), lang)}"]
    parts.extend(_detail_parts(child, tz_name, lang, names))
    parts.extend(
        _marker_parts(child, state=state, matched_title=matched_title, lang=lang, verdict=verdict)
    )
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


Recon = dict[int, tuple[str, object, str | None]]

# verdicts that ask a question (⚠️ on the list button); MORE_DETAIL only adds
_FLAGGED_VERDICTS = frozenset({"update", "conflict", "cancellation"})


def decision_mark(child: MemoryItem) -> str | None:
    """✅ kept / ✏️ fixed (kept + user-edited) / 🗑 dropped; None = undecided."""
    if child.child_state == "dropped":
        return "🗑"
    if child.child_state == "kept":
        return "✏️" if (child.extraction_meta or {}).get("user_edited") else "✅"
    return None


def _card_line(
    child: MemoryItem,
    index: int,
    number: int | None,
    recon: Recon,
    tz_name: str,
    lang: str,
    names: dict[str, str] | None,
) -> str:
    """One list line: the child line, prefixed ✅/✏️/🗑 once decided."""
    mark = decision_mark(child)
    entry = recon.get(index)
    if entry is None:
        line = child_line(
            child, tz_name, lang=lang, names=names, number=number, verdict=mark is None
        )
    else:
        verdict, rel, matched_title = entry
        state = (getattr(rel, "provenance", None) or {}).get("state")
        line = child_line(
            child,
            tz_name,
            state=state,
            matched_title=matched_title if verdict == "cancellation" else None,
            lang=lang,
            names=names,
            number=number,
        )
    if mark == "🗑":
        return f"{mark} <s>{line}</s>"
    return f"{mark} {line}" if mark else line


def _header(source: MemoryItem, count: int, lang: str) -> str:
    title = _esc(source_short_title(source))
    if count == 1:
        return i18n.t(lang, "card_header_one", title=title)
    return i18n.t(lang, "card_header", n=count, title=title)


def fit_lines(lines: list[str], budget: int, lang: str = i18n.DEFAULT) -> list[str]:
    """The leading ``lines`` that fit ``budget`` characters (newline-joined),
    plus "… +k more" for the rest. Whole lines only, so no HTML tag is ever
    cut; the caller wraps the result (a blockquote stays closed)."""
    if sum(len(line) + 1 for line in lines) <= budget:
        return lines
    room = budget - len(i18n.t(lang, "more_lines", n=len(lines))) - 1
    kept: list[str] = []
    used = 0
    for line in lines:
        if used + len(line) + 1 > room:
            break
        kept.append(line)
        used += len(line) + 1
    return [*kept, i18n.t(lang, "more_lines", n=len(lines) - len(kept))]


def _fyi_block(fyi: list[str], lang: str) -> str:
    # collapsed: the family taps to read; no buttons for these lines
    label = i18n.t(lang, "card_fyi", n=len(fyi))
    shown = fit_lines(fyi, _FYI_SHARE - len(label) - 40, lang)
    return "<blockquote expandable>" + "\n".join([label, *shown]) + "</blockquote>"


def _split_lines(
    children: list[MemoryItem],
    tz_name: str,
    recon: Recon,
    lang: str,
    names: dict[str, str] | None,
) -> tuple[list[str], list[str]]:
    numbers = card_numbers(children, recon)
    lines: list[str] = []
    fyi: list[str] = []
    for index, child in enumerate(children):
        number = numbers[index]
        line = _card_line(child, index, number, recon, tz_name, lang, names)
        (lines if number is not None else fyi).append(line)
    return lines, fyi


def render_source_card(
    source: MemoryItem,
    children: list[MemoryItem],
    tz_name: str,
    *,
    recon: Recon | None = None,
    lang: str = i18n.DEFAULT,
    names: dict[str, str] | None = None,
) -> str:
    """The card's LIST text for one source's children (HTML-escaped).

    ``children`` are every child of the source (:func:`card_children_of`):
    undecided ones render as before, decided ones stay on the card with a
    ✅ / ✏️ / 🗑 prefix (no button). ``recon`` (D-recon-2a) maps a child's
    index to (verdict, relation, matched_title): verdict children render
    their state-aware marker line. Actionable children are numbered 1..k;
    FYI children (:func:`is_fyi`) follow, unnumbered, in one collapsed
    ``<blockquote expandable>`` (the bot sends parse_mode=HTML; every line
    is escaped by :func:`child_line`)."""
    recon = recon or {}
    lines, fyi = _split_lines(children, tz_name, recon, lang, names)
    header = _header(source, len(children), lang)
    tail = [_fyi_block(fyi, lang)] if fyi else []
    budget = CARD_TEXT_MAX - len(header) - sum(len(t) + 1 for t in tail) - 1
    return "\n".join([header, *fit_lines(lines, budget, lang), *tail])


def undecided_positions(children: list[MemoryItem], recon: Recon | None = None) -> list[int]:
    """Indexes of the undecided actionable children, in card order: the
    list view's buttons."""
    numbers = card_numbers(children, recon)
    return [
        index
        for index, (child, n) in enumerate(zip(children, numbers, strict=True))
        if n is not None and child.child_state == "proposed"
    ]


def page_count(undecided: int) -> int:
    """Pages of the list view: one up to LIST_PAGING_ABOVE items, then
    LIST_PAGE_SIZE item buttons per page."""
    if undecided <= LIST_PAGING_ABOVE:
        return 1
    return -(-undecided // LIST_PAGE_SIZE)


def page_of(position: int, undecided: int) -> int:
    """The list page that shows the ``position``-th undecided item."""
    if undecided <= LIST_PAGING_ABOVE:
        return 0
    return max(0, min(position, undecided - 1)) // LIST_PAGE_SIZE


def _button_title(text: str) -> str:
    text = " ".join(text.split())
    if len(text) <= BUTTON_TITLE_MAX:
        return text
    return text[: BUTTON_TITLE_MAX - 1].rstrip() + "…"


def item_button_label(child: MemoryItem, number: int, tz_name: str, *, flagged: bool) -> str:
    """'3 · 📅 Parents evening · 05.11' (⚠️ first for a recon verdict)."""
    kind = child.type.value if child.type else ""
    label = f"{number} · {CHILD_EMOJI.get(kind, '•')} {_button_title(child.raw_text)}"
    moment = _local(child, ZoneInfo(tz_name))
    if moment is not None:
        label += f" · {moment.strftime('%d.%m')}"
    return f"⚠️ {label}" if flagged else label


def _flagged(recon: Recon, index: int) -> bool:
    entry = recon.get(index)
    return entry is not None and entry[0] in _FLAGGED_VERDICTS


def proposal_keyboard(
    source_id: object,
    children: list[MemoryItem],
    tz_name: str,
    *,
    recon: Recon | None = None,
    lang: str = i18n.DEFAULT,
    page: int = 0,
) -> list[list[tuple[str, str]]]:
    """The LIST view's keyboard: one ``po:<child>`` button per undecided
    actionable child (one per row, labelled with its card number), a
    ◀ p/P ▶ row once more than LIST_PAGING_ABOVE items wait, then
    ✅ Keep all N (N = what is still undecided). Decided and FYI children
    get no button. Callback data stays < 64 bytes (prefix + uuid [+ page]).
    ``page`` is clamped to the pages that exist."""
    recon = recon or {}
    numbers = card_numbers(children, recon)
    waiting = undecided_positions(children, recon)
    pages = page_count(len(waiting))
    page = max(0, min(page, pages - 1))
    shown = waiting[page * LIST_PAGE_SIZE : (page + 1) * LIST_PAGE_SIZE] if pages > 1 else waiting
    rows: list[list[tuple[str, str]]] = []
    for index in shown:
        child, number = children[index], numbers[index]
        assert number is not None  # undecided_positions keeps numbered lines only
        label = item_button_label(child, number, tz_name, flagged=_flagged(recon, index))
        rows.append([(label, f"{CB_PROPOSED_OPEN}:{child.id}")])
    if pages > 1:
        rows.append(
            [
                ("◀", f"{CB_PROPOSED_PAGE}:{source_id}:{(page - 1) % pages}"),
                (f"{page + 1}/{pages}", CB_PROPOSED_NOOP),
                ("▶", f"{CB_PROPOSED_PAGE}:{source_id}:{(page + 1) % pages}"),
            ]
        )
    # Keep all keeps every proposed child (FYI included); a card of FYI
    # lines only still offers it
    remaining = len(waiting) or sum(1 for c in children if c.child_state == "proposed")
    if remaining:
        rows.append(
            [(i18n.t(lang, "btn_keep_all_n", n=remaining), f"{CB_PROPOSED_KEEP_ALL}:{source_id}")]
        )
    return rows


def _excerpt(quote: str) -> str:
    quote = " ".join(quote.split())
    if len(quote) > _EXCERPT_MAX:
        quote = quote[: _EXCERPT_MAX - 1].rstrip() + "…"
    return quote


def render_focus(
    source: MemoryItem,
    children: list[MemoryItem],
    index: int,
    tz_name: str,
    *,
    recon: Recon | None = None,
    lang: str = i18n.DEFAULT,
    names: dict[str, str] | None = None,
    quote: str | None = None,
    blocks: Iterable[str] = (),
    with_fyi: bool = False,
) -> str:
    """The FOCUS view of one child (HTML-escaped): the card header, then
    "{n} / {N} · {icon} {title}", the date · person line, the recon /
    superseded markers, ``blocks`` (a CONFLICT's both versions) and a short
    italic excerpt of the source quote. ``with_fyi`` adds the collapsed FYI
    lines (a card whose only actionable child opens in focus directly)."""
    recon = recon or {}
    numbers = card_numbers(children, recon)
    child = children[index]
    number = numbers[index]
    total = max((n for n in numbers if n is not None), default=0)
    kind = child.type.value if child.type else ""
    warning = "⚠️ " if child.date_conflict else ""
    title = i18n.iso(_esc(child.raw_text), lang)
    lines = [
        _header(source, len(children), lang),
        f"<b>{number} / {total} · {warning}{CHILD_EMOJI.get(kind, '•')} {title}</b>",
    ]
    details = _detail_parts(child, tz_name, lang, names)
    if details:
        lines.append(" · ".join(details))
    entry = recon.get(index)
    state = (getattr(entry[1], "provenance", None) or {}).get("state") if entry else None
    matched = entry[2] if entry and entry[0] == "cancellation" else None
    markers = _marker_parts(child, state=state, matched_title=matched, lang=lang, verdict=True)
    if markers:
        lines.append(" · ".join(markers))
    lines.extend(block for block in blocks if block)
    if quote and quote.strip():
        lines.append(f"<i>{i18n.iso(_esc(_excerpt(quote)), lang)}</i>")
    if with_fyi:
        _lines, fyi = _split_lines(children, tz_name, recon, lang, names)
        if fyi:
            lines.append(_fyi_block(fyi, lang))
    return "\n".join(lines)


def _plain(label_key: str, lang: str) -> str:
    """A numbered button template without its number (the focus view)."""
    return i18n.t(lang, label_key, n="").strip()


def verdict_rows(
    entry: tuple[str, object, str | None], lang: str = i18n.DEFAULT
) -> list[list[tuple[str, str]]] | None:
    """The recon verdict's buttons (D-recon-2a) for its focus view, or None
    for a verdict that asks nothing (MORE_DETAIL keeps Keep/Fix/Drop).

    UPDATE proposes ✅ Confirm update / ✖ Keep both (or ↩︎ Undo once
    auto-applied), CONFLICT asks ✅ keep existing / ✅ keep new,
    CANCELLATION offers ✅ Confirm cancellation / ✖ Not cancelled. The
    buttons carry the RELATION ID (never a position)."""
    from brain.services.relations_view import (  # noqa: PLC0415
        CB_RECON_DISCARD,
        CB_REL_CONFIRM,
        CB_REL_KEEP_BOTH,
        CB_REL_UNDO,
    )

    verdict, rel, _matched_title = entry
    rel_id = getattr(rel, "id", None)
    state = (getattr(rel, "provenance", None) or {}).get("state")
    confirm = f"{CB_REL_CONFIRM}:{rel_id}"
    discard = f"{CB_RECON_DISCARD}:{rel_id}"
    if verdict == "update" and state == "applied":
        return [[(_plain("btn_undo", lang), f"{CB_REL_UNDO}:{rel_id}")]]
    if verdict == "update":
        return [
            [
                (_plain("btn_confirm_update", lang), confirm),
                (_plain("btn_keep_both", lang), f"{CB_REL_KEEP_BOTH}:{rel_id}"),
            ]
        ]
    if verdict == "conflict":
        return [
            [
                (_plain("btn_keep_existing", lang), discard),
                (_plain("btn_keep_new", lang), confirm),
            ]
        ]
    if verdict == "cancellation":
        return [
            [
                (_plain("btn_confirm_cancel", lang), confirm),
                (_plain("btn_not_cancelled", lang), discard),
            ]
        ]
    return None


def focus_keyboard(
    source_id: object,
    child: MemoryItem,
    *,
    recon_entry: tuple[str, object, str | None] | None = None,
    lang: str = i18n.DEFAULT,
    back_page: int | None = 0,
) -> list[list[tuple[str, str]]]:
    """The FOCUS view's keyboard: ✅ Keep / ✏️ Fix / 🗑 Drop (or the
    child's recon verdict buttons), then ↩︎ Back to list — the back row
    returns to ``back_page``; None leaves it out (a one-item card)."""
    rows = verdict_rows(recon_entry, lang) if recon_entry is not None else None
    if rows is None:
        rows = [
            [
                (_plain("btn_keep", lang), f"{CB_PROPOSED_KEEP}:{child.id}"),
                (_plain("btn_fix", lang), f"{CB_PROPOSED_FIX}:{child.id}"),
                (_plain("btn_drop", lang), f"{CB_PROPOSED_DROP}:{child.id}"),
            ]
        ]
    if back_page is not None:
        back = f"{CB_PROPOSED_BACK}:{source_id}:{back_page}"
        rows.append([(i18n.t(lang, "btn_back_list"), back)])
    return rows


def render_done(
    source: MemoryItem,
    children: list[MemoryItem],
    tz_name: str,
    *,
    recon: Recon | None = None,
    lang: str = i18n.DEFAULT,
    names: dict[str, str] | None = None,
) -> str:
    """Nothing left to decide: a one-line summary ("✅ 9 kept · ✏️ 1 fixed ·
    🗑 1 dropped", zero parts left out) over the whole list, collapsed in
    one expandable blockquote. Counts cover the numbered (actionable)
    children — the lines the family decided on."""
    recon = recon or {}
    numbers = card_numbers(children, recon)
    counts = {"✅": 0, "✏️": 0, "🗑": 0}
    for child, number in zip(children, numbers, strict=True):
        mark = decision_mark(child)
        if number is not None and mark is not None:
            counts[mark] += 1
    parts = [
        i18n.t(lang, key, n=counts[mark])
        for mark, key in (("✅", "done_kept"), ("✏️", "done_fixed"), ("🗑", "done_dropped"))
        if counts[mark]
    ]
    summary = " · ".join(parts) or i18n.t(lang, "all_done").strip()
    lines, fyi = _split_lines(children, tz_name, recon, lang, names)
    header = _header(source, len(children), lang)
    # the closing tag is always written: lines are dropped whole, never cut
    budget = CARD_TEXT_MAX - len(summary) - len(header) - 60
    body = [header, *fit_lines([*lines, *fyi], budget, lang)]
    return summary + "\n<blockquote expandable>" + "\n".join(body) + "</blockquote>"


def card_children_of(session: Session, source_id) -> list[MemoryItem]:
    """Every proposal child linked to one source — undecided and decided —
    in card order (the list keeps decided lines with their ✅/✏️/🗑)."""
    from brain.db.models import ItemSource  # noqa: PLC0415

    stmt = (
        select(MemoryItem)
        .join(ItemSource, ItemSource.child_id == MemoryItem.id)
        .where(ItemSource.source_id == source_id)
        .where(MemoryItem.child_state.is_not(None))
        .order_by(MemoryItem.created_at.asc(), MemoryItem.id.asc())
    )
    # a child may carry several quote rows for the same source: one line each
    seen: set[object] = set()
    children: list[MemoryItem] = []
    for child in session.scalars(stmt):
        if child.id not in seen:
            seen.add(child.id)
            children.append(child)
    return children


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
                child.id,
                exc,
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
