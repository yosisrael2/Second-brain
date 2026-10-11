"""FastAPI dependency providers (composition root for services)."""

from fastapi import Depends
from sqlalchemy.orm import Session

from brain.db.session import get_db
from brain.repositories.items import ItemsRepository
from brain.services.items import ItemsService


def get_items_service(db: Session = Depends(get_db)) -> ItemsService:
    """Provide an ItemsService bound to the request-scoped DB session."""
    return ItemsService(ItemsRepository(db))
