"""Trace tests: capture dry-run saves nothing; search/ask traces report steps."""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

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


class TestCaptureTrace:
    def test_reports_parse_classify_embed_steps(self, client: TestClient, clean_items: None):
        resp = client.post("/ops/trace/capture", json={"text": "Remember to water the plants"})
        assert resp.status_code == 200
        body = resp.json()
        steps = {step["step"] for step in body["steps"]}
        assert {"parse", "classify", "embed", "qdrant"} <= steps
        parse_step = next(s for s in body["steps"] if s["step"] == "parse")
        assert parse_step["ok"] is True
        assert parse_step["detail"]["kind"] in {"note", "task", "email", "whatsapp", "calendar"}

    def test_capture_trace_saves_nothing(self, client: TestClient, clean_items: None):
        with engine.begin() as conn:
            before = conn.execute(text("SELECT count(*) FROM memory_items")).scalar_one()
        client.post("/ops/trace/capture", json={"text": "dry run must not persist"})
        with engine.begin() as conn:
            after = conn.execute(text("SELECT count(*) FROM memory_items")).scalar_one()
        assert before == after == 0


class TestSearchTrace:
    def test_reports_embedding_and_candidates(self, client: TestClient, clean_items: None):
        resp = client.get("/ops/trace/search", params={"q": "plants"})
        assert resp.status_code == 200
        body = resp.json()
        steps = {step["step"] for step in body["steps"]}
        assert {"embed", "qdrant", "rerank"} <= steps
        rerank = next(s for s in body["steps"] if s["step"] == "rerank")
        assert "before" in rerank["detail"] and "after" in rerank["detail"]

    def test_reports_keyword_fallback_decision(self, client: TestClient, clean_items: None):
        body = client.get("/ops/trace/search", params={"q": "anything"}).json()
        fallback = next(s for s in body["steps"] if s["step"] == "keyword_fallback")
        assert fallback["detail"]["taken"] in {True, False}


class TestAskTrace:
    def test_reports_sources_prompt_and_answer(self, client: TestClient, clean_items: None):
        body = client.post("/ops/trace/ask", json={"question": "what?"}).json()
        steps = {step["step"] for step in body["steps"]}
        assert {"search", "prompt", "llm"} <= steps
        prompt = next(s for s in body["steps"] if s["step"] == "prompt")
        assert "sources" in prompt["detail"]
        llm_step = next(s for s in body["steps"] if s["step"] == "llm")
        assert "answer" in llm_step["detail"]
