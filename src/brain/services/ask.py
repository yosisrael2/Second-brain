"""Ask use cases: answer a question from retrieved context (Service layer)."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
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
    ) -> None:
        self._search = search
        self._llm = llm
        self._top_k = top_k
        self._recall_pool = recall_pool
        self._tz = tz or ZoneInfo("UTC")
        # Returns the confirmed-memory prompt block ('' when memory is empty);
        # empty output keeps the prompt byte-identical to the pre-memory format.
        self._memory_block = memory_block

    def ask(self, question: str, *, now: datetime | None = None) -> AskResult:
        """Answer ``question`` from the top-k most relevant memory items.

        ``now`` anchors relative date expressions ("today", "this week");
        production passes nothing (real clock), tests pin it.
        """
        with ops.span("ask", "search", edge="ask->search"):
            # rerank sees the whole pool; only the top_k best reach the prompt
            pool = self._search.search(question, limit=self._recall_pool)
            hits = pool[: self._top_k]
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
        with ops.span("ask", "llm", edge="ask->llm") as span:
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
