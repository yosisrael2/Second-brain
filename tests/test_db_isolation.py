"""Proof that the test suite can never wipe a developer's database.

The conftest pins DATABASE_URL to a *_test database and the destructive
teardown guards on the engine URL actually in use. These tests pin both
contract points, end to end: a marker row seeded in the DEV database must
survive a full run of the DB-backed suite.
"""

import subprocess
import sys
import uuid
from pathlib import Path

import psycopg
import pytest

from brain.config import get_settings
from conftest import assert_test_database

REPO_ROOT = Path(__file__).resolve().parents[1]

DEV_DB_URL = "postgresql://brain:brain@localhost:5432/brain"
MARKER_RAW_TEXT = "__db_isolation_marker__"


def test_guard_refuses_non_test_urls_and_accepts_test_ones() -> None:
    with pytest.raises(RuntimeError, match="not a \\*_test database"):
        assert_test_database("postgresql://brain:brain@localhost:5432/brain")
    assert_test_database("postgresql://brain:brain@localhost:5432/brain_test")
    assert_test_database("postgresql+psycopg://brain:brain@localhost:5432/brain_test?x=1")


def test_full_suite_leaves_dev_db_rows_untouched() -> None:
    """Seed a marker in the dev DB, run the DB suite, assert the marker survives."""
    try:
        with psycopg.connect(DEV_DB_URL, connect_timeout=3) as conn:
            conn.autocommit = True
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
