"""Ask use cases: answer a question from retrieved context (Service layer)."""

from __future__ import annotations

from dataclasses import dataclass, field

from brain.ai.ask import LLMClient, answer
from brain.services.search import SearchHit, SearchService

DEFAULT_TOP_K = 5


@dataclass(frozen=True)
class AskResult:
    """A grounded answer plus the items it was built from."""

    answer: str
    cited_ids: list[str] = field(default_factory=list)
    sources: list[SearchHit] = field(default_factory=list)
    model: str | None = None


class AskService:
    """Answers questions over the memory store: search top-k, then LLM.

    LLM failures propagate as :class:`brain.ai.ask.LLMError` (routers map
    them to 502); search itself already degrades to keyword fallback.
    """

    def __init__(self, search: SearchService, llm: LLMClient, top_k: int = DEFAULT_TOP_K) -> None:
        self._search = search
        self._llm = llm
        self._top_k = top_k

    def ask(self, question: str) -> AskResult:
        """Answer ``question`` from the top-k most relevant memory items."""
        hits = self._search.search(question, limit=self._top_k)
        context = [{"id": str(hit.item.id), "text": hit.item.raw_text} for hit in hits]
        result = answer(question, context, llm=self._llm)
        return AskResult(
            answer=result["answer"],
            cited_ids=result["cited_ids"],
            sources=hits,
            model=result["model"],
        )
