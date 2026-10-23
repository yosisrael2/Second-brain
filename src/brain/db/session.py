"""Engine and session factory, plus the FastAPI dependency for DB sessions."""

from collections.abc import Iterator

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from brain.config import assert_not_live_database, get_settings


def create_engine_from_settings() -> Engine:
    """Build a SQLAlchemy engine from settings (refuses the LIVE database).

    Construction-time guard: the engine is the chokepoint every consumer
    (app, bot, CLI) builds on — checking here means a live URL without
    BRAIN_ALLOW_LIVE=1 fails at import/build time, not only at app start.
    """
    database_url = get_settings().database_url
    assert_not_live_database(database_url)
    return create_engine(database_url, pool_pre_ping=True)


engine = create_engine_from_settings()
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def get_db() -> Iterator[Session]:
    """FastAPI dependency yielding a database session, closed on request end."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
