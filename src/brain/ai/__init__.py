"""AI layer: classification, embedding, vector search, rerank, and ask."""

from .ask import (
    ClaudeLLMClient,
    FakeLLMClient,
    LLMClient,
    LLMError,
    answer,
    build_prompt,
    extract_cited_ids,
)
from .classify import FALLBACK_CLASSIFICATION, classify, route
from .embedder import Embedder, EmbedderError, FakeEmbedder, OpenAIEmbedder
from .enums import Actionability, ItemStatus, ItemType
from .jev import JevChoice, JevClient, JevClientProtocol, JevError, JevScore
from .rerank import rerank
from .vectorstore import QdrantVectorStore, VectorStore, VectorStoreError

__all__ = [
    "Actionability",
    "ClaudeLLMClient",
    "Embedder",
    "EmbedderError",
    "FakeEmbedder",
    "FakeLLMClient",
    "FALLBACK_CLASSIFICATION",
    "ItemStatus",
    "ItemType",
    "JevChoice",
    "JevClient",
    "JevClientProtocol",
    "JevError",
    "JevScore",
    "LLMClient",
    "LLMError",
    "OpenAIEmbedder",
    "QdrantVectorStore",
    "VectorStore",
    "VectorStoreError",
    "answer",
    "build_prompt",
    "classify",
    "extract_cited_ids",
    "route",
    "rerank",
]
