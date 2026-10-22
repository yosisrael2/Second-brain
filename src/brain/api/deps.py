"""FastAPI dependency providers (composition root for services and AI clients).

AI clients are app-lifetime singletons (cached factories) built from
``Settings`` and closed on shutdown via :func:`close_shared_clients`; services
are request-scoped and assembled here via ``Depends``.
"""

from functools import lru_cache
from zoneinfo import ZoneInfo

from fastapi import Depends
from sqlalchemy.orm import Session

from brain.ai.ask import LLMClient
from brain.ai.classify import JevClassifier
from brain.ai.embedder import Embedder
from brain.ai.jev import JevClient
from brain.ai.rerank import JevReranker
from brain.ai.vectorstore import QdrantVectorStore
from brain.config import get_settings, unwrap_secret
from brain.db.session import get_db
from brain.factories import build_embedder, build_llm_client
from brain.repositories.items import ItemsRepository
from brain.repositories.memory import MemoryRepository
from brain.services.ask import AskService
from brain.services.indexing import QdrantItemIndexer
from brain.services.ingest import IngestService
from brain.services.items import ItemsService
from brain.services.memory import MemoryService
from brain.services.search import SearchService


@lru_cache
def _jev_client() -> JevClient:
    """App-lifetime Jev client built from settings."""
    settings = get_settings()
    return JevClient(
        api_key=unwrap_secret(settings.jev_api_key),
        base_url=settings.jev_base_url,
        model=settings.jev_model,
        timeout_s=settings.jev_timeout_ms / 1000,
    )


@lru_cache
def _embedder() -> Embedder:
    """App-lifetime embedder built from settings (provider-selectable)."""
    return build_embedder(get_settings())


@lru_cache
def _llm_client() -> LLMClient:
    """App-lifetime LLM client built from settings (provider-selectable)."""
    return build_llm_client(get_settings())


@lru_cache
def _vector_store() -> QdrantVectorStore:
    """App-lifetime Qdrant store built from settings."""
    settings = get_settings()
    return QdrantVectorStore(
        url=settings.qdrant_url,
        api_key=unwrap_secret(settings.qdrant_api_key),
        collection=settings.qdrant_collection,
        timeout_s=settings.qdrant_timeout_s,
    )


@lru_cache
def _item_indexer() -> QdrantItemIndexer:
    """App-lifetime item indexer over the shared embedder and store.

    Keeps ensure_collection to once per process.
    """
    return QdrantItemIndexer(_embedder(), _vector_store())


def close_shared_clients() -> None:
    """Close every already-built cached AI client; app lifespan calls this on shutdown.

    Only factories with a cached instance are closed — shutdown never
    constructs new clients (e.g. loading the fastembed model). All close()
    implementations are idempotent.
    """
    for factory in (_jev_client, _llm_client, _item_indexer, _embedder, _vector_store):
        if factory.cache_info().currsize:
            factory().close()


def get_embedder() -> Embedder:
    """Provide the shared embedder."""
    return _embedder()


def get_vector_store() -> QdrantVectorStore:
    """Provide the shared vector store."""
    return _vector_store()


def get_item_indexer() -> QdrantItemIndexer:
    """Provide the shared app-lifetime item indexer."""
    return _item_indexer()


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


def get_memory_repository(db: Session = Depends(get_db)) -> MemoryRepository:
    """Provide a request-scoped MemoryRepository."""
    return MemoryRepository(db)


def get_memory_service(
    repo: MemoryRepository = Depends(get_memory_repository),
) -> MemoryService:
    """Provide a MemoryService bound to the request-scoped DB session."""
    return MemoryService(repo)


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
    settings = get_settings()
    return AskService(
        search,
        llm,
        top_k=settings.ask_top_k,
        recall_pool=settings.ask_recall_pool,
        tz=ZoneInfo(settings.timezone),
    )
