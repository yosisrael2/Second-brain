"""Tests for brain.ai.embedder: FakeEmbedder determinism and OpenAI adapter."""

import json
import math

import httpx
import pytest

from brain.ai.embedder import EmbedderError, FakeEmbedder, OpenAIEmbedder


def cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    return dot / (math.sqrt(sum(x * x for x in a)) * math.sqrt(sum(y * y for y in b)))


class TestFakeEmbedder:
    def test_same_text_yields_identical_vector(self):
        embedder = FakeEmbedder(dim=32)

        first = embedder.embed("meeting notes from standup")
        second = embedder.embed("meeting notes from standup")

        assert first == second

    def test_vectors_are_unit_length(self):
        vector = FakeEmbedder(dim=32).embed("some text")

        assert math.isclose(sum(component**2 for component in vector), 1.0, rel_tol=1e-9)

    def test_dim_property_matches_vector_length(self):
        embedder = FakeEmbedder(dim=16)

        assert embedder.dim == 16
        assert len(embedder.embed("text")) == 16

    def test_empty_text_yields_zero_vector(self):
        assert FakeEmbedder(dim=8).embed("") == [0.0] * 8

    def test_similar_texts_closer_than_unrelated_ones(self):
        embedder = FakeEmbedder(dim=64)
        base = embedder.embed("quarterly revenue report for the team")
        similar = embedder.embed("quarterly revenue report for the team q3")
        unrelated = embedder.embed("recipe for banana bread")

        assert cosine(base, similar) > cosine(base, unrelated)


class TestOpenAIEmbedder:
    def test_sends_model_and_input_with_bearer_key(self):
        seen = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["url"] = str(request.url)
            seen["auth"] = request.headers.get("Authorization")
            seen["body"] = json.loads(request.content.decode("utf-8"))
            return httpx.Response(200, json={"data": [{"embedding": [0.5, -0.5, 1.0]}]})

        embedder = OpenAIEmbedder(api_key="k", transport=httpx.MockTransport(handler))
        vector = embedder.embed("hello")

        assert seen["url"].endswith("/v1/embeddings")
        assert seen["auth"] == "Bearer k"
        assert seen["body"] == {"model": "text-embedding-3-small", "input": "hello"}
        assert vector == [0.5, -0.5, 1.0]

    def test_base_url_env_override(self, monkeypatch):
        monkeypatch.setenv("OPENAI_BASE_URL", "https://proxy.example/v1")
        seen = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["url"] = str(request.url)
            return httpx.Response(200, json={"data": [{"embedding": [1.0]}]})

        embedder = OpenAIEmbedder(api_key="k", transport=httpx.MockTransport(handler))
        embedder.embed("text")

        assert seen["url"] == "https://proxy.example/v1/embeddings"

    def test_missing_api_key_fails_before_any_request(self):
        calls = []

        def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover
            calls.append(request)
            return httpx.Response(200, json={})

        embedder = OpenAIEmbedder(api_key="", transport=httpx.MockTransport(handler))

        with pytest.raises(EmbedderError, match="no API key"):
            embedder.embed("text")
        assert calls == []

    def test_http_error_maps_to_embedder_error(self):
        handler = lambda request: httpx.Response(503, text="down")  # noqa: E731
        embedder = OpenAIEmbedder(api_key="k", transport=httpx.MockTransport(handler))

        with pytest.raises(EmbedderError, match="request failed"):
            embedder.embed("text")

    @pytest.mark.parametrize(
        "payload",
        [
            {"data": []},
            {"data": [{}]},
            {"data": [{"embedding": "not-a-list"}]},
            {"data": [{"embedding": [1, "two"]}]},
            "not-json",
        ],
    )
    def test_malformed_responses_map_to_embedder_error(self, payload):
        def handler(request: httpx.Request) -> httpx.Response:
            if isinstance(payload, str):
                return httpx.Response(200, text=payload)
            return httpx.Response(200, json=payload)

        embedder = OpenAIEmbedder(api_key="k", transport=httpx.MockTransport(handler))

        with pytest.raises(EmbedderError, match="malformed"):
            embedder.embed("text")


def test_fake_embedder_satisfies_the_protocol():
    from brain.ai.embedder import Embedder

    embedder: Embedder = FakeEmbedder()
    assert isinstance(embedder.embed("x"), list)
