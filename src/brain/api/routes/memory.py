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
"""

import uuid
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, status

from brain import schemas
from brain.api.deps import get_memory_service
from brain.services.memory import MemoryService

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
        description="Keep only entities whose name or an alias matches",
    ),
    service: MemoryService = Depends(get_memory_service),
) -> schemas.MemoryOverview:
    """Entities with their facts, grouped; kids are ordered client-side."""
    entities = service.overview(
        status=status or "confirmed",
        q=(q.strip() or None) if q else None,
    )
    return schemas.MemoryOverview(entities=entities)


@router.post("/entities", status_code=status.HTTP_201_CREATED, response_model=schemas.EntityRead)
def create_entity(
    data: schemas.EntityCreate,
    service: MemoryService = Depends(get_memory_service),
) -> schemas.EntityRead:
    """Create an entity; aliases optional."""
    return schemas.EntityRead.model_validate(service.add_entity(**data.model_dump()))


@router.patch("/entities/{entity_id}", response_model=schemas.EntityRead)
def rename_entity(
    entity_id: uuid.UUID,
    data: schemas.EntityPatch,
    service: MemoryService = Depends(get_memory_service),
) -> schemas.EntityRead:
    """Rename an entity; 404 when missing."""
    return schemas.EntityRead.model_validate(service.patch_entity(entity_id, **data.model_dump(exclude_unset=True)))


@router.delete("/entities/{entity_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_entity(
    entity_id: uuid.UUID,
    service: MemoryService = Depends(get_memory_service),
) -> None:
    """Hard-delete an entity (cascades aliases and its facts); 404 when missing."""
    service.delete_entity(entity_id)


@router.post("/facts", status_code=status.HTTP_201_CREATED, response_model=schemas.FactRead)
def create_fact(
    data: schemas.FactCreate,
    service: MemoryService = Depends(get_memory_service),
) -> schemas.FactRead:
    """Add a fact; confirmed by default, or `status=proposed` for the inbox."""
    return schemas.FactRead.model_validate(service.add_fact(**data.model_dump()))


@router.patch("/facts/{fact_id}", response_model=schemas.FactRead)
def edit_fact(
    fact_id: uuid.UUID,
    data: schemas.FactPatch,
    service: MemoryService = Depends(get_memory_service),
) -> schemas.FactRead:
    """Edit a fact = close this version + open a new one; history kept."""
    return schemas.FactRead.model_validate(
        service.patch_fact(fact_id, **data.model_dump(exclude_unset=True))
    )


@router.delete("/facts/{fact_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_fact(
    fact_id: uuid.UUID,
    service: MemoryService = Depends(get_memory_service),
) -> None:
    """Soft-delete a fact (rejected, valid_to=now); history kept; 409 when already closed."""
    service.delete_fact(fact_id)


@router.post("/facts/{fact_id}/confirm", response_model=schemas.FactRead)
def confirm_fact(
    fact_id: uuid.UUID,
    service: MemoryService = Depends(get_memory_service),
) -> schemas.FactRead:
    """Confirm a proposed fact in place; 409 when it is not proposed."""
    return schemas.FactRead.model_validate(service.confirm_fact(fact_id))


@router.get("/facts/{fact_id}/history", response_model=list[schemas.FactRead])
def fact_history(
    fact_id: uuid.UUID,
    service: MemoryService = Depends(get_memory_service),
) -> list[schemas.FactRead]:
    """The fact's version chain (oldest first); 404 when the id is unknown."""
    return [
        schemas.FactRead.model_validate(fact) for fact in service.fact_history(fact_id)
    ]
