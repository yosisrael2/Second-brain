"""Model configurability and visibility for /ask.

Covers: the default Anthropic model id, model pass-through to the SDK/API
call, and observability — the model that actually answered travels with the
completion (never shared client state) and is included in the /ask response.
All offline (fake query_fn / httpx.MockTransport / in-memory Qdrant / fake
repo).
"""

import json
import threading
import time
from collections.abc import AsyncIterator

import httpx
from fastapi.testclient import TestClient
from qdrant_client import QdrantClient
from tests.fakes import FakeRepository

from brain.ai.ask import ClaudeLLMClient, FakeLLMClient, answer
from brain.ai.claude_sdk import ClaudeSdkLLMClient
from brain.ai.embedder import FakeEmbedder
from brain.ai.jev import JevClient
from brain.ai.rerank import JevReranker
from brain.ai.vectorstore import QdrantVectorStore
from brain.api import deps
from brain.config import DEFAULT_CLAUDE_MODEL
from brain.main import create_app
from brain.services.ask import AskService
from brain.services.search import SearchService

MODEL = "claude-sonnet-5-5"


class FakeQuery:
    """query_fn fake yielding one canned SDK message per call; records calls."""

    def __init__(self, messages: list) -> None:
        self._messages = list(messages)
        self._lock = threading.Lock()
        self.calls: list[dict] = []

    def __call__(self, *, prompt: str, options) -> AsyncIterator:  # noqa: ANN001
        with self._lock:
            index = len(self.calls)
            self.calls.append({"prompt": prompt, "options": options})
        message = self._messages[index] if index < len(self._messages) else None

        async def gen() -> AsyncIterator:
            if message is not None:
                yield message

        return gen()


def anthropic_handler_factory(seen: dict) -> httpx.MockTransport:
    """Mock Anthropic API: records the request payload, replies text + model."""

    def handler(request: httpx.Request) -> httpx.Response:
        seen["payload"] = json.loads(request.content.decode("utf-8"))
        return httpx.Response(
            200,
            json={"content": [{"type": "text", "text": "Port 8000."}], "model": MODEL},
        )

    return httpx.MockTransport(handler)


def test_default_anthropic_model_is_current():
    """The default Anthropic model id must be the current generation."""
    assert DEFAULT_CLAUDE_MODEL == MODEL


