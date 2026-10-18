from __future__ import annotations

import hashlib
import re
from collections.abc import Sequence
from typing import Any, Protocol

import httpx

from brain.config import (
    DEFAULT_EMBEDDING_MODEL,
    DEFAULT_FASTEMBED_MODEL,
    DEFAULT_OPENAI_TIMEOUT_S,
    DEFAULT_OPENAI_URL,
)

from ._http import post_json

FAKE_EMBED_DIM = 64
_GRAM = re.compile(r"[a-z0-9]+")

# Vector sizes per OpenAI embedding model (per OpenAI docs).
_MODEL_DIMS: dict[str, int] = {
    "text-embedding-3-small": 1536,
    "text-embedding-3-large": 3072,
    "text-embedding-ada-002": 1536,
}


class EmbedderError(RuntimeError):
    """An embedding request failed or returned an unusable payload."""


class Embedder(Protocol):
    """Strategy interface: turns text into a fixed-length vector."""

    def embed(self, text: str) -> list[float]:
        """Return the embedding vector for ``text``."""
        ...

    def embed_batch(self, texts: Sequence[str]) -> list[list[float]]:
        """Return the embedding vectors for ``texts``, one per input, in order.

        Implementations should issue a single backend call for the whole
        batch (this is what ``brain reindex`` batching relies on).
        """
        ...

    def close(self) -> None:
        """Release underlying connections (no-op for in-memory fakes)."""
        ...


