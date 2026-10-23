"""Memory item endpoints (thin router over ItemsService).

Route order matters: static paths (/search) are declared before the
/{item_id} route so they are not captured by the uuid converter.

PR B adds relations endpoints (items 20/21/22/D4). The /relations/{rid}/...
routes live under the /items prefix and are declared AFTER /{item_id} in
source order, but FastAPI matches by path structure (/{item_id}/relations is
2 segments past the prefix; /relations/{rid}/confirm would collide ONLY for
an item_id literally named 'relations' — UUID converter rejects it first).
"""

import uuid
from typing import TYPE_CHECKING, Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status

from brain import schemas
from brain.api.deps import get_ingest_service, get_items_service
from brain.db.enums import ItemStatus, ItemType
from brain.services.ingest import IngestService
from brain.services.items import ItemsService

if TYPE_CHECKING:
    from brain.services.relations import RelationsService

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
    include_superseded: Annotated[
        bool, Query(description="Include superseded (struck-through) items")
    ] = False,
    service: ItemsService = Depends(get_items_service),
) -> list[schemas.ItemRead]:
    """Full-text keyword search over item raw_text."""
    items = service.search(
        query=q, limit=limit, offset=offset, include_superseded=include_superseded
    )
    return [schemas.ItemRead.model_validate(item) for item in items]


@router.get("", response_model=list[schemas.ItemRead])
def list_items(
    type: Annotated[ItemType | None, Query(description="Filter by item type")] = None,  # noqa: A002
    project: str | None = None,
    status: Annotated[ItemStatus | None, Query(description="Filter by item status")] = None,  # noqa: A002
    limit: Annotated[int, Query(ge=1, le=schemas.MAX_PAGE_SIZE)] = schemas.DEFAULT_PAGE_SIZE,
    offset: Annotated[int, Query(ge=0)] = 0,
    include_superseded: Annotated[
        bool, Query(description="Include superseded (struck-through) items")
    ] = False,
    service: ItemsService = Depends(get_items_service),
) -> list[schemas.ItemRead]:
    """List items newest-first, optionally filtered by type/project/status."""
    items = service.list(
        item_type=type,
        project=project,
        status=status,
        limit=limit,
        offset=offset,
        include_superseded=include_superseded,
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


# --- PR B: relations (items 20, 21, 22, D4) --------------------------------------


def _relations_service() -> "RelationsService":
    """Request-scoped RelationsService (deferred import: avoids the api cycle)."""
    from brain.services.relations import RelationsService  # noqa: PLC0415

    return RelationsService()


@router.post(
    "/{item_id}/relations",
    status_code=status.HTTP_201_CREATED,
    response_model=schemas.RelationRead,
)
def add_relation(
    item_id: uuid.UUID,
    data: schemas.RelationCreate,
    service: "RelationsService" = Depends(_relations_service),
) -> schemas.RelationRead:
    """Link this item to another (kind supersedes|corrects|derived_from|same_as).

    With ``propose=true`` and kind=supersedes the relation is only PROPOSED
    (item 20b): the old item stays visible until POST /relations/{id}/confirm.
    """
    from brain.services.relations import RelationsError  # noqa: PLC0415

    try:
        if data.kind == "supersedes":
            # item 20's tiers: explicit propose = cross-thread LLM match
            # (old item stays visible); plain POST = same-thread correction
            # (auto-applied, undoable).
            if data.propose:
                rel = service.propose_supersede(
                    new_id=item_id, old_id=data.to_id, provenance=data.provenance
                )
            else:
                rel = service.apply_supersede(
                    new_id=item_id, old_id=data.to_id, provenance=data.provenance
                )
        else:
            rel = service.add(item_id, data.to_id, kind=data.kind, provenance=data.provenance)
    except RelationsError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)
        ) from exc
    return schemas.RelationRead.model_validate(rel)


@router.get("/{item_id}/relations", response_model=list[schemas.RelationRead])
def list_relations(
    item_id: uuid.UUID,
    service: "RelationsService" = Depends(_relations_service),
) -> list[schemas.RelationRead]:
    """Every relation touching this item (outgoing + incoming)."""
    return [schemas.RelationRead.model_validate(rel) for rel in service.relations_of(item_id)]


@router.get("/{item_id}/current", response_model=schemas.ItemRead)
def current_version(
    item_id: uuid.UUID,
    service: ItemsService = Depends(get_items_service),
    relations: "RelationsService" = Depends(_relations_service),
) -> schemas.ItemRead:
    """Resolve the supersedes chain to the CURRENT version of this item."""
    from brain.services.relations import RelationsError  # noqa: PLC0415

    try:
        current_id = relations.current_version(item_id)
    except RelationsError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)
        ) from exc
    return schemas.ItemRead.model_validate(service.get(current_id))


@router.post("/relations/{relation_id}/confirm", response_model=schemas.RelationRead)
def confirm_relation(
    relation_id: uuid.UUID,
    service: "RelationsService" = Depends(_relations_service),
) -> schemas.RelationRead:
    """Confirm a PROPOSED supersede: the old item becomes struck-through."""
    from brain.services.relations import RelationsError  # noqa: PLC0415

    try:
        rel = service.confirm_supersede(relation_id)
    except RelationsError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)
        ) from exc
    return schemas.RelationRead.model_validate(rel)


@router.post("/relations/{relation_id}/undo", response_model=schemas.RelationRead)
def undo_relation(
    relation_id: uuid.UUID,
    service: "RelationsService" = Depends(_relations_service),
) -> schemas.RelationRead:
    """Undo a supersede (item 20a/D4): the old item becomes current again."""
    from brain.services.relations import RelationsError  # noqa: PLC0415

    try:
        rel = service.undo_supersede(relation_id)
    except RelationsError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)
        ) from exc
    return schemas.RelationRead.model_validate(rel)
