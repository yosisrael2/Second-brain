"""Vector store repository backed by Qdrant."""

from __future__ import annotations

import uuid
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from typing import Any, Protocol

from qdrant_client import QdrantClient, models

DEFAULT_COLLECTION = "brain_items"
DEFAULT_QDRANT_TIMEOUT_S = 10  # seconds; QdrantClient takes int|None


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

    def delete(self, item_id: str | int) -> None:
        """Remove one item's vector; a missing id is not an error."""
        ...

    def delete_collection(self) -> None:
        """Drop the whole collection if it exists (used by full rebuilds)."""
        ...

    def search(self, vector: list[float], k: int = 5) -> list[dict[str, Any]]:
        """Return the ``k`` nearest items as ``{id, score, payload}`` dicts."""
        ...

    def close(self) -> None:
        """Release underlying connections (no-op for in-memory fakes)."""
        ...


class QdrantVectorStore:
    """Repository (Adapter) implementing VectorStore on top of Qdrant.

    Configuration is injected; the app-wide factory lives in
    ``brain.api.deps`` (values from ``Settings``). A ready-made
    ``QdrantClient`` can be injected instead (tests use an in-memory
    client). Non-integer, non-UUID item ids are mapped to a deterministic
    UUID point id; the original id is always kept in the stored payload and
    returned by :meth:`search`.
    """

    def __init__(
        self,
        url: str | None = None,
        api_key: str | None = None,
        collection: str = DEFAULT_COLLECTION,
        timeout_s: int = DEFAULT_QDRANT_TIMEOUT_S,
        client: QdrantClient | None = None,
    ) -> None:
        self._collection = collection
        self._client = client or self._build_client(url, api_key, timeout_s)

    @staticmethod
    def _build_client(url: str | None, api_key: str | None, timeout_s: int) -> QdrantClient:
        if not url:
            raise VectorStoreError("no Qdrant URL configured (QDRANT_URL)")
        return QdrantClient(url=url, api_key=api_key, timeout=timeout_s)

    def close(self) -> None:
        """Release the underlying Qdrant client connection (idempotent)."""
        self._client.close()

    def ensure_collection(self, dim: int) -> None:
        """Create the collection with cosine distance unless it exists."""
        with self._translated():
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
        with self._translated():
            self._client.upsert(self._collection, points=[point])

    def delete(self, item_id: str | int) -> None:
        """Remove one item's vector; deleting a missing id is not an error."""
        with self._translated():
            self._client.delete(
                self._collection, points_selector=models.PointIdsList(points=[_point_id(item_id)])
            )

    def delete_collection(self) -> None:
        """Drop the whole collection if it exists (used by full rebuilds)."""
        with self._translated():
            if self._client.collection_exists(self._collection):
                self._client.delete_collection(self._collection)

    def search(self, vector: list[float], k: int = 5) -> list[dict[str, Any]]:
        """Return the ``k`` nearest items, best match first."""
        with self._translated():
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

    @contextmanager
    def _translated(self) -> Iterator[None]:
        """Translate qdrant-client failures into :class:`VectorStoreError`.

        Deliberate boundary: this is the one place catching broad exceptions,
        so callers can narrow to VectorStoreError.
        """
        try:
            yield
        except VectorStoreError:
            raise
        except Exception as exc:
            raise VectorStoreError(f"qdrant operation failed: {type(exc).__name__}: {exc}") from exc


def _point_id(item_id: str | int) -> str | int:
    if isinstance(item_id, int):
        if item_id < 0:
            raise VectorStoreError(f"integer item ids must be non-negative, got {item_id}")
        return item_id
    try:
        return str(uuid.UUID(str(item_id)))
    except ValueError:
        return str(uuid.uuid5(uuid.NAMESPACE_URL, f"second-brain:{item_id}"))
