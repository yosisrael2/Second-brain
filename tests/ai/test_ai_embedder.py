"""Tests for brain.ai.embedder: FakeEmbedder determinism and OpenAI adapter."""

import json
import math
import sys
import types

import httpx
import pytest

from brain.ai.embedder import EmbedderError, FakeEmbedder, FastEmbedEmbedder, OpenAIEmbedder


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

    def test_embed_batch_matches_per_item_embeds(self):
        """embed_batch returns exactly the per-item vectors, in order."""
        embedder = FakeEmbedder(dim=32)
        texts = ["first note", "second note", "third note"]

        batched = embedder.embed_batch(texts)

        assert batched == [embedder.embed(text) for text in texts]


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

    def test_embed_batch_sends_one_request_and_returns_vectors_in_order(self):
        """Batched embedding: one POST with the input list, vectors in order."""
        seen = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["requests"] = seen.get("requests", 0) + 1
            seen["body"] = json.loads(request.content.decode("utf-8"))
            return httpx.Response(
                200,
                json={
                    "data": [
                        {"index": 0, "embedding": [1.0, 0.0]},
                        {"index": 1, "embedding": [0.0, 1.0]},
                        {"index": 2, "embedding": [0.5, 0.5]},
                    ]
                },
            )

        embedder = OpenAIEmbedder(api_key="k", transport=httpx.MockTransport(handler))
        vectors = embedder.embed_batch(["a", "b", "c"])

        assert seen["requests"] == 1
        assert seen["body"] == {"model": "text-embedding-3-small", "input": ["a", "b", "c"]}
        assert vectors == [[1.0, 0.0], [0.0, 1.0], [0.5, 0.5]]

    def test_base_url_override(self):
        seen = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["url"] = str(request.url)
            return httpx.Response(200, json={"data": [{"embedding": [1.0]}]})

        embedder = OpenAIEmbedder(
            api_key="k", base_url="https://proxy.example/v1", transport=httpx.MockTransport(handler)
        )
        embedder.embed("text")

        assert seen["url"] == "https://proxy.example/v1/embeddings"

    def test_dim_matches_known_models(self):
        assert OpenAIEmbedder(api_key="k").dim == 1536
        assert OpenAIEmbedder(api_key="k", model="text-embedding-3-large").dim == 3072

        with pytest.raises(EmbedderError, match="unknown embedding model"):
            OpenAIEmbedder(api_key="k", model="mystery-model").dim  # noqa: B018

    def test_close_releases_the_http_client(self):
        embedder = OpenAIEmbedder(api_key="k")

        embedder.close()  # must not raise

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


class _FakeBackend:
    """Stand-in for fastembed.TextEmbedding returning deterministic vectors."""

    def __init__(self, dim: int = 3) -> None:
        self._dim = dim
        self.batched: list[list[str]] = []

    def embed(self, texts: list[str]) -> list[list[float]]:
        self.batched.append(texts)
        return [[float(len(text)), 0.5, -0.25][: self._dim] for text in texts]


class TestFastEmbedEmbedder:
    def test_embed_and_dim(self, monkeypatch):
        backend = _FakeBackend(dim=3)

        class FakeTextEmbedding:
            def __init__(self, model_name: str) -> None:
                assert model_name == "minishlab/potion-multilingual-128M"
                self._inner = backend

            def embed(self, texts: list[str]) -> list[list[float]]:
                return self._inner.embed(texts)

        monkeypatch.setitem(
            sys.modules, "fastembed", types.SimpleNamespace(TextEmbedding=FakeTextEmbedding)
        )
        embedder = FastEmbedEmbedder()

        assert embedder.embed("kubernetes rollout") == [18.0, 0.5, -0.25]
        assert embedder.dim == 3  # probed lazily via one embed call
        assert backend.batched[-1] == [""]  # the dim probe embedded one empty string

    def test_missing_library_maps_to_embedder_error(self, monkeypatch):
        original = sys.modules.pop("fastembed", None)
        monkeypatch.setitem(sys.modules, "fastembed", None)  # import raises ImportError
        try:
            with pytest.raises(EmbedderError, match="fastembed is not installed"):
                FastEmbedEmbedder()
        finally:
            if original is not None:
                sys.modules["fastembed"] = original

    def test_model_load_failure_maps_to_embedder_error(self, monkeypatch):
        class BrokenTextEmbedding:
            def __init__(self, model_name: str) -> None:
                raise RuntimeError("download failed")

        monkeypatch.setitem(
            sys.modules, "fastembed", types.SimpleNamespace(TextEmbedding=BrokenTextEmbedding)
        )

        with pytest.raises(EmbedderError, match="failed to load fastembed model"):
            FastEmbedEmbedder()

    def test_inference_failure_maps_to_embedder_error(self, monkeypatch):
        class BrokenBackend:
            def embed(self, texts: list[str]) -> list[list[float]]:
                raise RuntimeError("onnx exploded")

        class FakeTextEmbedding:
            def __init__(self, model_name: str) -> None:
                self._inner = BrokenBackend()

            def embed(self, texts: list[str]) -> list[list[float]]:
                return self._inner.embed(texts)

        monkeypatch.setitem(
            sys.modules, "fastembed", types.SimpleNamespace(TextEmbedding=FakeTextEmbedding)
        )
        embedder = FastEmbedEmbedder()

        with pytest.raises(EmbedderError, match="fastembed embedding failed"):
            embedder.embed("text")
