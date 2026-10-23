"""Engine and session factory, plus the FastAPI dependency for DB sessions."""

from collections.abc import Iterator
from typing import Any

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from brain.config import assert_not_live_database, get_settings


def create_engine_from_settings() -> Engine:
    """Build a SQLAlchemy engine from settings (refuses the LIVE database).

    Construction-time guard: checking here means a live URL without
    BRAIN_ALLOW_LIVE=1 fails exactly when a live-capable engine is actually
    built — not on every import of this module (a 'brain --help' or an
    eval-only command must not need the flag for doing nothing dangerous).
    """
    database_url = get_settings().database_url
    assert_not_live_database(database_url)
    return create_engine(database_url, pool_pre_ping=True)


_engine: Engine | None = None
_session_factory: sessionmaker | None = None


def get_engine() -> Engine:
    """The shared engine, built (and live-guarded) on first use."""
    global _engine, _session_factory
    if _engine is None:
        _engine = create_engine_from_settings()
        _session_factory = sessionmaker(
            bind=_engine, autoflush=False, expire_on_commit=False
        )
    return _engine


class _LazyEngine:
    """Module-level ``engine`` stand-in: the real engine builds on first use.

    Keeps ``from brain.db.session import engine`` cheap and guard-free at
    import time (the CLI imports this module even for commands that never
    touch the DB); every attribute access goes to the real engine, so the
    guard fires exactly when the database would actually be used.
    """

    def __getattr__(self, name: str) -> Any:
        return getattr(get_engine(), name)


class _LazySessionFactory:
    """Module-level ``SessionLocal`` stand-in: binds on first call."""

    def __call__(self, *args: Any, **kwargs: Any) -> Session:
        get_engine()  # ensures _session_factory exists (and runs the guard)
        assert _session_factory is not None
        return _session_factory(*args, **kwargs)

    def __getattr__(self, name: str) -> Any:
        get_engine()
        assert _session_factory is not None
        return getattr(_session_factory, name)


engine = _LazyEngine()
SessionLocal = _LazySessionFactory()


def get_db() -> Iterator[Session]:
    """FastAPI dependency yielding a database session, closed on request end."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
