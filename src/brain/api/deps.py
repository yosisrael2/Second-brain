"""FastAPI dependency providers (composition root for services and AI clients).

AI clients are app-lifetime singletons (cached factories) built from
``Settings`` and closed on shutdown via :func:`close_shared_clients`; services
are request-scoped and assembled here via ``Depends``.
"""

from functools import lru_cache

from fastapi import Depends
from pydantic import SecretStr
from sqlalchemy.orm import Session

from brain.ai.ask import ClaudeLLMClient, LLMClient
from brain.ai.classify import JevClassifier
from brain.ai.embedder import Embedder, OpenAIEmbedder
from brain.ai.jev import JevClient
from brain.ai.rerank import JevReranker
from brain.ai.vectorstore import QdrantVectorStore
from brain.config import get_settings
from brain.db.session import get_db
from brain.repositories.items import ItemsRepository
from brain.services.ask import AskService
from brain.services.indexing import QdrantItemIndexer
from brain.services.ingest import IngestService
from brain.services.items import ItemsService
from brain.services.search import SearchService


def _secret(value: SecretStr | None) -> str | None:
    """Unwrap an optional SecretStr for client constructors."""
    return value.get_secret_value() if value else None


@lru_cache
def _jev_client() -> JevClient:
    """App-lifetime Jev client built from settings."""
    settings = get_settings()
    return JevClient(
        api_key=_secret(settings.jev_api_key),
        base_url=settings.jev_base_url,
        model=settings.jev_model,
        timeout_s=settings.jev_timeout_ms / 1000,
    )


@lru_cache
def _embedder() -> OpenAIEmbedder:
    """App-lifetime embedding client built from settings."""
    settings = get_settings()
    return OpenAIEmbedder(
        api_key=_secret(settings.openai_api_key),
        model=settings.openai_embedding_model,
        base_url=settings.openai_base_url,
        timeout_s=settings.openai_timeout_s,
    )


@lru_cache
def _llm_client() -> ClaudeLLMClient:
    """App-lifetime LLM client built from settings."""
    settings = get_settings()
    return ClaudeLLMClient(
        api_key=_secret(settings.anthropic_api_key),
        model=settings.anthropic_model,
        base_url=settings.anthropic_base_url,
        max_tokens=settings.anthropic_max_tokens,
        timeout_s=settings.anthropic_timeout_s,
    )


@lru_cache
def _vector_store() -> QdrantVectorStore:
    """App-lifetime Qdrant store built from settings."""
    settings = get_settings()
    return QdrantVectorStore(
        url=settings.qdrant_url,
        api_key=_secret(settings.qdrant_api_key),
        collection=settings.qdrant_collection,
        timeout_s=settings.qdrant_timeout_s,
    )


def close_shared_clients() -> None:
    """Close every cached AI client; called from the app lifespan on shutdown."""
    for client in (_jev_client(), _embedder(), _llm_client(), _vector_store()):
        client.close()


def get_embedder() -> Embedder:
    """Provide the shared embedder."""
    return _embedder()


def get_vector_store() -> QdrantVectorStore:
    """Provide the shared vector store."""
    return _vector_store()


def get_item_indexer(
    embedder: Embedder = Depends(get_embedder),
    store: QdrantVectorStore = Depends(get_vector_store),
) -> QdrantItemIndexer:
    """Provide the shared item indexer (embed + upsert / remove / purge)."""
    return QdrantItemIndexer(embedder, store)


def get_classifier() -> JevClassifier:
    """Provide the shared classifier strategy."""
    return JevClassifier(_jev_client())


def get_reranker() -> JevReranker:
    """Provide the shared reranker strategy."""
    return JevReranker(_jev_client())


def get_llm_client() -> LLMClient:
    """Provide the shared LLM client."""
    return _llm_client()


def get_items_repository(db: Session = Depends(get_db)) -> ItemsRepository:
    """Provide a request-scoped ItemsRepository."""
    return ItemsRepository(db)


def get_items_service(
    repo: ItemsRepository = Depends(get_items_repository),
    indexer: QdrantItemIndexer = Depends(get_item_indexer),
) -> ItemsService:
    """Provide an ItemsService bound to the request-scoped DB session."""
    return ItemsService(repo, indexer)


def get_ingest_service(
    repo: ItemsRepository = Depends(get_items_repository),
    classifier: JevClassifier = Depends(get_classifier),
    indexer: QdrantItemIndexer = Depends(get_item_indexer),
) -> IngestService:
    """Provide an IngestService wired to the shared AI clients."""
    return IngestService(repo, classifier, indexer)


def get_search_service(
    repo: ItemsRepository = Depends(get_items_repository),
    embedder: Embedder = Depends(get_embedder),
    store: QdrantVectorStore = Depends(get_vector_store),
    reranker: JevReranker = Depends(get_reranker),
) -> SearchService:
    """Provide a SearchService wired to the shared AI clients."""
    return SearchService(repo, embedder, store, reranker)


def get_ask_service(
    search: SearchService = Depends(get_search_service),
    llm: LLMClient = Depends(get_llm_client),
) -> AskService:
    """Provide an AskService wired to search and the shared LLM client."""
    return AskService(search, llm)
