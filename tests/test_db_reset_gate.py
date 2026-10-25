"""The conftest revision-unknown fallback must not silently DROP the test DB.

The drop fallback (conftest ``_run_migrations``) existed for the era when
``brain_test`` was SHARED between checkouts. With per-worker ``*_test`` DBs
it is a weapon: a session whose migration scripts do not know the DB's
stamped revision (a different checkout's branch adds migrations) terminated
every live connection and dropped the DB under a RUNNING sibling suite
(25 Oct incident: worker_7's brain_w7_mem_test, stamped 7d90f6ea8407, was
dropped 4+ times mid-suite by main-scripts sessions).

Contract pinned here with the holder technique (a long-lived connection that
prints/raises when terminated):
* default: the second session FAILS FAST naming the DB and the stamped
  revision and telling the operator about ``BRAIN_TEST_DB_RESET=1``; the
  holder SURVIVES; the DB and its rows are intact.
* ``BRAIN_TEST_DB_RESET=1``: the old behaviour — drop, recreate, migrate.
"""

import os
import subprocess
import sys
import uuid
from pathlib import Path

import psycopg
import pytest

from brain.config import get_settings
from conftest import _database_name

REPO_ROOT = Path(__file__).resolve().parents[1]
UNKNOWN_REVISION = "deadbeefdeadunknown"
MARKER_RAW_TEXT = "__reset_gate_marker__"


def _admin_url() -> str:
    """The maintenance DB next to whatever server the suite itself uses.

    Derived from the suite's resolved DATABASE_URL (like conftest's own
    admin connections) — the port is NOT hardcoded: CI runs Postgres on
    5432, the dev stack on 27432.
    """
    url = get_settings().database_url
    return url.rsplit("/", 1)[0].replace("postgresql+psycopg", "postgresql") + "/postgres"


def _gate_db_url() -> str:
    """A scratch *_test DB derived from this suite's own DB (per-worktree)."""
    base = _database_name(get_settings().database_url)
    assert base.endswith("_test")
    return f"postgresql://brain:brain@localhost:27432/{base[: -len('_test')]}_rgate_test"


def _drop_database(url: str) -> None:
    name = _database_name(url)
    assert name.endswith("_test"), f"refusing to drop non-test db {name!r}"
    with psycopg.connect(_admin_url(), connect_timeout=3, autocommit=True) as conn:
        conn.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')


def _create_and_migrate(url: str) -> None:
    name = _database_name(url)
    with psycopg.connect(_admin_url(), connect_timeout=3, autocommit=True) as conn:
        conn.execute(f'CREATE DATABASE "{name}"')
    migrated = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        capture_output=True,
        text=True,
        timeout=120,
        cwd=str(REPO_ROOT),
        env={**_child_env(), "DATABASE_URL": url},
    )
    assert migrated.returncode == 0, migrated.stderr[-400:]


def _stamp_unknown_revision(url: str) -> None:
    with psycopg.connect(url, connect_timeout=3, autocommit=True) as conn:
        conn.execute("DELETE FROM alembic_version")
        conn.execute("INSERT INTO alembic_version VALUES (%s)", (UNKNOWN_REVISION,))


def _plant_marker(url: str) -> None:
    with psycopg.connect(url, connect_timeout=3, autocommit=True) as conn:
        conn.execute(
            "INSERT INTO memory_items (id, raw_text, source, status) "
            "VALUES (%s, %s, 'manual', 'inbox')",
            (uuid.uuid4(), MARKER_RAW_TEXT),
        )


def _marker_count(url: str) -> int:
    with psycopg.connect(url, connect_timeout=3) as conn:
        return conn.execute(
            "SELECT count(*) FROM memory_items WHERE raw_text = %s",
            (MARKER_RAW_TEXT,),
        ).fetchone()[0]


def _child_env() -> dict[str, str]:
    env = dict(os.environ)
    env.pop("QDRANT_COLLECTION", None)  # the child derives its own from its DB
    return env


def _run_child(extra_env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "pytest", "tests/test_items.py", "-q", "--timeout=60"],
        capture_output=True,
        text=True,
        timeout=300,
        cwd=str(REPO_ROOT),
        env={**_child_env(), **extra_env},
    )


@pytest.fixture(name="stamped_db")
def stamped_db_fixture():
    """A migrated scratch DB stamped at an unknown revision, with a marker row."""
    url = _gate_db_url()
    try:
        _drop_database(url)
        _create_and_migrate(url)
        _stamp_unknown_revision(url)
        _plant_marker(url)
    except psycopg.OperationalError as exc:
        # Same posture as test_db_isolation: no Postgres (or an unreachable
        # admin server) skips the subprocess legs instead of erroring.
        pytest.skip(f"Postgres unavailable ({exc})")
    yield url
    _drop_database(url)


def test_unknown_revision_fails_fast_and_spares_the_db(stamped_db: str) -> None:
    """DEFAULT: no silent drop. The second session fails fast (DB name +
    stamped revision + the BRAIN_TEST_DB_RESET=1 instruction); the holder
    connection survives; the DB and its rows are intact."""
    url = stamped_db
    holder = psycopg.connect(url, connect_timeout=3)

    try:
        result = _run_child({"DATABASE_URL": url})
    finally:
        holder_alive = True
        try:
            with holder.cursor() as cur:
                cur.execute("SELECT 1")
                cur.fetchone()
        except psycopg.Error:
            holder_alive = False

    combined = result.stdout + result.stderr
    assert result.returncode != 0, (
        "the child session dropped the DB and ran green — the silent drop "
        f"is back. Output tail: {combined[-400:]}"
    )
    assert _database_name(url) in combined, combined[-400:]
    assert UNKNOWN_REVISION in combined, combined[-400:]
    assert "stamped by a different checkout" in combined, combined[-400:]
    assert "BRAIN_TEST_DB_RESET=1" in combined, combined[-400:]
    assert holder_alive, "the holder connection was terminated — a DROP happened"
    assert _marker_count(url) == 1, "the DB's rows were destroyed"
    with psycopg.connect(url, connect_timeout=3) as conn:
        stamped = conn.execute("SELECT version_num FROM alembic_version").fetchone()[0]
    assert stamped == UNKNOWN_REVISION, f"the stamp was replaced: {stamped!r}"


def test_reset_env_var_rebuilds_the_db(stamped_db: str) -> None:
    """BRAIN_TEST_DB_RESET=1 keeps today's behaviour: drop, recreate, migrate
    — the holder is terminated (the drop's pg_terminate_backend) and the DB
    comes back at THIS checkout's head, empty."""
    url = stamped_db
    holder = psycopg.connect(url, connect_timeout=3)

    result = _run_child({"DATABASE_URL": url, "BRAIN_TEST_DB_RESET": "1"})

    assert result.returncode == 0, result.stdout + result.stderr
    try:
        with holder.cursor() as cur:
            cur.execute("SELECT 1")
            holder.cursor  # noqa: B018 -- reached only if the holder still lives
        holder_dead = False
    except psycopg.Error:
        holder_dead = True
    assert holder_dead, "BRAIN_TEST_DB_RESET=1 must keep the drop behaviour"
    assert _marker_count(url) == 0, "the rebuilt DB should not contain old rows"
    with psycopg.connect(url, connect_timeout=3) as conn:
        stamped = conn.execute("SELECT version_num FROM alembic_version").fetchone()[0]
    assert stamped != UNKNOWN_REVISION, "the DB was not re-migrated to a known head"
