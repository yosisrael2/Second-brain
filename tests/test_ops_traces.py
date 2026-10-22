"""Trace tests: capture dry-run saves nothing; search/ask traces report steps.

All AI dependencies are fake-backed (tests/fakes + wiring-style in-memory
qdrant): the suite must never need API keys, a Claude login, or a model
download — the traces' contract is their STRUCTURE (steps, details), which
these tests pin.
"""

import pytest
from fastapi.testclient import TestClient
from qdrant_client import QdrantClient
from sqlalchemy import text
from tests.fakes import FULL_CLASSIFICATION, StubClassifier
from tests.test_search_service import StubReranker

from brain.ai.ask import FakeLLMClient
from brain.ai.embedder import FakeEmbedder
from brain.ai.vectorstore import QdrantVectorStore
from brain.api import deps
from brain.db.session import engine


@pytest.fixture(name="clean_items")
def clean_items_fixture(database: None):
    url = str(engine.url)
    assert url.rsplit("/", 1)[-1].endswith("_test"), f"not a test db: {url}"
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM memory_items"))
        conn.execute(text("DELETE FROM ops_events"))
    yield
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM memory_items"))
        conn.execute(text("DELETE FROM ops_events"))


@pytest.fixture(name="fake_ai")
def fake_ai_fixture(monkeypatch: pytest.MonkeyPatch):
    """Point the traces' direct deps.* calls at offline fakes."""
    store = QdrantVectorStore(client=QdrantClient(":memory:"))
    store.ensure_collection(8)
    monkeypatch.setattr(deps, "get_embedder", lambda: FakeEmbedder(dim=8))
    monkeypatch.setattr(deps, "get_vector_store", lambda: store)
    monkeypatch.setattr(deps, "get_reranker", lambda: StubReranker())
    monkeypatch.setattr(
        deps, "get_classifier", lambda: StubClassifier(FULL_CLASSIFICATION)
    )
    monkeypatch.setattr(deps, "get_llm_client", lambda: FakeLLMClient(reply="see sources"))
    return store


class TestCaptureTrace:
    def test_reports_parse_classify_embed_steps(self, client: TestClient, clean_items: None,
                                                fake_ai):
        resp = client.post("/ops/trace/capture", json={"text": "Remember to water the plants"})
        assert resp.status_code == 200
        body = resp.json()
        steps = {step["step"] for step in body["steps"]}
        assert {"parse", "classify", "embed", "qdrant"} <= steps
        parse_step = next(s for s in body["steps"] if s["step"] == "parse")
        assert parse_step["ok"] is True
        assert parse_step["detail"]["kind"] in {"note", "task", "email", "whatsapp", "calendar"}

    def test_capture_trace_saves_nothing(self, client: TestClient, clean_items: None, fake_ai):
        with engine.begin() as conn:
            before = conn.execute(text("SELECT count(*) FROM memory_items")).scalar_one()
        client.post("/ops/trace/capture", json={"text": "dry run must not persist"})
        with engine.begin() as conn:
            after = conn.execute(text("SELECT count(*) FROM memory_items")).scalar_one()
        assert before == after == 0

    def test_capture_trace_reports_qdrant_neighbours(self, client: TestClient,
                                                     clean_items: None, fake_ai):
        resp = client.post("/ops/trace/capture", json={"text": "note for tracing"})
        neighbours = resp.json()["neighbours"]
        assert isinstance(neighbours, list)  # empty store -> []; shape is the contract


class TestSearchTrace:
    def test_reports_embedding_and_candidates(self, client: TestClient, clean_items: None,
                                              fake_ai):
        resp = client.get("/ops/trace/search", params={"q": "plants"})
        assert resp.status_code == 200
        body = resp.json()
        steps = {step["step"] for step in body["steps"]}
        assert {"embed", "qdrant", "rerank"} <= steps
        rerank = next(s for s in body["steps"] if s["step"] == "rerank")
        assert "before" in rerank["detail"] and "after" in rerank["detail"]

    def test_reports_keyword_fallback_decision(self, client: TestClient, clean_items: None,
                                               fake_ai):
        body = client.get("/ops/trace/search", params={"q": "anything"}).json()
        fallback = next(s for s in body["steps"] if s["step"] == "keyword_fallback")
        assert fallback["detail"]["taken"] in {True, False}

    def test_fallback_taken_when_embedder_down(self, client: TestClient, clean_items: None,
                                               monkeypatch):
        from brain.ai.embedder import EmbedderError

        class BrokenEmbedder:
            def embed(self, text: str) -> list[float]:
                raise EmbedderError("embedder down")

        monkeypatch.setattr(deps, "get_embedder", lambda: BrokenEmbedder())
        body = client.get("/ops/trace/search", params={"q": "plants"}).json()
        fallback = next(s for s in body["steps"] if s["step"] == "keyword_fallback")
        assert fallback["detail"]["taken"] is True
        assert "embed failed" in (fallback["detail"]["reason"] or "")


class TestAskTrace:
    def test_reports_sources_prompt_and_answer(self, client: TestClient, clean_items: None,
                                               fake_ai):
        body = client.post("/ops/trace/ask", json={"question": "what?"}).json()
        steps = {step["step"] for step in body["steps"]}
        assert {"search", "prompt", "llm"} <= steps
        prompt = next(s for s in body["steps"] if s["step"] == "prompt")
        assert "sources" in prompt["detail"]
        llm_step = next(s for s in body["steps"] if s["step"] == "llm")
        assert llm_step["ok"] is True and "answer" in llm_step["detail"]
