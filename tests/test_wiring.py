"""End-to-end wiring tests: routers + DI with fake-backed infrastructure."""

import re

import pytest
from fastapi.testclient import TestClient
from qdrant_client import QdrantClient
from tests.fakes import FULL_CLASSIFICATION, FakeRepository, StubClassifier

from brain.ai.ask import FakeLLMClient
from brain.ai.classify import FALLBACK_CLASSIFICATION
from brain.ai.embedder import FakeEmbedder
from brain.ai.vectorstore import QdrantVectorStore
from brain.api import deps
from brain.db.enums import ItemStatus
from brain.main import create_app


@pytest.fixture(name="wired")
def wired_fixture():
    """App with every AI dependency replaced by offline fakes."""
    repo = FakeRepository()
    store = QdrantVectorStore(client=QdrantClient(":memory:"))
    store.ensure_collection(8)

    app = create_app()
    app.dependency_overrides[deps.get_items_repository] = lambda: repo
    app.dependency_overrides[deps.get_classifier] = lambda: StubClassifier(FULL_CLASSIFICATION)
    app.dependency_overrides[deps.get_embedder] = lambda: FakeEmbedder(dim=8)
    app.dependency_overrides[deps.get_vector_store] = lambda: store
    app.dependency_overrides[deps.get_llm_client] = lambda: FakeLLMClient(reply="see sources")
    yield app, repo, store
    app.dependency_overrides.clear()


def test_post_items_classifies_and_routes(wired):
    app, repo, _ = wired
    client = TestClient(app)

    response = client.post("/items", json={"raw_text": "Renew the TLS certificate"})

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["type"] == "task"
    assert body["status"] == ItemStatus.FILED.value
    assert body["confidence"] == pytest.approx(0.95)


def test_post_items_classify_false_keeps_inbox(wired):
    app, repo, _ = wired
    client = TestClient(app)

    response = client.post("/items?classify=false", json={"raw_text": "raw capture"})

    assert response.status_code == 201
    body = response.json()
    assert body["type"] is None
    assert body["status"] == ItemStatus.INBOX.value


def test_post_items_jev_failure_still_captures(wired):
    app, _, _ = wired
    app.dependency_overrides[deps.get_classifier] = lambda: StubClassifier(FALLBACK_CLASSIFICATION)
    client = TestClient(app)

    response = client.post("/items", json={"raw_text": "captured anyway"})

    assert response.status_code == 201
    assert response.json()["status"] == ItemStatus.INBOX.value


def test_get_search_returns_ranked_items_with_scores(wired):
    app, repo, _ = wired
    client = TestClient(app)
    created = client.post(
        "/items?classify=false", json={"raw_text": "kubernetes rollout runbook"}
    ).json()
    client.post("/items?classify=false", json={"raw_text": "banana bread recipe"})

    response = client.get("/search", params={"q": "kubernetes rollout", "limit": 2})

    assert response.status_code == 200
    results = response.json()
    assert len(results) >= 1
    assert results[0]["id"] == created["id"]
    assert results[0]["score"] is not None


def test_get_search_q_required(wired):
    client = TestClient(wired[0])

    response = client.get("/search")

    assert response.status_code == 422  # q is mandatory


def test_post_ask_answers_with_citations(wired):
    app, _, _ = wired
    created = TestClient(app).post(
        "/items?classify=false", json={"raw_text": "The server runs on port 8000."}
    ).json()
    app.dependency_overrides[deps.get_llm_client] = lambda: FakeLLMClient(
        reply=lambda prompt: "Port: "
        + " ".join(f"[{m}]" for m in re.findall(r"- \[([^\]]+)\]", prompt))
    )
    client = TestClient(app)

    response = client.post("/ask", json={"question": "what port does the server run on?"})

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["answer"].startswith("Port:")
    assert body["cited_ids"] == [created["id"]]
    assert body["sources"][0]["id"] == created["id"]


def test_post_ask_llm_down_returns_502(wired):
    from brain.ai.ask import LLMError

    class ExplodingLLM:
        def complete(self, prompt: str) -> str:
            raise LLMError("llm down")

    app, _, _ = wired
    app.dependency_overrides[deps.get_llm_client] = lambda: ExplodingLLM()
    client = TestClient(app)

    response = client.post("/ask", json={"question": "anything"})

    assert response.status_code == 502


def test_existing_items_contract_still_green(wired):
    app, repo, _ = wired
    client = TestClient(app)

    created = client.post("/items?classify=false", json={"raw_text": "plain item"}).json()
    listed = client.get("/items").json()
    fetched = client.get(f"/items/{created['id']}").json()
    patched = client.patch(f"/items/{created['id']}", json={"project": "ops"}).json()
    deleted = client.delete(f"/items/{created['id']}")

    assert created["status"] == "inbox"
    assert [item["id"] for item in listed] == [created["id"]]
    assert fetched["id"] == created["id"]
    assert patched["project"] == "ops"
    assert deleted.status_code == 204
    assert repo.items == {}
