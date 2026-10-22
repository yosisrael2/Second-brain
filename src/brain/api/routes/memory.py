"""Memory-layer HTTP endpoints (thin router over MemoryService).

The memory layer stores durable, confirmed knowledge about the user's world:
entities (people/groups/places/orgs with aliases) and versioned facts.
Facts are never updated in place: PATCH closes the old version and opens a
new one (supersedes_id chain, history kept); DELETE is soft (status=rejected,
valid_to set, history kept).

GET    /memory                    entities with facts (?status=proposed, ?q=)
POST   /memory/entities           create an entity (+aliases)
PATCH  /memory/entities/{id}      rename
DELETE /memory/entities/{id}      hard delete (cascades aliases + facts)
POST   /memory/facts              add a fact (status=confirmed by default)
PATCH  /memory/facts/{id}         new version (old one closed, history kept)
DELETE /memory/facts/{id}         soft delete (rejected, history kept)
POST   /memory/facts/{id}/confirm proposed -> confirmed (409 otherwise)
GET    /memory/facts/{id}/history the version chain, oldest first

Error mapping (the service raises KeyError/MemoryError/ValueError):
KeyError -> 404 unknown entity/fact; MemoryError -> 409 bad state transition
(already closed / not proposed); ValueError -> 422 (fact without object/value).
"""

import uuid
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, status

from brain import schemas
from brain.api.deps import get_memory_service
from brain.services.memory import MemoryError, MemoryService

router = APIRouter(prefix="/memory", tags=["memory"])


@router.get("", response_model=schemas.MemoryOverview)
def get_memory(
    status: Literal["confirmed", "proposed"] | None = Query(
        default=None,
        description="Filter facts by status (default: confirmed only)",
    ),
    q: str | None = Query(
        default=None,
        max_length=200,
        description="Keep only entities matching by name, alias or fact value",
    ),
    service: MemoryService = Depends(get_memory_service),
) -> schemas.MemoryOverview:
    """Entities with their facts, grouped; kids are ordered client-side.

    With an explicit ``status`` filter, entities carrying no fact of that
    status are dropped (an empty group is noise — e.g. the Proposed inbox).
    """
    entities = service.overview(
        status=status or "confirmed",
        q=(q.strip() or None) if q else None,
    )
    if status is not None:
        entities = [entity for entity in entities if entity.get("facts")]
    return schemas.MemoryOverview(
        entities=[schemas.EntityRead.model_validate(entity) for entity in entities]
    )


@router.post("/entities", status_code=status.HTTP_201_CREATED, response_model=schemas.EntityRead)
def create_entity(
    data: schemas.EntityCreate,
    service: MemoryService = Depends(get_memory_service),
) -> schemas.EntityRead:
    """Create an entity; aliases optional (an identical entity is returned as-is)."""
    return schemas.EntityRead.model_validate(service.add_entity(**data.model_dump()))


@router.patch("/entities/{entity_id}", response_model=schemas.EntityRead)
def rename_entity(
    entity_id: uuid.UUID,
    data: schemas.EntityPatch,
    service: MemoryService = Depends(get_memory_service),
) -> schemas.EntityRead:
    """Rename an entity; 404 when missing."""
    try:
        entity = service.patch_entity(entity_id, **data.model_dump(exclude_unset=True))
    except KeyError:
        raise HTTPException(status_code=404, detail=f"entity '{entity_id}' not found") from None
    return schemas.EntityRead.model_validate(entity)


@router.delete("/entities/{entity_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_entity(
    entity_id: uuid.UUID,
    service: MemoryService = Depends(get_memory_service),
) -> None:
    """Hard-delete an entity (cascades aliases and its facts); 404 when missing."""
    try:
        service.delete_entity(entity_id)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"entity '{entity_id}' not found") from None


@router.post("/facts", status_code=status.HTTP_201_CREATED, response_model=schemas.FactRead)
def create_fact(
    data: schemas.FactCreate,
    service: MemoryService = Depends(get_memory_service),
) -> schemas.FactRead:
    """Add a fact; confirmed by default, or status=proposed for the inbox."""
    try:
        fact = service.add_fact(**data.model_dump())
    except KeyError:
        raise HTTPException(
            status_code=404, detail=f"subject entity '{data.subject_id}' not found"
        ) from None
    return schemas.FactRead.model_validate(fact)


@router.patch("/facts/{fact_id}", response_model=schemas.FactRead)
def edit_fact(
    fact_id: uuid.UUID,
    data: schemas.FactPatch,
    service: MemoryService = Depends(get_memory_service),
) -> schemas.FactRead:
    """Edit a fact = close this version + open a new one; history kept."""
    try:
        fact = service.patch_fact(fact_id, **data.model_dump(exclude_unset=True))
    except KeyError:
        raise HTTPException(status_code=404, detail=f"fact '{fact_id}' not found") from None
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    return schemas.FactRead.model_validate(fact)


@router.delete("/facts/{fact_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_fact(
    fact_id: uuid.UUID,
    service: MemoryService = Depends(get_memory_service),
) -> None:
    """Soft-delete a fact (rejected, valid_to=now); history kept; 409 when already closed."""
    try:
        service.delete_fact(fact_id)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"fact '{fact_id}' not found") from None
    except MemoryError:
        raise HTTPException(
            status_code=409, detail="fact is already closed (deleted or superseded)"
        ) from None


@router.post("/facts/{fact_id}/confirm", response_model=schemas.FactRead)
def confirm_fact(
    fact_id: uuid.UUID,
    service: MemoryService = Depends(get_memory_service),
) -> schemas.FactRead:
    """Confirm a proposed fact in place; 409 when it is not proposed."""
    try:
        fact = service.confirm_fact(fact_id)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"fact '{fact_id}' not found") from None
    except MemoryError:
        raise HTTPException(
            status_code=409, detail="fact is not proposed; confirm only applies to proposed facts"
        ) from None
    return schemas.FactRead.model_validate(fact)


@router.get("/facts/{fact_id}/history", response_model=list[schemas.FactRead])
def fact_history(
    fact_id: uuid.UUID,
    service: MemoryService = Depends(get_memory_service),
) -> list[schemas.FactRead]:
    """The fact's version chain (oldest first); 404 when the id is unknown."""
    try:
        chain = service.fact_history(fact_id)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"fact '{fact_id}' not found") from None
    return [schemas.FactRead.model_validate(fact) for fact in chain]
