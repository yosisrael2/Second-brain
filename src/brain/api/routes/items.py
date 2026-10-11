"""Memory item endpoints (thin router over ItemsService).

Route order matters: static paths (/search) are declared before the
/{item_id} route so they are not captured by the uuid converter.
"""

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query, status

from brain import schemas
from brain.api.deps import get_ingest_service, get_items_service
from brain.db.enums import ItemStatus, ItemType
from brain.services.ingest import IngestService
from brain.services.items import ItemsService

router = APIRouter(prefix="/items", tags=["items"])


@router.post("", status_code=status.HTTP_201_CREATED, response_model=schemas.ItemRead)
def create_item(
    data: schemas.ItemCreate,
    classify: Annotated[
        bool,
        Query(description="Auto-classify via Jev when the payload omits `type`"),
    ] = True,
    service: IngestService = Depends(get_ingest_service),
) -> schemas.ItemRead:
    """Create a memory item (starts in status=inbox).

    With ``classify=true`` (default) and no explicit ``type`` in the payload,
    the item is auto-classified via Jev and routed (filed/needs_review/inbox).
    Classification and indexing failures never block creation.
    """
    return schemas.ItemRead.model_validate(service.create(data, classify=classify))


@router.get("/search", response_model=list[schemas.ItemRead])
def search_items(
    q: Annotated[
        str,
        Query(
            min_length=1,
            max_length=512,
            description="Keyword query (Postgres websearch syntax)",
        ),
    ],
    limit: Annotated[int, Query(ge=1, le=schemas.MAX_PAGE_SIZE)] = schemas.DEFAULT_PAGE_SIZE,
    offset: Annotated[int, Query(ge=0)] = 0,
    service: ItemsService = Depends(get_items_service),
) -> list[schemas.ItemRead]:
    """Full-text keyword search over item raw_text."""
    items = service.search(query=q, limit=limit, offset=offset)
    return [schemas.ItemRead.model_validate(item) for item in items]


@router.get("", response_model=list[schemas.ItemRead])
def list_items(
    type: Annotated[ItemType | None, Query(description="Filter by item type")] = None,  # noqa: A002
    project: str | None = None,
    status: Annotated[ItemStatus | None, Query(description="Filter by item status")] = None,  # noqa: A002
    limit: Annotated[int, Query(ge=1, le=schemas.MAX_PAGE_SIZE)] = schemas.DEFAULT_PAGE_SIZE,
    offset: Annotated[int, Query(ge=0)] = 0,
    service: ItemsService = Depends(get_items_service),
) -> list[schemas.ItemRead]:
    """List items newest-first, optionally filtered by type/project/status."""
    items = service.list(
        item_type=type,
        project=project,
        status=status,
        limit=limit,
        offset=offset,
    )
    return [schemas.ItemRead.model_validate(item) for item in items]


@router.get("/{item_id}", response_model=schemas.ItemRead)
def get_item(
    item_id: uuid.UUID,
    service: ItemsService = Depends(get_items_service),
) -> schemas.ItemRead:
    """Fetch a single memory item by id."""
    return schemas.ItemRead.model_validate(service.get(item_id))


@router.patch("/{item_id}", response_model=schemas.ItemRead)
def update_item(
    item_id: uuid.UUID,
    data: schemas.ItemUpdate,
    service: ItemsService = Depends(get_items_service),
) -> schemas.ItemRead:
    """Partially update a memory item; absent fields stay unchanged."""
    return schemas.ItemRead.model_validate(service.update(item_id, data))


@router.delete("/{item_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_item(
    item_id: uuid.UUID,
    service: ItemsService = Depends(get_items_service),
) -> None:
    """Delete a memory item by id."""
    service.delete(item_id)
