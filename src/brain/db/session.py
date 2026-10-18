"""Engine and session factory, plus the FastAPI dependency for DB sessions."""

from collections.abc import Iterator

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from brain.config import get_settings


def create_engine_from_settings() -> Engine:
    """Build a SQLAlchemy engine from settings."""
    return create_engine(get_settings().database_url, pool_pre_ping=True)


engine = create_engine_from_settings()
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def get_db() -> Iterator[Session]:
    """FastAPI dependency yielding a database session, closed on request end."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
