"""Ask use cases: answer a question from retrieved context (Service layer)."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol
from zoneinfo import ZoneInfo

from brain import ops
from brain.ai.ask import LLMClient, answer
from brain.db.models import MemoryItem
from brain.services.search import SearchHit, SearchService
from brain.services.temporal import parse_question_ranges

DEFAULT_TOP_K = 8
# Candidates fetched from semantic search before truncating to top_k. The
# reranker can only reorder what reaches it: measured on the live corpus, an
# English question against Hebrew emails scores ~0.2 cosine (the TALAN email
# sits at vector rank 23) — with a pool of 8 the reranker never sees it; at
# 32 it lands in the top-8 after rerank. One rerank call, ~4x documents.
DEFAULT_RECALL_POOL = 32
# Each source is truncated in the LLM prompt only; the API response keeps the
# full raw_text. Bounds prompt size (top_k x raw_text can otherwise reach
# ~50k chars) and blunts cost/latency surprises.
SOURCE_PROMPT_MAX_CHARS = 2000
# Day/date questions fetch every event in the resolved range; this cap keeps
# a week-long range from swallowing the whole prompt.
TEMPORAL_FETCH_CAP = 30
# Kid-recall leg (PR 0d): when a question names a confirmed kid, items from
# that kid's channels/class join the recall pool before the rerank. Default
# cap for the leg (Settings.ask_kid_recall_limit overrides).
DEFAULT_KID_RECALL_LIMIT = 16


@dataclass(frozen=True)
class AskResult:
    """A grounded answer plus the items it was built from."""

    answer: str
    cited_ids: list[str] = field(default_factory=list)
    sources: list[SearchHit] = field(default_factory=list)
    model: str | None = None


def _unique_by_text(hits: list[SearchHit]) -> list[SearchHit]:
    """Collapse hits with identical raw_text (split WhatsApp chunks)."""
    seen: set[str] = set()
    unique: list[SearchHit] = []
    for hit in hits:
        if hit.item.raw_text not in seen:
            seen.add(hit.item.raw_text)
            unique.append(hit)
    return unique


class MemoryServiceProtocol(Protocol):
    """What the kid-recall leg needs from MemoryService (overview)."""

    def overview(
        self, *, status: str = "confirmed", q: str | None = None
    ) -> list[dict[str, Any]]: ...


class KidRecallRepoProto(Protocol):
    """What the kid-recall leg needs from the items repository."""

    def by_text_any(self, needles: Sequence[str], *, limit: int) -> Sequence[MemoryItem]: ...


class AskService:
    """Answers questions over the memory store: search top-k, then LLM.

    Day/date questions ("Tuesday", "Friday evening", "השבוע") cannot be
    answered by top-k similarity alone: :mod:`brain.services.temporal`
    resolves them to [start, end) ranges and EVERY item occurring in the
    range is merged into the context AHEAD of the semantic hits (dedupe by
    id and identical text). LLM failures propagate as
    :class:`brain.ai.ask.LLMError` (routers map them to 502); search itself
    already degrades to keyword fallback.
    """

    def __init__(
        self,
        search: SearchService,
        llm: LLMClient,
        top_k: int = DEFAULT_TOP_K,
        recall_pool: int = DEFAULT_RECALL_POOL,
        tz: ZoneInfo | None = None,
        memory_block: Callable[[], str] | None = None,
        memory: MemoryServiceProtocol | None = None,
        items_repo: KidRecallRepoProto | None = None,
        kid_recall_limit: int = DEFAULT_KID_RECALL_LIMIT,
    ) -> None:
        self._search = search
        self._llm = llm
        self._top_k = top_k
        self._recall_pool = recall_pool
        self._tz = tz or ZoneInfo("UTC")
        # Returns the confirmed-memory prompt block ('' when memory is empty);
        # empty output keeps the prompt byte-identical to the pre-memory format.
        self._memory_block = memory_block
        # PR 0d kid-recall leg: both must be present for the leg to run.
        self._memory = memory
        self._items_repo = items_repo
        self._kid_recall_limit = kid_recall_limit

    def ask(
        self, question: str, *, now: datetime | None = None, record_ops: bool = True
    ) -> AskResult:
        """Answer ``question`` from the top-k most relevant memory items.

        ``now`` anchors relative date expressions ("today", "this week");
        production passes nothing (real clock), tests pin it.

        ``record_ops=False`` runs the whole path WITHOUT recording ops
        spans/events (ask->search, ask->llm, kid_recall). DIAGNOSTICS ONLY:
        live checks that exercise the ask path with a fake LLM must pass it,
        otherwise /ops/health shows 'llm — last call failed' until the next
        real call. Real requests keep the default (True) — no change.
        """
        span_factory = ops.span if record_ops else _null_span
        with span_factory("ask", "search", edge="ask->search") as span:
            # rerank sees the whole pool; only the top_k best reach the prompt.
            # No kid named -> the call is EXACTLY the pre-0d one (byte-identical
            # pool; also keeps third-party search fakes without the kwarg working).
            kid_items = self._kid_recall_items(question, record_ops=record_ops)
            if kid_items:
                pool = self._search.search(
                    question,
                    limit=self._recall_pool,
                    extra_items=kid_items,
                )
            else:
                pool = self._search.search(question, limit=self._recall_pool)
            hits = pool[: self._top_k]
            pool_ids = [str(hit.item.id) for hit in pool]
            span.detail = {
                "kid_leg_fired": bool(kid_items),
                "kid_leg_count": len(kid_items),
                "pool_size": len(pool_ids),
                "pool_hash": hashlib.sha1(",".join(pool_ids).encode()).hexdigest()[:12],
            }
        context_hits = self._temporal_hits(question, now) + hits
        context_hits = _unique_by_text(self._unique_by_id(context_hits))
        context = [
            {
                "id": str(hit.item.id),
                "text": hit.item.raw_text[:SOURCE_PROMPT_MAX_CHARS],
                "when": self._when_of(hit.item),
            }
            for hit in context_hits
        ]
        with span_factory("ask", "llm", edge="ask->llm") as span:
            result = answer(
                question,
                context,
                llm=self._llm,
                now=now,
                tz=self._tz,
                memory_block=self._memory_block() if self._memory_block else "",
            )
            span.detail = {"model": result["model"], "cited": len(result["cited_ids"])}
        return AskResult(
            answer=result["answer"],
            cited_ids=result["cited_ids"],
            sources=_unique_by_text(context_hits),
            model=result["model"],
        )

    def _kid_recall_items(self, question: str, *, record_ops: bool = True) -> list[MemoryItem]:
        """Extra candidates: items from a named kid's channels and class (PR 0d).

        The question is NEVER rewritten — this only widens recall. A kid is
        named when the question contains their name or an alias
        (case-insensitive); their channels are the ones whose current
        confirmed ``applies_to`` is that kid or ``family``. WhatsApp captures
        keep the chat name in their raw_text header, so the exact stored
        channel name matches those items. No kid named -> no leg at all:
        behaviour is byte-identical to the pre-0d pipeline.
        """
        if self._memory is None or self._items_repo is None:
            return []
        entities = self._memory.overview(status="confirmed", q=None)

        def mentions(name: str) -> bool:
            if not name:
                return False
            # Whole-word match only: 'May' must not fire inside 'mayor',
            # 'שחר' not inside 'שחרור'. \w is Unicode-aware, so Hebrew
            # letters count as word characters on both sides. Hebrew words,
            # though, glue one-letter prefixes onto the word itself ('לאיתמר',
            # 'ואיתמר', 'שלאיתמר') — so for names written in Hebrew letters
            # allow an optional run of up to 3 Hebrew prefix letters right
            # before the name, still requiring that nothing word-like
            # precedes the run and that nothing word-like follows the name.
            body = re.escape(name)
            if re.search(r"[\u05d0-\u05ea]", name):
                body = rf"[והבכלמש]{{0,3}}{body}"
            return re.search(rf"(?<!\w){body}(?!\w)", question, re.IGNORECASE) is not None

        kids: list[dict[str, Any]] = []
        channels: list[dict[str, Any]] = []
        for entity in entities:
            facts = entity.get("facts", [])
            if entity.get("kind") == "person" and any(
                fact.get("predicate") == "grade" for fact in facts
            ):
                kids.append(entity)
            elif entity.get("kind") == "channel":
                channels.append(entity)
        needles: list[str] = []
        for kid in kids:
            names = [kid.get("name", ""), *(kid.get("aliases") or [])]
            if not any(mentions(name) for name in names):
                continue
            for channel in channels:
                applies = [
                    fact.get("value")
                    for fact in channel.get("facts", [])
                    if fact.get("predicate") == "applies_to"
                ]
                if kid.get("name") in applies or "family" in applies:
                    needles.append(channel.get("name", ""))
            class_value = next(
                (
                    fact.get("value")
                    for fact in kid.get("facts", [])
                    if fact.get("predicate") == "class" and fact.get("value")
                ),
                None,
            )
            if class_value:
                needles.append(class_value)
        needles = [needle for needle in dict.fromkeys(needles) if needle]
        if not needles:
            return []
        items = list(self._items_repo.by_text_any(needles, limit=self._kid_recall_limit))
        if record_ops:
            ops.record_event(
                "ask",
                "kid_recall",
                ok=True,
                detail={
                    "needles": len(needles),
                    "fetched": len(items),
                },
            )
        return items

    def _temporal_hits(self, question: str, now: datetime | None) -> list[SearchHit]:
        """Fetch every item inside the question's resolved date ranges."""
        instant = now or datetime.now(UTC)
        ranges = parse_question_ranges(question, self._tz, instant)
        hits: list[SearchHit] = []
        for start, end in ranges:
            hits.extend(self._search.by_occurrence(start, end, limit=TEMPORAL_FETCH_CAP))
        return hits

    def _unique_by_id(self, hits: list[SearchHit]) -> list[SearchHit]:
        """Order-preserving id dedupe (temporal + semantic can overlap)."""
        seen: set[str] = set()
        unique: list[SearchHit] = []
        for hit in hits:
            key = str(hit.item.id)
            if key not in seen:
                seen.add(key)
                unique.append(hit)
        return unique

    def _when_of(self, item: MemoryItem) -> str | None:
        """Local When-line for the prompt ('Fri 23 Oct 19:00-22:00'), or None."""
        start = item.occurred_at
        if start is None:
            return None
        local = start.astimezone(self._tz)
        end = (item.meta or {}).get("end") or (item.meta or {}).get("dtend")
        if isinstance(end, str):
            try:
                end_dt = datetime.fromisoformat(end)
                end_str = f"-{end_dt.astimezone(self._tz):%H:%M}"
            except ValueError:
                end_str = ""
        else:
            end_str = ""
        return f"{local:%a} {local.day} {local:%b} {local:%H:%M}{end_str}"


class _NullSpan:
    """Stand-in for ops.span when recording is off (diagnostics runs)."""

    def __init__(self) -> None:
        self.detail: dict[str, Any] = {}

    def __enter__(self) -> _NullSpan:
        return self

    def __exit__(self, *exc: object) -> None:
        return None


def _null_span(component: str, kind: str, edge: str | None = None) -> _NullSpan:
    """Drop-in for ops.span that records nothing."""
    return _NullSpan()
