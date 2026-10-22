"""Proof that the test suite can never wipe a developer's database.

The conftest pins DATABASE_URL to a *_test database and the destructive
teardown guards on the engine URL actually in use. These tests pin both
contract points, end to end: a marker row seeded in the DEV database must
survive a full run of the DB-backed suite.
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

DEV_DB_URL = "postgresql://brain:brain@localhost:27432/brain"
MARKER_RAW_TEXT = "__db_isolation_marker__"


def test_guard_refuses_non_test_urls_and_accepts_test_ones() -> None:
    with pytest.raises(RuntimeError, match="not a \\*_test database"):
        assert_test_database("postgresql://brain:brain@localhost:27432/brain")
    assert_test_database("postgresql://brain:brain@localhost:27432/brain_test")
    assert_test_database("postgresql+psycopg://brain:brain@localhost:27432/brain_test?x=1")


def _ensure_dev_like_schema() -> None:
    """Migrate the dev-like DB when it has no schema (CI throwaway case).

    No-op on a developer Mac (schema exists). In CI the 'brain' service
    database is empty — only brain_test is migrated — so the guard has
    nothing to protect until we apply the migrations, exactly as dev-start
    would before serving. Migration failure skips with an explicit reason,
    never a silent pass.
    """
    with psycopg.connect(DEV_DB_URL, connect_timeout=3) as conn:
        exists = conn.execute(
            "SELECT to_regclass('public.memory_items')"
        ).fetchone()[0]
        if exists is not None:
            return
    migrated = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        capture_output=True,
        text=True,
        timeout=120,
        cwd=str(REPO_ROOT),
        env={**os.environ, "DATABASE_URL": DEV_DB_URL},
    )
    if migrated.returncode != 0:
        pytest.skip(
            "dev-like DB has no schema and migrating it failed: "
            f"{migrated.stderr.strip()[:200]}"
        )


def test_full_suite_leaves_dev_db_rows_untouched() -> None:
    """Seed a marker in the dev DB, run the DB suite, assert the marker survives."""
    try:
        with psycopg.connect(DEV_DB_URL, connect_timeout=3) as conn:
            conn.autocommit = True
            _ensure_dev_like_schema()
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
                "PATH": "/usr/bin:/bin:/usr/sbin:/usr/local/bin",
                "HOME": str(Path.home()),
            },
        )
        assert result.returncode == 0, result.stdout + result.stderr

        with psycopg.connect(DEV_DB_URL, connect_timeout=3) as conn:
            survived = conn.execute(
                "SELECT count(*) FROM memory_items WHERE raw_text = %s",
                (MARKER_RAW_TEXT,),
            ).fetchone()[0]
        assert survived == 1, "the suite wiped a row from the dev database!"
    finally:
        with psycopg.connect(DEV_DB_URL, connect_timeout=3) as conn:
            conn.autocommit = True
            conn.execute(
                "DELETE FROM memory_items WHERE raw_text = %s", (MARKER_RAW_TEXT,)
            )


def test_suite_settings_point_at_a_test_database() -> None:
    """Whatever the ambient environment says, the suite resolves to *_test."""
    assert get_settings().database_url.endswith("/brain_test")


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
