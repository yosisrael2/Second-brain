"""Shared pytest fixtures."""

from collections.abc import Iterator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.exc import OperationalError

from brain.db.session import engine
from brain.main import create_app

_REPO_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(name="client")
def client_fixture() -> TestClient:
    """TestClient wired to a fresh app instance."""
    return TestClient(create_app())


@pytest.fixture(scope="session", name="database")
def database_fixture() -> None:
    """Ensure Postgres is reachable and migrated; skip DB-backed tests otherwise."""
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
    except OperationalError as exc:
        pytest.skip(f"Postgres unavailable - run `docker compose up -d` ({exc})")
    _run_migrations()


@pytest.fixture(name="clean_items")
def clean_items_fixture(database: None) -> Iterator[None]:
    """Delete all memory items after the test, isolating DB-backed tests."""
    yield
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM memory_items"))


def _run_migrations() -> None:
    """Apply Alembic migrations up to head against the configured database."""
    cfg = Config(str(_REPO_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(_REPO_ROOT / "src/brain/db/migrations"))
    command.upgrade(cfg, "head")
