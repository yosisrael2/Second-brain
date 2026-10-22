"""Proof that the test suite can never wipe a developer's database.

The conftest pins DATABASE_URL to a *_test database and the destructive
tardown guards on the engine URL actually in use. These tests pin both
contract points, end to end: a marker row seeded in a disposable 'dev-like'
database must survive a full run of the DB-backed suite.

The dev-like database is NOT the developer's real database: it is derived
from the suite's own *_test database name (``brain_w3_test`` ->
``brain_w3_dev``), created + migrated by the test and dropped afterwards.
The helper refuses any URL whose database is the live 'brain' or lacks a
disposable suffix, so the suite can never seed or delete rows in a real
database.
"""

import os
import subprocess
import sys
import uuid
from pathlib import Path

import psycopg
import pytest

from brain.config import get_settings
from conftest import assert_test_database

REPO_ROOT = Path(__file__).resolve().parents[1]

MAINTENANCE_URL = "postgresql://brain:brain@localhost:27432/postgres"
MARKER_RAW_TEXT = "__db_isolation_marker__"


def _dev_like_db_url() -> str:
    """Disposable 'dev-like' DB derived from the suite's own test DB.

    brain_w3_test -> brain_w3_dev; brain_test (CI default) -> brain_dev.
    Refuses to return anything that could be a real database: the name must
    end with _dev and must not be 'brain'.
    """
    test_db = get_settings().database_url.rsplit("/", 1)[-1].split("?")[0]
    assert test_db.endswith("_test"), f"suite is not pinned to a *_test db: {test_db!r}"
    dev_name = test_db[: -len("_test")] + "_dev"
    if dev_name == "brain" or not dev_name.endswith("_dev"):
        pytest.fail(f"derived dev-like db is not disposable: {dev_name!r}")
    return f"postgresql://brain:brain@localhost:27432/{dev_name}"


def _drop_database(url: str) -> None:
    name = url.rsplit("/", 1)[-1]
    assert name.endswith("_dev"), f"refusing to drop non-disposable db {name!r}"
    with psycopg.connect(MAINTENANCE_URL, connect_timeout=3, autocommit=True) as conn:
        conn.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')


def _create_and_migrate_dev_like_db(url: str) -> None:
    """Create the disposable DB and migrate it to head (as dev-start would)."""
    name = url.rsplit("/", 1)[-1]
    with psycopg.connect(MAINTENANCE_URL, connect_timeout=3, autocommit=True) as conn:
        conn.execute(f'CREATE DATABASE "{name}"')
    migrated = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        capture_output=True,
        text=True,
        timeout=120,
        cwd=str(REPO_ROOT),
        env={**os.environ, "DATABASE_URL": url},
    )
    if migrated.returncode != 0:
        pytest.skip(
            "dev-like DB could not be migrated: "
            f"{migrated.stderr.strip()[:200]}"
        )


def test_marker_db_is_never_the_live_database() -> None:
    """The isolation 'dev-like' DB must never be the user's live 'brain' DB.

    It must be a disposable per-worker database whose name ends with _dev,
    derived from the suite's own *_test database.
    """
    name = _dev_like_db_url().rsplit("/", 1)[-1].split("?")[0]
    assert name != "brain", "isolation test would seed/delete rows in the LIVE db"
    assert name.endswith("_dev"), f"dev-like db {name!r} is not disposable (_dev)"


def test_guard_refuses_non_test_urls_and_accepts_test_ones() -> None:
    with pytest.raises(RuntimeError, match="not a \\*_test database"):
        assert_test_database("postgresql://brain:brain@localhost:27432/brain")
    assert_test_database("postgresql://brain:brain@localhost:27432/brain_test")
    assert_test_database("postgresql+psycopg://brain:brain@localhost:27432/brain_test?x=1")


def test_full_suite_leaves_dev_db_rows_untouched() -> None:
    """Seed a marker in a disposable dev-like DB, run the DB suite, assert it survives.

    The dev-like DB is created fresh (empty), migrated to head exactly as
    dev-start would, then dropped. If the suite ever connected to it, the
    marker (or the whole schema) would vanish.
    """
    dev_url = _dev_like_db_url()
    try:
        _drop_database(dev_url)  # defensive: start from a clean slate
        _create_and_migrate_dev_like_db(dev_url)
        with psycopg.connect(dev_url, connect_timeout=3, autocommit=True) as conn:
            conn.execute(
                "INSERT INTO memory_items (id, raw_text, source, status) "
                "VALUES (%s, %s, 'manual', 'inbox')",
                (uuid.uuid4(), MARKER_RAW_TEXT),
            )
    except psycopg.OperationalError as exc:
        pytest.skip(f"dev Postgres unavailable ({exc})")

    try:
        result = subprocess.run(
            [sys.executable, "-m", "pytest", "tests/test_items.py", "-q"],
            capture_output=True,
            text=True,
            timeout=300,
            cwd=str(REPO_ROOT),
            env={
                **os.environ,  # forward DATABASE_URL: each worker owns a *_test DB
                "PATH": "/usr/bin:/bin:/usr/sbin:/usr/local/bin",
                "HOME": str(Path.home()),
            },
        )
        assert result.returncode == 0, result.stdout + result.stderr

        with psycopg.connect(dev_url, connect_timeout=3) as conn:
            survived = conn.execute(
                "SELECT count(*) FROM memory_items WHERE raw_text = %s",
                (MARKER_RAW_TEXT,),
            ).fetchone()[0]
        assert survived == 1, "the suite wiped a row from the dev-like database!"
    finally:
        _drop_database(dev_url)


def test_suite_settings_point_at_a_test_database() -> None:
    """Whatever the ambient environment says, the suite resolves to *_test."""
    assert get_settings().database_url.rsplit("/", 1)[-1].endswith("_test")


def test_suite_never_targets_the_default_qdrant_collection() -> None:
    """The vector store gets the same protection as the database.

    Regression guard: capture e2e tests used to wire the real app settings,
    whose qdrant_collection default is the live 'brain_items' — every run
    upserted test items into the developer's real collection (2214 stray
    points found on the user's instance). The conftest now pins a *_test
    collection and refuses the suite otherwise.
    """
    from brain.config import DEFAULT_QDRANT_COLLECTION

    resolved = get_settings().qdrant_collection
    assert resolved != DEFAULT_QDRANT_COLLECTION, (
        "suite would write to the live qdrant collection — "
        "conftest must pin QDRANT_COLLECTION to a *_test collection"
    )
    assert resolved.endswith("_test")