class OpenAIEmbedder:
    """Strategy producing embeddings via the OpenAI ``/embeddings`` endpoint.

    Key/endpoint come from ``OPENAI_API_KEY`` / ``OPENAI_BASE_URL`` unless
    overridden. Network or payload failures raise :class:`EmbedderError`.
    """

    def __init__(
        self,
        api_key: str | None = None,
        model: str = DEFAULT_EMBEDDING_MODEL,
        base_url: str = DEFAULT_OPENAI_URL,
        timeout_s: float = DEFAULT_OPENAI_TIMEOUT_S,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._model = model
        self._api_key = api_key
        self._url = base_url.rstrip("/") + "/embeddings"
        self._timeout_s = timeout_s
        self._http = httpx.Client(timeout=timeout_s, transport=transport)

    @property
    def dim(self) -> int:
        """Vector size for the configured embedding model."""
        try:
            return _MODEL_DIMS[self._model]
        except KeyError as exc:
            raise EmbedderError(
                f"unknown embedding model {self._model!r}; known: {sorted(_MODEL_DIMS)}"
            ) from exc

    def close(self) -> None:
        """Release the underlying HTTP connection pool."""
        self._http.close()

    def embed(self, text: str) -> list[float]:
        """Return the embedding vector for ``text``."""
        if not self._api_key:
            raise EmbedderError("no API key configured for embeddings (OPENAI_API_KEY)")
        response = self._post(self._api_key, text)
        return self._extract_vector(response)

    def embed_batch(self, texts: Sequence[str]) -> list[list[float]]:
        """Return one embedding per input text, in order, from a single request."""
        if not self._api_key:
            raise EmbedderError("no API key configured for embeddings (OPENAI_API_KEY)")
        response = self._post(self._api_key, list(texts))
        return self._extract_vectors(response)

    def _post(self, api_key: str, text: str | list[str]) -> httpx.Response:
        return post_json(
            self._http,
            self._url,
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            payload={"model": self._model, "input": text},
            error=EmbedderError,
            context="embedding",
            timeout_s=self._timeout_s,
        )

    @staticmethod
    def _extract_vectors(response: httpx.Response) -> list[list[float]]:
        try:
            data = response.json()["data"]
            if not isinstance(data, list) or not data:
                raise TypeError("embedding response has no 'data' entries")
            return [
                [float(component) for component in entry["embedding"]]
                for entry in data
            ]
        except (TypeError, ValueError, KeyError, IndexError) as exc:
            raise EmbedderError(f"malformed embedding response: {exc!r}") from exc

    @staticmethod
    def _extract_vector(response: httpx.Response) -> list[float]:
        try:
            vector = response.json()["data"][0]["embedding"]
            if not isinstance(vector, list) or not vector:
                raise TypeError("embedding is not a non-empty list")
            return [float(component) for component in vector]
        except (TypeError, ValueError, KeyError, IndexError) as exc:
            raise EmbedderError(f"malformed embedding response: {exc!r}") from exc


class FastEmbedEmbedder:
    """Strategy producing local, offline embeddings via the fastembed library.

    The model (default ``BAAI/bge-small-en-v1.5``, dim 384) is downloaded on
    first use and cached by fastembed; afterwards it runs fully offline. Load
    or inference failures raise :class:`EmbedderError`.
    """

    def __init__(self, model: str = DEFAULT_FASTEMBED_MODEL) -> None:
        self._model = model
        self._backend = self._load_model(model)
        self._dim: int | None = None

    @staticmethod
    def _load_model(model: str) -> Any:
        try:
            from fastembed import TextEmbedding
        except ImportError as exc:
            raise EmbedderError(
                "fastembed is not installed; install it to use EMBEDDER_PROVIDER=fastembed"
            ) from exc
        try:
            return TextEmbedding(model_name=model)
        except Exception as exc:
            # Deliberate boundary: fastembed/model downloads raise many types
            # (network, onnxruntime, OSError); callers only see EmbedderError.
            raise EmbedderError(f"failed to load fastembed model {model!r}: {exc}") from exc

    @property
    def dim(self) -> int:
        """Vector size of the loaded model (probed once)."""
        if self._dim is None:
            self._dim = len(self.embed(""))
        return self._dim

    def close(self) -> None:
        """No-op: the model is plain in-process memory."""

    def embed(self, text: str) -> list[float]:
        """Return the embedding vector for ``text``."""
        try:
            vectors = list(self._backend.embed([text]))
        except Exception as exc:
            # Boundary as above: normalize every fastembed failure.
            raise EmbedderError(f"fastembed embedding failed: {exc}") from exc
        if not vectors:
            raise EmbedderError("fastembed returned no vectors")
        return [float(component) for component in vectors[0]]

    def embed_batch(self, texts: Sequence[str]) -> list[list[float]]:
        """Return one embedding per input text, in order, in one model call."""
        try:
            vectors = list(self._backend.embed(list(texts)))
        except Exception as exc:
            # Boundary as above: normalize every fastembed failure.
            raise EmbedderError(f"fastembed embedding failed: {exc}") from exc
        if len(vectors) != len(texts):
            raise EmbedderError(
                f"fastembed returned {len(vectors)} vectors for {len(texts)} inputs"
            )
        return [[float(component) for component in vector] for vector in vectors]


class FakeEmbedder:
    """Deterministic offline Embedder strategy for tests.

    Hashes word tokens and character 3-grams into a fixed number of buckets
    with signed weights, then L2-normalizes. Same text always yields the same
    vector; texts sharing vocabulary land close together.
    """

    def __init__(self, dim: int = FAKE_EMBED_DIM) -> None:
        self._dim = dim

    @property
    def dim(self) -> int:
        return self._dim

    def close(self) -> None:
        """No-op: mirrors the real embedder's lifecycle method."""

    def embed(self, text: str) -> list[float]:
        """Return the deterministic embedding vector for ``text``."""
        vector = [0.0] * self._dim
        for feature in self._features(text):
            digest = hashlib.sha256(feature.encode("utf-8")).digest()
            index = int.from_bytes(digest[:4], "big") % self._dim
            sign = 1.0 if digest[4] % 2 == 0 else -1.0
            vector[index] += sign
        return _normalize(vector)

    def embed_batch(self, texts: Sequence[str]) -> list[list[float]]:
        """Return the per-item embeddings, in order (same vectors as ``embed``)."""
        return [self.embed(text) for text in texts]

    @staticmethod
    def _features(text: str) -> set[str]:
        features: set[str] = set()
        for token in _GRAM.findall(text.lower()):
            features.add(token)
            padded = f"<{token}>"
            features.update(padded[i : i + 3] for i in range(len(padded) - 2))
        return features


def _normalize(vector: list[float]) -> list[float]:
    norm = sum(component**2 for component in vector) ** 0.5
    if norm == 0.0:
        return vector
    return [component / norm for component in vector]
