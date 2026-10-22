"""Extraction evaluation harness: score predicted children against gold labels.

This is the deterministic scorer for meaning extraction (design spec
`meaning-extraction-spec.md` + v2/v3/v4 revisions; gold-set process: items
25-28). It compares two JSONL files of child items — the hand-labeled gold
set (owned by the manager, drafted by a worker who builds no extraction) and
a prediction file — and reports precision/recall/F1, date accuracy,
applies_to accuracy, quote grounding, relation reproduction and
date_conflict recall.

INPUT FORMAT (one JSON object per line; unknown extra fields are ignored):

    {"email_id": "...", "child_id": "...", "type": "event|task|reminder|fact|...",
     "title": "...",                       # source language (Hebrew or English)
     "start": "2026-10-19T13:35:00",       # ISO local wall-clock; optional
     "end": null, "all_day": true,         # all_day rows carry a date-only start
     "due": "2026-11-01",                  # tasks: due when no start
     "applies_to": "rona",                 # "rona, itamar" | ["Rona", "me"] | "me" | "nobody"
     "importance": 3, "actionability": "now",
     "source_quote": "verbatim text from the email",
     "relation": {"kind": "supersedes", "target": "<child_id>"},   # or "supersedes:<child_id>"
     "date_conflict": false,               # explicit boolean flag
     "notes": "...", "email_text": "full source text (gold rows only, optional)"}

Email text for the grounding check: the first non-empty `email_text` seen for
an email_id among gold rows, overridden/filled by the `--emails` sidecar
(rows of {"email_id", "email_text"}). Grounding verifies each PRED source_quote
as a normalized substring of that email text (see brain.services.textnorm).

MATCHING (deterministic, documented):
- Rows are grouped by email_id; matching happens per email only. Gold rows in
  an email with no preds are false negatives; preds in an unknown email are
  false positives.
- Hard gates for a candidate pair (gold, pred): same `type` (casefolded
  string — types are not enum-coupled here), same local calendar day of the
  effective date (`start` if present else `due`), times within ±1 hour when
  both sides are timed (all_day rows gate on the day only; a timed row may
  match an all_day row of the same day, and then counts as date-inexact),
  and compatible `applies_to` (JSON string or JSON list of strings, both
  normalized to the same token set; normalized token sets equal;
  "unknown"/empty is a wildcard that matches anything — but see applies_to
  accuracy below; two different KNOWN values never match). Any field whose
  JSON value has an unexpected type (applies_to that is neither string nor
  list, non-boolean all_day/date_conflict, non-integer importance, non-string
  relation/notes/email_text/source_quote/actionability) raises EvalError —
  nothing is silently coerced to a default, because a silent fallback (e.g.
  applies_to -> unknown) would let every row match every prediction.
- Children with no date at all (standing facts) match only other dateless
  children of the same email. A date-only ISO start/due (e.g. "2026-11-01",
  no time part) implies an all-day child unless an explicit all_day field
  says otherwise, so a gold label written as a bare date still matches a
  prediction that resolved a time for the same day (and counts as
  date-inexact).
  children of the same email, by title + applies_to.
- Assignment: greedy over all gate-compatible pairs, best title similarity
  first (ties broken by gold then pred child_id — fully deterministic). Title
  similarity is max(SequenceMatcher ratio, token Jaccard) over normalized
  titles, so it works across Hebrew and English. Pairs below
  `--title-threshold` (default 0.3) are never matchable. Greedy is used over
  Hungarian: per-email candidate sets are tiny, greedy is deterministic, and
  it avoids a scipy dependency. P/R/F1 count a matched pair as TP, an
  unmatched pred as FP and an unmatched gold row as FN.

METRICS (all printed as `key: value` lines):
- precision/recall/F1 overall (micro) and per type.
- date_exact_match_rate: matched pairs whose compared date is exactly equal
  (same day, same all_day-ness, and for timed rows the same minute).
- applies_to_accuracy: matched pairs with equal normalized applies_to.
  "unknown" is a wildcard for MATCHING but a known gold value vs a pred
  "unknown" still counts as wrong (see applies_to_pred_unknown).
- grounding_rate: pred children whose source_quote verifies against the email
  text / pred children with known email text. Preds whose email text is
  unknown are counted separately (grounding_unverifiable) and excluded from
  the rate — a data gap is not the model's fault, but it is surfaced.
- relation reproduction per kind (e.g. supersedes/same_as/derived_from): a
  gold relation (kind, target=G2) is reproduced only if the pred matched to
  its source carries the same kind AND targets the pred matched to G2
  (direction-agnostic: the link may be recorded on either child).
- date_conflict_recall: gold children flagged date_conflict whose matched
  pred is flagged too. Gold rows may fall back to the word appearing in
  `notes`; pred rows must use the boolean field.

OUTPUT CONTRACTS:
- `--aggregate-only` prints ONLY `key: value` numbers (n/a where a rate has
  no denominator) — no titles, quotes, or email ids. This is what the
  extraction developer sees.
- full mode additionally writes a per-child markdown report (matched pairs
  with titles/quotes, misses, false positives, grounding failures) for the
  manager, and prints the report path.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

from brain.services.textnorm import normalize, quote_in_source

TIME_TOLERANCE = timedelta(hours=1)
DEFAULT_TITLE_THRESHOLD = 0.3


class EvalError(Exception):
    """Raised for unreadable inputs or schema violations; the CLI prints it."""


@dataclass
class Child:
    """One gold or predicted child row (fields beyond these are ignored)."""

    email_id: str
    child_id: str
    type: str
    title: str
    start: datetime | None
    due: datetime | None
    all_day: bool
    applies_to: frozenset[str]  # empty set == unknown (wildcard)
    applies_raw: str
    importance: int | None
    actionability: str | None
    source_quote: str
    relation: tuple[str, str] | None  # (kind, target child_id)
    date_conflict: bool
    notes: str
    email_text: str

    @property
    def effective_date(self) -> datetime | None:
        """The date this child is matched on: start, else due."""
        return self.start if self.start is not None else self.due


@dataclass
class Match:
    gold: Child
    pred: Child
    similarity: float


@dataclass
class TypeStat:
    gold: int = 0
    pred: int = 0
    tp: int = 0


@dataclass
class RelStat:
    gold: int = 0
    reproduced: int = 0
    unresolvable: int = 0


@dataclass
class GroundStat:
    grounded: int = 0
    denominator: int = 0
    unverifiable: int = 0


@dataclass
class EvalResult:
    gold_children: list[Child]
    pred_children: list[Child]
    matches: list[Match]
    unmatched_gold: list[Child]
    unmatched_pred: list[Child]
    type_stats: dict[str, TypeStat]
    date_exact: int
    date_denominator: int
    applies_ok: int
    applies_pred_unknown: int
    relations: dict[str, RelStat]
    grounding: GroundStat
    grounding_by_pred: dict[str, bool | None] = field(default_factory=dict)
    conflict_gold: int = 0
    conflict_reproduced: int = 0


# ---- input parsing -----------------------------------------------------------


def load_jsonl(path: str | Path) -> list[dict[str, Any]]:
    """Read a JSONL file of child rows; any problem raises EvalError."""
    file = Path(path)
    rows: list[dict[str, Any]] = []
    try:
        with file.open(encoding="utf-8") as handle:
            for lineno, line in enumerate(handle, 1):
                line = line.strip()
                if not line:
                    continue
                try:
                    value = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise EvalError(f"{file}:{lineno}: invalid JSON: {exc}") from exc
                if not isinstance(value, dict):
                    raise EvalError(f"{file}:{lineno}: expected a JSON object")
                rows.append(value)
    except OSError as exc:
        raise EvalError(f"cannot read {file}: {exc}") from exc
    return rows


def load_email_texts(path: str | Path) -> dict[str, str]:
    """Read the optional --emails sidecar: rows of {"email_id", "email_text"}."""
    texts: dict[str, str] = {}
    for idx, row_ in enumerate(load_jsonl(path)):
        email_id = row_.get("email_id")
        text = row_.get("email_text")
        if isinstance(email_id, str) and email_id.strip() and isinstance(text, str):
            texts[email_id.strip()] = text
        else:
            raise EvalError(f"emails sidecar row {idx}: need email_id + email_text")
    return texts


def _req_str(row: dict[str, Any], key: str, side: str, idx: int) -> str:
    value = row.get(key)
    if not isinstance(value, str) or not value.strip():
        raise EvalError(f"{side} row {idx}: missing required field {key!r}")
    return value.strip()


def _opt_datetime(row: dict[str, Any], key: str, side: str, idx: int) -> datetime | None:
    value = row.get(key)
    if value is None or value == "":
        return None
    if not isinstance(value, str):
        raise EvalError(f"{side} row {idx}: {key} must be an ISO string")
    try:
        return datetime.fromisoformat(value.strip())
    except ValueError as exc:
        raise EvalError(f"{side} row {idx}: bad datetime {value!r}: {exc}") from exc


def _opt_int(row: dict[str, Any], key: str, side: str, idx: int) -> int | None:
    """Optional integer field: absent/null -> None; any other type raises."""
    value = row.get(key)
    if value is None or value == "":
        return None
    if not isinstance(value, int) or isinstance(value, bool):
        raise EvalError(f"{side} row {idx}: {key} must be an integer")
    return value


def _opt_bool(row: dict[str, Any], key: str, side: str, idx: int) -> bool | None:
    """Optional boolean field: absent/null -> None; any other type raises."""
    value = row.get(key)
    if value is None:
        return None
    if not isinstance(value, bool):
        raise EvalError(f"{side} row {idx}: {key} must be a boolean")
    return value


def _opt_str(row: dict[str, Any], key: str, side: str, idx: int) -> str:
    """Optional string field: absent/null -> ''; any other type raises."""
    value = row.get(key)
    if value is None or value == "":
        return ""
    if not isinstance(value, str):
        raise EvalError(f"{side} row {idx}: {key} must be a string")
    return value.strip()


def _parse_relation(value: Any, side: str, idx: int) -> tuple[str, str] | None:
    """Accept {"kind", "target"} or "kind", "kind:target", "kind target".

    A bare kind string (no target) is kept as (kind, "") — the eval then
    reports the relation as unresolvable (target not labeled). Any other
    JSON type is a schema error, never silently ignored.
    """
    if value is None or value == "":
        return None
    if isinstance(value, str):
        text = value.strip()
        for sep in (":", " "):
            if sep in text:
                kind, target = (part.strip() for part in text.split(sep, 1))
                if kind and target:
                    return (kind.casefold(), target)
        return (text.casefold(), "") if text else None
    if isinstance(value, dict):
        dict_kind = value.get("kind")
        dict_target = value.get("target")
        if (
            not isinstance(dict_kind, str)
            or not isinstance(dict_target, str)
            or not dict_kind.strip()
            or not dict_target.strip()
        ):
            raise EvalError(
                f"{side} row {idx}: relation object needs non-empty string 'kind' and 'target'"
            )
        return (dict_kind.strip().casefold(), dict_target.strip())
    raise EvalError(
        f"{side} row {idx}: relation must be a string or an object with string 'kind' and 'target'"
    )


def _is_date_only(value: Any) -> bool:
    """True for an ISO date string with no time component (e.g. 2026-11-01)."""
    return (
        isinstance(value, str)
        and bool(value.strip())
        and "T" not in value
        and " " not in value.strip()
        and ":" not in value
    )


def _parse_applies_to(row: dict[str, Any], side: str, idx: int) -> tuple[frozenset[str], str]:
    """Normalize applies_to to a token set; str and list[str] both work.

    worker_3's gold schema uses a JSON LIST (e.g. ["Rona"], ["Itamar",
    "me"]); a string ("rona, me") normalizes to the same token set. An
    empty value or ["unknown"] becomes the empty set == unknown wildcard.
    Any other JSON type is a schema error — a silent fallback to unknown
    would let every row match every prediction.
    """
    value = row.get("applies_to")
    if value is None or value == "":
        return frozenset(), ""
    if isinstance(value, str):
        applies_raw = value.strip()
        raw_tokens = re.split(r"[,;/]", applies_raw)
    elif isinstance(value, list):
        if not all(isinstance(item, str) for item in value):
            raise EvalError(f"{side} row {idx}: applies_to list must contain strings")
        applies_raw = ", ".join(item.strip() for item in value)
        raw_tokens = value
    else:
        raise EvalError(f"{side} row {idx}: applies_to must be a string or list of strings")
    tokens = frozenset(token.strip().casefold() for token in raw_tokens if token.strip())
    if tokens == {"unknown"}:
        tokens = frozenset()
    return tokens, applies_raw


def _parse_child(row: dict[str, Any], side: str, idx: int) -> Child:
    tokens, applies_raw = _parse_applies_to(row, side, idx)
    notes = _opt_str(row, "notes", side, idx)
    email_text = _opt_str(row, "email_text", side, idx)
    relation = _parse_relation(row.get("relation"), side, idx)
    explicit_all_day = _opt_bool(row, "all_day", side, idx)
    if explicit_all_day is not None:
        all_day = explicit_all_day
    else:
        # A date-only ISO start/due (no time part) implies an all-day child.
        all_day = _is_date_only(row.get("start")) or _is_date_only(row.get("due"))
    actionability = _opt_str(row, "actionability", side, idx) or None
    source_quote = _opt_str(row, "source_quote", side, idx)
    explicit_conflict = _opt_bool(row, "date_conflict", side, idx)
    return Child(
        email_id=_req_str(row, "email_id", side, idx),
        child_id=_req_str(row, "child_id", side, idx),
        type=_req_str(row, "type", side, idx).casefold(),
        title=_req_str(row, "title", side, idx),
        start=_opt_datetime(row, "start", side, idx),
        due=_opt_datetime(row, "due", side, idx),
        all_day=all_day,
        applies_to=tokens,
        applies_raw=applies_raw,
        importance=_opt_int(row, "importance", side, idx),
        actionability=actionability,
        source_quote=source_quote,
        relation=relation,
        # Gold rows only: the notes text may flag a date_conflict. Preds must
        # use the explicit boolean field (a pred note mentioning the word is
        # not a claim the pipeline verified).
        date_conflict=explicit_conflict is True
        or (side == "gold" and "date_conflict" in notes.casefold()),
        notes=notes,
        email_text=email_text,
    )


def _check_unique(children: list[Child], side: str) -> None:
    seen: set[str] = set()
    for child in children:
        if child.child_id in seen:
            raise EvalError(f"duplicate child_id {child.child_id!r} in {side} rows")
        seen.add(child.child_id)


# ---- matching + metrics ------------------------------------------------------


def title_similarity(a: str, b: str) -> float:
    """max(SequenceMatcher ratio, token Jaccard) over normalized titles.

    SequenceMatcher handles near-identical strings and small edits; token
    Jaccard handles word reordering. Both operate on the shared normalizer's
    output, so geresh/nikud/bidi/whitespace/case differences never matter and
    Hebrew and English titles behave the same way.
    """
    left, right = normalize(a), normalize(b)
    if not left or not right:
        return 0.0
    if left == right:
        return 1.0
    ratio = SequenceMatcher(None, left, right).ratio()
    left_tokens, right_tokens = set(left.split()), set(right.split())
    jaccard = (
        len(left_tokens & right_tokens) / len(left_tokens | right_tokens)
        if left_tokens and right_tokens
        else 0.0
    )
    return max(ratio, jaccard)


def _compatible(gold: Child, pred: Child) -> bool:
    if gold.type != pred.type:
        return False
    gold_date, pred_date = gold.effective_date, pred.effective_date
    if gold_date is None or pred_date is None:
        return gold_date is None and pred_date is None  # both dateless: compatible
    if gold_date.date() != pred_date.date():
        return False
    if not gold.all_day and not pred.all_day and abs(gold_date - pred_date) > TIME_TOLERANCE:
        return False
    if gold.applies_to and pred.applies_to:
        return gold.applies_to == pred.applies_to
    return True  # unknown/empty applies_to on either side is a wildcard


def evaluate(
    gold_rows: list[dict[str, Any]],
    pred_rows: list[dict[str, Any]],
    *,
    email_texts: dict[str, str] | None = None,
    title_threshold: float = DEFAULT_TITLE_THRESHOLD,
) -> EvalResult:
    """Match pred children to gold children per email and compute all metrics."""
    gold = [_parse_child(row, "gold", i) for i, row in enumerate(gold_rows)]
    pred = [_parse_child(row, "pred", i) for i, row in enumerate(pred_rows)]
    _check_unique(gold, "gold")
    _check_unique(pred, "pred")

    texts: dict[str, str] = {}
    for child in gold:
        if child.email_text:
            texts.setdefault(child.email_id, child.email_text)
    if email_texts:
        texts.update(email_texts)  # the sidecar wins over inline gold rows

    gold_by_email: dict[str, list[Child]] = {}
    pred_by_email: dict[str, list[Child]] = {}
    for child in gold:
        gold_by_email.setdefault(child.email_id, []).append(child)
    for child in pred:
        pred_by_email.setdefault(child.email_id, []).append(child)

    matches: list[Match] = []
    used_gold: set[str] = set()
    used_pred: set[str] = set()
    for email_id in sorted(set(gold_by_email) | set(pred_by_email)):
        candidates: list[tuple[float, str, str, Child, Child]] = []
        for gold_child in gold_by_email.get(email_id, []):
            for pred_child in pred_by_email.get(email_id, []):
                similarity = title_similarity(gold_child.title, pred_child.title)
                if similarity < title_threshold:
                    continue
                if not _compatible(gold_child, pred_child):
                    continue
                candidates.append(
                    (-similarity, gold_child.child_id, pred_child.child_id, gold_child, pred_child)
                )
        candidates.sort(key=lambda item: (item[0], item[1], item[2]))
        for neg_sim, _, _, gold_child, pred_child in candidates:
            if gold_child.child_id in used_gold or pred_child.child_id in used_pred:
                continue
            used_gold.add(gold_child.child_id)
            used_pred.add(pred_child.child_id)
            matches.append(Match(gold_child, pred_child, -neg_sim))
        # rows in an email that only exists on one side are all FN/FP

    type_stats: dict[str, TypeStat] = {}
    for child in gold:
        type_stats.setdefault(child.type, TypeStat()).gold += 1
    for child in pred:
        type_stats.setdefault(child.type, TypeStat()).pred += 1
    for match in matches:
        type_stats[match.gold.type].tp += 1

    date_exact = 0
    date_denominator = 0
    for match in matches:
        gold_date, pred_date = (
            match.gold.effective_date,
            match.pred.effective_date,
        )
        if gold_date is None or pred_date is None:
            continue  # dateless pairs have no date to compare
        date_denominator += 1
        if gold_date == pred_date and match.gold.all_day == match.pred.all_day:
            date_exact += 1

    applies_ok = sum(1 for m in matches if m.gold.applies_to == m.pred.applies_to)
    applies_pred_unknown = sum(1 for m in matches if not m.pred.applies_to and m.gold.applies_to)

    relations: dict[str, RelStat] = {}
    gold_by_id = {child.child_id: child for child in gold}
    match_of_gold = {m.gold.child_id: m for m in matches}
    for child in gold:
        if child.relation is None:
            continue
        kind, target_id = child.relation
        stat = relations.setdefault(kind, RelStat())
        stat.gold += 1
        target = gold_by_id.get(target_id)
        if target is None:
            stat.unresolvable += 1  # target not labeled -> cannot check
            continue
        source_match = match_of_gold.get(child.child_id)
        target_match = match_of_gold.get(target.child_id)
        if source_match is None or target_match is None:
            continue  # either side unmatched -> relation cannot reproduce
        # direction-agnostic: the link may be recorded on either pred child
        for source, target_pred in (
            (source_match.pred, target_match.pred),
            (target_match.pred, source_match.pred),
        ):
            if (
                source.relation is not None
                and source.relation[0] == kind
                and source.relation[1] == target_pred.child_id
            ):
                stat.reproduced += 1
                break

    grounding_by_pred: dict[str, bool | None] = {}
    grounded = 0
    denominator = 0
    unverifiable = 0
    for child in pred:
        text = texts.get(child.email_id)
        if text is None:
            grounding_by_pred[child.child_id] = None
            unverifiable += 1
            continue
        denominator += 1
        verdict = quote_in_source(child.source_quote, text)
        grounding_by_pred[child.child_id] = verdict
        if verdict:
            grounded += 1

    conflicted = [child for child in gold if child.date_conflict]
    conflict_reproduced = sum(
        1
        for child in conflicted
        if child.child_id in match_of_gold and match_of_gold[child.child_id].pred.date_conflict
    )

    return EvalResult(
        gold_children=gold,
        pred_children=pred,
        matches=matches,
        unmatched_gold=[c for c in gold if c.child_id not in used_gold],
        unmatched_pred=[c for c in pred if c.child_id not in used_pred],
        type_stats=type_stats,
        date_exact=date_exact,
        date_denominator=date_denominator,
        applies_ok=applies_ok,
        applies_pred_unknown=applies_pred_unknown,
        relations=relations,
        grounding=GroundStat(grounded=grounded, denominator=denominator, unverifiable=unverifiable),
        grounding_by_pred=grounding_by_pred,
        conflict_gold=len(conflicted),
        conflict_reproduced=conflict_reproduced,
    )


# ---- output ------------------------------------------------------------------


def _fmt(value: float | None) -> str:
    return "n/a" if value is None else str(round(value, 4))


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", text.casefold()).strip("_") or "unknown"


def _prf(tp: int, gold: int, pred: int) -> tuple[float | None, float | None, float | None]:
    precision = tp / pred if pred else None
    recall = tp / gold if gold else None
    if precision is None or recall is None:
        return precision, recall, None
    if precision + recall == 0:
        return precision, recall, 0.0
    return precision, recall, 2 * precision * recall / (precision + recall)


def format_aggregate(result: EvalResult) -> str:
    """Aggregate numbers only — no titles, quotes, or email ids (worker-facing)."""
    tp = len(result.matches)
    lines = [
        f"gold_children: {len(result.gold_children)}",
        f"pred_children: {len(result.pred_children)}",
        f"matched: {tp}",
        f"unmatched_gold: {len(result.unmatched_gold)}",
        f"unmatched_pred: {len(result.unmatched_pred)}",
    ]
    precision, recall, f1 = _prf(tp, len(result.gold_children), len(result.pred_children))
    lines += [
        f"precision: {_fmt(precision)}",
        f"recall: {_fmt(recall)}",
        f"f1: {_fmt(f1)}",
    ]
    for type_name, type_stat in sorted(result.type_stats.items()):
        p, r, f = _prf(type_stat.tp, type_stat.gold, type_stat.pred)
        lines += [
            f"type_{_slug(type_name)}_gold: {type_stat.gold}",
            f"type_{_slug(type_name)}_pred: {type_stat.pred}",
            f"type_{_slug(type_name)}_matched: {type_stat.tp}",
            f"type_{_slug(type_name)}_precision: {_fmt(p)}",
            f"type_{_slug(type_name)}_recall: {_fmt(r)}",
            f"type_{_slug(type_name)}_f1: {_fmt(f)}",
        ]
    date_rate = result.date_exact / result.date_denominator if result.date_denominator else None
    applies_rate = result.applies_ok / tp if tp else None
    grounding_rate = (
        result.grounding.grounded / result.grounding.denominator
        if result.grounding.denominator
        else None
    )
    unknown_rate = result.applies_pred_unknown / tp if tp else None
    lines += [
        f"date_exact_match_rate: {_fmt(date_rate)}",
        f"applies_to_accuracy: {_fmt(applies_rate)}",
        f"applies_to_pred_unknown: {result.applies_pred_unknown}",
        f"applies_to_pred_unknown_rate: {_fmt(unknown_rate)}",
        f"grounding_rate: {_fmt(grounding_rate)}",
        f"grounding_grounded: {result.grounding.grounded}",
        f"grounding_denominator: {result.grounding.denominator}",
        f"grounding_unverifiable: {result.grounding.unverifiable}",
    ]
    for kind, stat in sorted(result.relations.items()):
        checkable = stat.gold - stat.unresolvable
        rate = stat.reproduced / checkable if checkable else None
        lines += [
            f"relation_{_slug(kind)}_gold: {stat.gold}",
            f"relation_{_slug(kind)}_reproduced: {stat.reproduced}",
            f"relation_{_slug(kind)}_unresolvable: {stat.unresolvable}",
            f"relation_{_slug(kind)}_rate: {_fmt(rate)}",
        ]
    conflict_rate = (
        result.conflict_reproduced / result.conflict_gold if result.conflict_gold else None
    )
    lines += [
        f"date_conflict_gold: {result.conflict_gold}",
        f"date_conflict_reproduced: {result.conflict_reproduced}",
        f"date_conflict_recall: {_fmt(conflict_rate)}",
    ]
    return "\n".join(lines)


def _relation_display(relation: tuple[str, str] | None) -> str:
    return "none" if relation is None else f"{relation[0]} -> {relation[1]}"


def write_report(
    result: EvalResult,
    path: str | Path,
    *,
    gold_path: str = "",
    pred_path: str = "",
) -> Path:
    """Full per-child markdown report — manager only (titles, quotes, ids)."""
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "# Extraction eval report",
        "",
        f"- gold: `{gold_path}`",
        f"- pred: `{pred_path}`",
        "",
        "## Aggregate",
        "",
        "```",
        format_aggregate(result).rstrip(),
        "```",
        "",
    ]

    grounding_failures = []
    for child in result.pred_children:
        verdict = result.grounding_by_pred.get(child.child_id)
        if verdict is False:
            grounding_failures.append(
                f"- pred `{child.child_id}` (email `{child.email_id}`): quote not "
                f"found in source — {child.source_quote!r}"
            )
        elif verdict is None:
            grounding_failures.append(
                f"- pred `{child.child_id}` (email `{child.email_id}`): "
                f"unverifiable — no email text for `{child.email_id}`"
            )
    if grounding_failures:
        lines += ["## Grounding failures", *grounding_failures, ""]

    email_ids = {child.email_id for child in result.gold_children}
    email_ids.update(child.email_id for child in result.pred_children)
    for email_id in sorted(email_ids):
        lines += [f"## Email `{email_id}`", ""]
        email_matches = [m for m in result.matches if m.gold.email_id == email_id]
        if email_matches:
            lines += ["### Matched", ""]
            for m in email_matches:
                verdict = result.grounding_by_pred.get(m.pred.child_id)
                if verdict is None:
                    quote_line = "quote: unverifiable (no email text)"
                elif verdict:
                    quote_line = f"quote: verified — {m.pred.source_quote!r}"
                else:
                    quote_line = f"quote: NOT VERIFIED — {m.pred.source_quote!r}"
                lines += [
                    f"- gold `{m.gold.child_id}` <-> pred `{m.pred.child_id}` — "
                    f"type `{m.gold.type}`, title similarity {m.similarity:.2f}",
                    f"  - gold title: {m.gold.title}",
                    f"  - pred title: {m.pred.title}",
                    f"  - date: gold {m.gold.effective_date} (all_day={m.gold.all_day})"
                    f" / pred {m.pred.effective_date} (all_day={m.pred.all_day})",
                    f"  - applies_to: gold {m.gold.applies_raw or 'unknown'}"
                    f" / pred {m.pred.applies_raw or 'unknown'}",
                    f"  - {quote_line}",
                    f"  - relation: gold {_relation_display(m.gold.relation)}"
                    f" / pred {_relation_display(m.pred.relation)}",
                    f"  - date_conflict: gold {'yes' if m.gold.date_conflict else 'no'}"
                    f" / pred {'yes' if m.pred.date_conflict else 'no'}",
                ]
                if m.gold.notes or m.pred.notes:
                    lines.append(f"  - notes: gold {m.gold.notes!r} / pred {m.pred.notes!r}")
            lines.append("")
        misses = [c for c in result.unmatched_gold if c.email_id == email_id]
        if misses:
            lines += [
                "### Unmatched gold (missed)",
                "",
                *[
                    f"- gold `{c.child_id}` ({c.type}) — {c.title}; "
                    f"start {c.effective_date}; quote: {c.source_quote!r}"
                    for c in misses
                ],
                "",
            ]
        false_positives = [c for c in result.unmatched_pred if c.email_id == email_id]
        if false_positives:
            lines += [
                "### Unmatched pred (false positives)",
                "",
                *[
                    f"- pred `{c.child_id}` ({c.type}) — {c.title}; "
                    f"start {c.effective_date}; quote: {c.source_quote!r}"
                    for c in false_positives
                ],
                "",
            ]
        if not email_matches and not misses and not false_positives:
            lines += ["_(no children)_", ""]
    out.write_text("\n".join(lines), encoding="utf-8")
    return out
