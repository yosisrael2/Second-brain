"""Embedding strategies: OpenAI-backed and a deterministic offline fake."""

from __future__ import annotations

import hashlib
import os
import re
from typing import Protocol

import httpx

from ._http import post_json

DEFAULT_OPENAI_URL = "https://api.openai.com/v1"
DEFAULT_EMBEDDING_MODEL = "text-embedding-3-small"
DEFAULT_OPENAI_TIMEOUT_S = 30.0
FAKE_EMBED_DIM = 64
_GRAM = re.compile(r"[a-z0-9]+")


class EmbedderError(RuntimeError):
    """An embedding request failed or returned an unusable payload."""


class Embedder(Protocol):
    """Strategy interface: turns text into a fixed-length vector."""

    def embed(self, text: str) -> list[float]:
        """Return the embedding vector for ``text``."""
        ...


class OpenAIEmbedder:
    """Strategy producing embeddings via the OpenAI ``/embeddings`` endpoint.

    Key/endpoint come from ``OPENAI_API_KEY`` / ``OPENAI_BASE_URL`` unless
    overridden. Network or payload failures raise :class:`EmbedderError`.
    """

    def __init__(
        self,
        model: str = DEFAULT_EMBEDDING_MODEL,
        api_key: str | None = None,
        base_url: str | None = None,
        timeout_s: float = DEFAULT_OPENAI_TIMEOUT_S,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._model = model
        self._api_key = api_key
        self._url = (
            (base_url or os.getenv("OPENAI_BASE_URL") or DEFAULT_OPENAI_URL).rstrip("/")
            + "/embeddings"
        )
        self._timeout_s = timeout_s
        self._http = httpx.Client(timeout=timeout_s, transport=transport)

    def embed(self, text: str) -> list[float]:
        """Return the embedding vector for ``text``."""
        api_key = self._api_key if self._api_key is not None else os.getenv("OPENAI_API_KEY")
        if not api_key:
            raise EmbedderError("no API key: set OPENAI_API_KEY (or pass api_key)")
        response = self._post(api_key, text)
        return self._extract_vector(response)

    def _post(self, api_key: str, text: str) -> httpx.Response:
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
    def _extract_vector(response: httpx.Response) -> list[float]:
        try:
            vector = response.json()["data"][0]["embedding"]
            if not isinstance(vector, list) or not vector:
                raise TypeError("embedding is not a non-empty list")
            return [float(component) for component in vector]
        except (TypeError, ValueError, KeyError, IndexError) as exc:
            raise EmbedderError(f"malformed embedding response: {exc!r}") from exc


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

    def embed(self, text: str) -> list[float]:
        """Return the deterministic embedding vector for ``text``."""
        vector = [0.0] * self._dim
        for feature in self._features(text):
            digest = hashlib.sha256(feature.encode("utf-8")).digest()
            index = int.from_bytes(digest[:4], "big") % self._dim
            sign = 1.0 if digest[4] % 2 == 0 else -1.0
            vector[index] += sign
        return _normalize(vector)

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
