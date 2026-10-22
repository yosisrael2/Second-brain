"""AskService tests: grounded answers with citations."""

from tests.fakes import FakeRepository

from brain.ai.ask import FakeLLMClient, LLMError
from brain.db.models import MemoryItem
from brain.services.ask import AskService
from brain.services.search import SearchHit


class StubSearch:
    """SearchService stand-in returning fixed hits."""

    def __init__(self, hits: list[SearchHit]) -> None:
        self._hits = hits
        self.last_query: str | None = None

    def search(self, query: str, *, limit: int, offset: int = 0) -> list[SearchHit]:
        self.last_query = query
        return self._hits[:limit]


def make_hits(repo: FakeRepository, *texts: str) -> list[SearchHit]:
    hits = []
    for text in texts:
        item = repo.add(MemoryItem(raw_text=text))
        hits.append(SearchHit(item=item, score=0.9))
    return hits


def cite_every_source(prompt: str) -> str:
    """LLM stand-in: echo every source id from the prompt as a citation."""
    ids = [line[2:].split("]")[0] for line in prompt.splitlines() if line.startswith("- [")]
    return "Based on: " + " ".join(f"[{item_id}]" for item_id in ids)


def test_ask_cites_item_ids_and_returns_sources():
    repo = FakeRepository()
    hits = make_hits(repo, "The server runs on port 27800.", "Deployments use Jenkins.")
    service = AskService(StubSearch(hits), FakeLLMClient(reply=cite_every_source))  # type: ignore[arg-type]

    result = service.ask("what port does the server use?")

    assert result.cited_ids == [str(hit.item.id) for hit in hits]
    assert result.sources == hits
    assert result.answer.startswith("Based on:")


def test_ask_builds_context_from_search_hits():
    repo = FakeRepository()
    hits = make_hits(repo, "some fact")
    captured: dict = {}

    def capture(prompt: str) -> str:
        captured["prompt"] = prompt
        return "I do not know."

    service = AskService(StubSearch(hits), FakeLLMClient(reply=capture))  # type: ignore[arg-type]

    service.ask("what?")

    assert "some fact" in captured["prompt"]
    assert f"- [{hits[0].item.id}]" in captured["prompt"]


def test_ask_llm_failure_propagates():
    class ExplodingLLM:
        def complete(self, prompt: str) -> str:
            raise LLMError("boom")

    service = AskService(StubSearch([]), ExplodingLLM())  # type: ignore[arg-type]

    try:
        service.ask("question")
        raised = False
    except LLMError:
        raised = True
    assert raised, "LLMError must propagate to the router"


def test_ask_with_no_sources_still_answers():
    service = AskService(StubSearch([]), FakeLLMClient(reply="No relevant information."))  # type: ignore[arg-type]

    result = service.ask("anything?")

    assert result.cited_ids == []
    assert result.sources == []


def test_prompt_sources_are_truncated_but_api_sources_are_not():
    """The LLM prompt carries truncated text; the API response keeps raw_text whole."""
    long_text = "needle " + "filler " * 800  # ~5600 chars > SOURCE_PROMPT_MAX_CHARS
    repo = FakeRepository()
    item = repo.add(MemoryItem(raw_text=long_text))
    hits = [SearchHit(item=item, score=0.9)]
    captured: dict[str, str] = {}

    def capture(prompt: str) -> str:
        captured["prompt"] = prompt
        return f"see [{item.id}]"

    service = AskService(StubSearch(hits), FakeLLMClient(reply=capture))  # type: ignore[arg-type]

    result = service.ask("where is the needle")

    assert len(captured["prompt"]) < len(long_text)  # prompt truncated, not the whole text
    assert "filler " * 400 not in captured["prompt"]  # a run beyond 2000 chars is gone
    assert result.sources[0].item.raw_text == long_text  # API response keeps it whole
    assert result.cited_ids == [str(item.id)]


def test_ask_context_dedupes_identical_raw_text():
    """Split WhatsApp chunks can share text: the LLM sees each text once."""
    repo = FakeRepository()
    first = repo.add(MemoryItem(raw_text="identical text"))
    second = repo.add(MemoryItem(raw_text="identical text"))
    hits = [
        SearchHit(item=first, score=0.9),
        SearchHit(item=second, score=0.8),
    ]
    captured: dict = {}

    def capture(prompt: str) -> str:
        captured["prompt"] = prompt
        return "I do not know."

    service = AskService(StubSearch(hits), FakeLLMClient(reply=capture))  # type: ignore[arg-type]

    service.ask("what?")

    assert captured["prompt"].count("identical text") == 1


def test_ask_cited_ids_dedupe_preserving_order():
    """cited_ids lists each cited item once, in first-citation order."""
    repo = FakeRepository()
    hits = make_hits(repo, "fact one", "fact two")
    order = [str(hits[1].item.id), str(hits[0].item.id), str(hits[1].item.id)]

    from brain.ai.ask import extract_cited_ids

    cited = extract_cited_ids(
        f"x [{order[0]}] y [{order[1]}] z [{order[1]}]",
        [{"id": str(hits[0].item.id)}, {"id": str(hits[1].item.id)}],
    )

    assert cited == [str(hits[1].item.id), str(hits[0].item.id)]