class TestClaudeSdkModel:
    def test_configured_model_passed_to_sdk_options(self):
        from claude_agent_sdk import AssistantMessage, TextBlock

        fake = FakeQuery([AssistantMessage(content=[TextBlock(text="ok")], model=MODEL)])
        client = ClaudeSdkLLMClient(model="claude-haiku-5-5", query_fn=fake)

        client.complete("prompt")

        assert fake.calls[0]["options"].model == "claude-haiku-5-5"

    def test_reports_model_from_sdk_response_messages(self):
        from claude_agent_sdk import AssistantMessage, TextBlock

        fake = FakeQuery([AssistantMessage(content=[TextBlock(text="ok")], model=MODEL)])
        client = ClaudeSdkLLMClient(model=None, query_fn=fake)

        completion = client.complete("prompt")

        assert completion.model == MODEL

    def test_falls_back_to_configured_value_when_sdk_omits_it(self):
        from claude_agent_sdk import AssistantMessage, TextBlock

        fake = FakeQuery([AssistantMessage(content=[TextBlock(text="ok")], model="")])
        client = ClaudeSdkLLMClient(model=MODEL, query_fn=fake)

        completion = client.complete("prompt")

        assert completion.model == MODEL

    def test_falls_back_to_none_when_unconfigured_and_sdk_omits_it(self):
        from claude_agent_sdk import AssistantMessage, TextBlock

        fake = FakeQuery([AssistantMessage(content=[TextBlock(text="ok")], model="")])
        client = ClaudeSdkLLMClient(model=None, query_fn=fake)

        completion = client.complete("prompt")

        assert completion.model is None

    def test_each_completion_carries_its_own_model(self):
        """A completion must not be re-attributed by a later call's model.

        Regression guard for the shared-singleton defect: read A's result only
        after B's completion has run — A still reports A's model.
        """
        from claude_agent_sdk import AssistantMessage, TextBlock

        fake = FakeQuery(
            [
                AssistantMessage(content=[TextBlock(text="a")], model="model-A"),
                AssistantMessage(content=[TextBlock(text="b")], model="model-B"),
            ]
        )
        client = ClaudeSdkLLMClient(model=None, query_fn=fake)

        first = client.complete("prompt A")
        second = client.complete("prompt B")

        assert first.model == "model-A"
        assert second.model == "model-B"

    def test_concurrent_completions_carry_their_own_model(self):
        """Concurrent calls on the SAME client must not leak models across.

        Mirrors two concurrent /ask requests on the app-lifetime singleton:
        B's completion finishes before A's result is read; A's result still
        carries A's model.
        """
        import concurrent.futures

        from claude_agent_sdk import AssistantMessage, TextBlock

        calls = {"n": 0}
        lock = threading.Lock()

        class InterleavedQuery:
            """query_fn fake whose FIRST call waits for the second to finish."""

            def __call__(self, *, prompt: str, options) -> AsyncIterator:
                with lock:
                    calls["n"] += 1
                    n = calls["n"]

                async def gen() -> AsyncIterator:
                    model = "model-A" if n == 1 else "model-B"
                    if n == 1:
                        # Hold A's completion open until B's has fully run,
                        # reproducing the interleaving of concurrent requests.
                        with lock:
                            while calls["n"] < 2:
                                lock.release()
                                time.sleep(0.01)
                                lock.acquire()
                        yield AssistantMessage(
                            content=[TextBlock(text="a")], model=model
                        )
                    else:
                        yield AssistantMessage(
                            content=[TextBlock(text="b")], model=model
                        )

                return gen()

        client = ClaudeSdkLLMClient(model=None, query_fn=InterleavedQuery())

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            future_a = pool.submit(client.complete, "prompt A")
            future_b = pool.submit(client.complete, "prompt B")
            first = future_a.result(timeout=10)
            second = future_b.result(timeout=10)

        # Which thread is scheduled first is not deterministic — the fake tags
        # each completion by its TEXT ("a" first-called, "b" second), so map
        # model expectations to the text each Completion carries.
        by_text = {first.text: first.model, second.text: second.model}
        assert by_text["a"] == "model-A"
        assert by_text["b"] == "model-B"


class TestAnthropicModel:
    def test_configured_model_sent_in_api_payload(self):
        seen: dict = {}
        client = ClaudeLLMClient(
            api_key="test-key", model=MODEL, transport=anthropic_handler_factory(seen)
        )

        client.complete("prompt")

        assert seen["payload"]["model"] == MODEL

    def test_reports_model_from_api_response(self):
        seen: dict = {}
        client = ClaudeLLMClient(
            api_key="test-key", model=MODEL, transport=anthropic_handler_factory(seen)
        )

        completion = client.complete("prompt")

        assert completion.model == MODEL


def test_answer_includes_model_that_answered():
    result = answer("q", [], llm=FakeLLMClient(reply="fine", model=MODEL))

    assert result["model"] == MODEL


def test_ask_response_includes_model_that_answered():
    """POST /ask exposes the model that actually answered."""
    store = QdrantVectorStore(client=QdrantClient(":memory:"))
    store.ensure_collection(8)
    app = create_app()
    app.dependency_overrides[deps.get_ask_service] = lambda: AskService(
        SearchService(
            FakeRepository(),
            FakeEmbedder(dim=8),
            store,
            JevReranker(JevClient(api_key=None)),
        ),
        FakeLLMClient(reply="[item-1] noted", model="claude-haiku-5-5"),
    )
    client = TestClient(app)

    response = client.post("/ask", json={"question": "anything"})

    assert response.status_code == 200, response.text
    assert response.json()["model"] == "claude-haiku-5-5"
