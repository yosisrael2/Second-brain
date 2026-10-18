"""Model configurability and visibility for /ask.

Covers: the default Anthropic model id, model pass-through to the SDK/API
call, and observability — the model that actually answered is reported by the
clients and included in the /ask response. All offline (fake query_fn /
httpx.MockTransport / in-memory Qdrant / fake repo).
"""

import json
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
    """query_fn fake yielding canned SDK messages; records calls."""

    def __init__(self, messages: list) -> None:
        self._messages = messages
        self.calls: list[dict] = []

    def __call__(self, *, prompt: str, options) -> AsyncIterator:  # noqa: ANN001
        self.calls.append({"prompt": prompt, "options": options})

        async def gen() -> AsyncIterator:
            for message in self._messages:
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

        client.complete("prompt")

        assert client.model == MODEL

    def test_falls_back_to_configured_value_when_sdk_omits_it(self):
        from claude_agent_sdk import AssistantMessage, TextBlock

        fake = FakeQuery([AssistantMessage(content=[TextBlock(text="ok")], model="")])
        client = ClaudeSdkLLMClient(model=MODEL, query_fn=fake)

        client.complete("prompt")

        assert client.model == MODEL

    def test_falls_back_to_none_when_unconfigured_and_sdk_omits_it(self):
        from claude_agent_sdk import AssistantMessage, TextBlock

        fake = FakeQuery([AssistantMessage(content=[TextBlock(text="ok")], model="")])
        client = ClaudeSdkLLMClient(model=None, query_fn=fake)

        client.complete("prompt")

        assert client.model is None


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

        client.complete("prompt")

        assert client.model == MODEL


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
