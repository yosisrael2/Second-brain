"""Shared pytest fixtures.

SAFETY: the suite never touches a developer's real database. DATABASE_URL is
forced to a ``*_test`` database (created on demand); any non-test URL fails
fast, and the destructive fixtures double-check the engine URL before deleting.
"""

import os

_TEST_DB_URL = "postgresql+psycopg://brain:brain@localhost:27432/brain_test"

# The env var wins over .env in pydantic-settings, so this pins the suite to
# the test database unless the caller already chose a *_test one.
_env_url = os.environ.get("DATABASE_URL")
if _env_url is not None and not _env_url.rsplit("/", 1)[-1].split("?")[0].endswith("_test"):
    raise RuntimeError(
        "refusing to run tests against non-test database "
        f"{_env_url.rsplit('/', 1)[-1]!r}; point DATABASE_URL at a *_test database"
    )
os.environ.setdefault("DATABASE_URL", _TEST_DB_URL)

# Qdrant gets the same treatment as Postgres: the suite must never write to a
# developer's real collection. The default collection name is the LIVE one
# ('brain_items'), so ambient .env files would silently point e2e tests at it.
# Pin a *_test collection; refuse non-test collections outright.
_env_collection = os.environ.get("QDRANT_COLLECTION")
if _env_collection is not None and not _env_collection.endswith("_test"):
    raise RuntimeError(
        "refusing to run tests against non-test qdrant collection "
        f"{_env_collection!r}; point QDRANT_COLLECTION at a *_test collection"
    )
os.environ.setdefault("QDRANT_COLLECTION", "brain_items_test")

from collections.abc import Iterator  # noqa: E402
from pathlib import Path  # noqa: E402

import psycopg  # noqa: E402
import pytest  # noqa: E402
from alembic import command  # noqa: E402
from alembic.config import Config  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import text  # noqa: E402
from sqlalchemy.exc import OperationalError  # noqa: E402

from brain.config import get_settings  # noqa: E402
from brain.db.session import engine  # noqa: E402
from brain.main import create_app  # noqa: E402

_REPO_ROOT = Path(__file__).resolve().parents[1]


def _database_name(url: str) -> str:
    """Extract the database name from a SQLAlchemy/psycopg URL."""
    return url.rsplit("/", 1)[-1].split("?")[0]


def assert_test_database(url: str) -> None:
    """Refuse destructive fixtures unless the URL's database name ends in _test."""
    if not _database_name(url).endswith("_test"):
        raise RuntimeError(
            f"refusing destructive fixture: {url!r} is not a *_test database"
        )


def _database_url() -> str:
    """The URL of the engine the suite actually uses (Settings-resolved)."""
    return get_settings().database_url


def _ensure_test_database() -> None:
    """CREATE DATABASE the test db (idempotent) on the same server."""
    url = _database_url()
    db_name = _database_name(url)
    admin_url = url.rsplit("/", 1)[0].replace("postgresql+psycopg", "postgresql") + "/postgres"
    with psycopg.connect(admin_url, autocommit=True, connect_timeout=3) as conn:
        exists = conn.execute(
            "SELECT 1 FROM pg_database WHERE datname = %s", (db_name,)
        ).fetchone()
        if exists is None:
            conn.execute(f'CREATE DATABASE "{db_name}"')


@pytest.fixture(name="client")
def client_fixture(monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    """TestClient wired to a fresh app with hermetic settings.

    Ambient secrets in .env / the environment (e.g. a real JEV_API_KEY) must
    not change test outcomes: with Jev configured, POST /items would classify
    and file items where these tests assert inbox defaults.
    """
    monkeypatch.setenv("JEV_API_KEY", "")
    get_settings.cache_clear()
    yield TestClient(create_app())
    get_settings.cache_clear()


@pytest.fixture(scope="session", name="database")
def database_fixture() -> Iterator[None]:
    """Point the suite at the *_test database and migrate it; skip without Postgres."""
    try:
        _ensure_test_database()  # connects to the admin db; creates *_test if missing
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
    except OperationalError as exc:
        pytest.skip(f"Postgres unavailable - run `docker compose up -d` ({exc})")
    assert_test_database(_database_url())
    _ensure_test_database()
    _run_migrations()
    yield


@pytest.fixture(name="clean_items")
def clean_items_fixture(database: None) -> Iterator[None]:
    """Delete all memory items after the test, isolating DB-backed tests.

    Guarded at the point of destruction: the DELETE only runs when the engine
    actually in use points at a ``*_test`` database — never the dev DB.
    """
    yield
    assert_test_database(str(engine.url))
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM memory_items"))


@pytest.fixture(name="clean_memory")
def clean_memory_fixture(database: None) -> Iterator[None]:
    """Delete all memory-layer rows after the test (facts first: FK order).

    Same guard as clean_items: only ever runs against a ``*_test`` database.
    """
    yield
    assert_test_database(str(engine.url))
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM memory_facts"))
        conn.execute(text("DELETE FROM entity_aliases"))
        conn.execute(text("DELETE FROM entities"))


@pytest.fixture(name="memory_db")
def memory_db_fixture(clean_memory: None) -> Iterator[None]:
    """Wipe the memory tables BEFORE the test too (clean_memory handles after)."""
    assert_test_database(str(engine.url))
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM memory_facts"))
        conn.execute(text("DELETE FROM entity_aliases"))
        conn.execute(text("DELETE FROM entities"))
    yield


def _run_migrations() -> None:
    """Apply Alembic migrations up to head against the configured database.

    brain_test is SHARED between worker checkouts: a sibling worker's
    unmerged branch can leave it stamped at a revision this tree does not
    know (ResolutionError on upgrade). The test database is disposable, so
    in that case drop and recreate it, then migrate from scratch — guarded
    by assert_test_database like every destructive fixture.
    """
    try:
        _upgrade_head()
    except Exception as exc:
        unknown = "Can't locate revision" in str(exc) or "not a valid upgrade target" in str(exc)
        if not unknown:
            raise
        assert_test_database(_database_url())
        _drop_test_database()
        _ensure_test_database()
        _upgrade_head()


def _upgrade_head() -> None:
    cfg = Config(str(_REPO_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(_REPO_ROOT / "src/brain/db/migrations"))
    command.upgrade(cfg, "head")


def _drop_test_database() -> None:
    """DROP DATABASE <the *_test db> via the admin connection (idempotent)."""
    url = _database_url()
    db_name = _database_name(url)
    admin_url = url.rsplit("/", 1)[0].replace("postgresql+psycopg", "postgresql") + "/postgres"
    with psycopg.connect(admin_url, autocommit=True, connect_timeout=3) as conn:
        conn.execute(
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
            "WHERE datname = %s AND pid <> pg_backend_pid()",
            (db_name,),
        )
        conn.execute(f'DROP DATABASE IF EXISTS "{db_name}"')


# F3: the attachments-core fixture (tests/test_attachments.py) shared with the
# poller test modules. Module-level alias — pytest collects fixture objects by
# value, so the same fixture instance is available as `svc_env` everywhere.
from tests.test_attachments import svc_env_fixture as svc_env  # noqa: E402,F401
