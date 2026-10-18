"""Semantic search endpoint (thin router over SearchService)."""

from typing import Annotated

from fastapi import APIRouter, Depends, Query

from brain import schemas
from brain.api.deps import get_search_service
from brain.services.search import SearchHit, SearchService

router = APIRouter(tags=["search"])


def to_result_read(hit: SearchHit) -> schemas.SearchResultRead:
    """Map a SearchHit (item + score) to its API representation."""
    item = schemas.ItemRead.model_validate(hit.item)
    return schemas.SearchResultRead(**item.model_dump(), score=hit.score)


@router.get("/search", response_model=list[schemas.SearchResultRead])
def semantic_search(
    q: Annotated[
        str,
        Query(min_length=1, max_length=512, description="Natural-language query"),
    ],
    limit: Annotated[int, Query(ge=1, le=schemas.MAX_PAGE_SIZE)] = schemas.DEFAULT_PAGE_SIZE,
    offset: Annotated[int, Query(ge=0)] = 0,
    service: SearchService = Depends(get_search_service),
) -> list[schemas.SearchResultRead]:
    """Semantic search over memory items (embed -> vector search -> rerank).

    Falls back to keyword search when embeddings/Qdrant are unavailable;
    fallback hits carry `score: null`.
    """
    hits: list[SearchHit] = service.search(q, limit=limit, offset=offset)
    return [to_result_read(hit) for hit in hits]
