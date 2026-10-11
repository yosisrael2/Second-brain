"""Vector store repository backed by Qdrant."""

from __future__ import annotations

import os
import uuid
from collections.abc import Mapping
from typing import Any, Protocol

from qdrant_client import QdrantClient, models

DEFAULT_COLLECTION = "brain_items"
_QDRANT_TIMEOUT_S = 10  # seconds; QdrantClient takes int|None


class VectorStoreError(RuntimeError):
    """The vector store could not be reached or returned an unusable result."""


class VectorStore(Protocol):
    """Repository interface for storing and searching item vectors."""

    def ensure_collection(self, dim: int) -> None:
        """Create the collection (cosine distance) if it does not exist."""
        ...

    def upsert(self, item_id: str | int, vector: list[float], payload: Mapping[str, Any]) -> None:
        """Insert or replace one item; ``payload`` is stored alongside it."""
        ...

    def search(self, vector: list[float], k: int = 5) -> list[dict[str, Any]]:
        """Return the ``k`` nearest items as ``{id, score, payload}`` dicts."""
        ...


class QdrantVectorStore:
    """Repository (Adapter) implementing VectorStore on top of Qdrant.

    Connection comes from ``QDRANT_URL`` / ``QDRANT_API_KEY`` unless a
    ready-made ``QdrantClient`` is injected (tests use an in-memory client).
    Non-integer, non-UUID item ids are mapped to a deterministic UUID point
    id; the original id is always kept in the stored payload and returned by
    :meth:`search`.
    """

    def __init__(
        self,
        url: str | None = None,
        api_key: str | None = None,
        collection: str | None = None,
        client: QdrantClient | None = None,
    ) -> None:
        self._collection = collection or os.getenv("QDRANT_COLLECTION") or DEFAULT_COLLECTION
        self._client = client or self._build_client(url, api_key)

    @staticmethod
    def _build_client(url: str | None, api_key: str | None) -> QdrantClient:
        url = url or os.getenv("QDRANT_URL")
        if not url:
            raise VectorStoreError("no Qdrant URL: set QDRANT_URL (or pass url/client)")
        return QdrantClient(
            url=url,
            api_key=api_key or os.getenv("QDRANT_API_KEY"),
            timeout=_QDRANT_TIMEOUT_S,
        )

    def ensure_collection(self, dim: int) -> None:
        """Create the collection with cosine distance unless it exists."""
        if self._client.collection_exists(self._collection):
            return
        self._client.create_collection(
            self._collection,
            vectors_config=models.VectorParams(size=dim, distance=models.Distance.COSINE),
        )

    def upsert(self, item_id: str | int, vector: list[float], payload: Mapping[str, Any]) -> None:
        """Insert or replace one item under a deterministic point id."""
        stored_payload = dict(payload)
        stored_payload.setdefault("id", item_id)
        point = models.PointStruct(id=_point_id(item_id), vector=vector, payload=stored_payload)
        self._client.upsert(self._collection, points=[point])

    def search(self, vector: list[float], k: int = 5) -> list[dict[str, Any]]:
        """Return the ``k`` nearest items, best match first."""
        result = self._client.query_points(
            self._collection, query=vector, limit=k, with_payload=True
        )
        return [
            {
                "id": (point.payload or {}).get("id", point.id),
                "score": float(point.score),
                "payload": point.payload or {},
            }
            for point in result.points
        ]


def _point_id(item_id: str | int) -> str | int:
    if isinstance(item_id, int):
        if item_id < 0:
            raise VectorStoreError(f"integer item ids must be non-negative, got {item_id}")
        return item_id
    try:
        return str(uuid.UUID(str(item_id)))
    except ValueError:
        return str(uuid.uuid5(uuid.NAMESPACE_URL, f"second-brain:{item_id}"))
