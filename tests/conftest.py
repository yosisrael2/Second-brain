"""Shared pytest fixtures.

SAFETY: the suite never touches a developer's real database. DATABASE_URL is
forced to a ``*_test`` database (created on demand); any non-test URL fails
fast, and the destructive fixtures double-check the engine URL before deleting.
"""

import hashlib
import os
import re

from brain.config import test_collection_for

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
# Pin a *_test collection; refuse non-test collections outright. When the
# caller did not pick one explicitly, DERIVE it from the test database name
# (brain_w5_test -> brain_items_w5_test): concurrent suites from different
# workers then never share a collection — vector pollution between suites
# was a real incident (see README "Running the tests").
_env_collection = os.environ.get("QDRANT_COLLECTION")
if _env_collection is not None and not _env_collection.endswith("_test"):
    raise RuntimeError(
        "refusing to run tests against non-test qdrant collection "
        f"{_env_collection!r}; point QDRANT_COLLECTION at a *_test collection"
    )
if _env_collection is None:
    _db_name = os.environ["DATABASE_URL"].rsplit("/", 1)[-1].split("?")[0]
    assert _db_name.endswith("_test"), f"test database {_db_name!r} must end in _test"
    os.environ["QDRANT_COLLECTION"] = test_collection_for(_db_name)

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


# --- session-start DB reset (T1) ----------------------------------------------
# The *_test database persists across pytest sessions; an interrupted run (a
# test killed mid-flight, before its per-test teardown) leaves rows behind
# that leak into the NEXT session. Truncate the app tables after migrations
# so every session starts empty. Guarded like every destructive fixture, so
# it can never touch a non-test database; connector tables are included only
# when present (they arrive with the connectors migration).
_RESET_TABLES = (
    "memory_items",  # CASCADE: attachments, item_sources, item_relations, ... hang off it
    "attachments",
    "item_relations",
    "connector_state",
    "connector_records",
    "ops_events",
)


def _reset_test_tables() -> None:
    assert_test_database(_database_url())
    with engine.begin() as conn:
        present = {
            row[0]
            for row in conn.execute(
                text("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
            )
        }
        for name in _RESET_TABLES:
            if name in present:
                conn.execute(text(f'TRUNCATE TABLE "{name}" CASCADE'))


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
    _reset_test_tables()  # T1: a session never inherits an interrupted one's rows
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

    If the ``*_test`` database is stamped at a revision this checkout's
    scripts do not know (ResolutionError on upgrade — almost always ANOTHER
    checkout's branch, which added migrations), the session FAILS FAST
    naming the database and the stamped revision: silently dropping the DB
    here terminated every live connection of a RUNNING sibling suite and
    rebuilt the DB under it (the 25 Oct incident — worker_7's
    ``brain_w7_mem_test``, stamped ``7d90f6ea8407``, was dropped 4+ times
    mid-suite by main-scripts sessions, ~40 failures each). The old
    drop-and-rebuild behaviour stays available behind the explicit
    ``BRAIN_TEST_DB_RESET=1`` opt-in, guarded by ``assert_test_database``
    like every destructive fixture.
    """
    try:
        _upgrade_head()
    except Exception as exc:
        unknown = "Can't locate revision" in str(exc) or "not a valid upgrade target" in str(exc)
        if not unknown:
            raise
        if os.environ.get("BRAIN_TEST_DB_RESET") != "1":
            identified = re.search(r"identified by '([^']+)'", str(exc))
            stamped = identified.group(1) if identified else "(unparseable)"
            raise RuntimeError(
                f"test database {_database_name(_database_url())!r} is stamped at "
                f"revision {stamped!r}, which this checkout's migrations do not "
                "know; this *_test DB is stamped by a different checkout; set "
                "BRAIN_TEST_DB_RESET=1 to drop and rebuild it"
            ) from exc
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


# --- live-collection tripwire (session level) ---------------------------------
# The PR #42 incident: a test vector landed in the LIVE qdrant collection and
# nothing noticed. REFINED 24 Oct (false positive: the live instance ingested
# a real note mid-suite and the raw count delta failed the run): the guard now
# detects TEST-originated points — every vector a test writes carries a
# test-origin payload marker, and the session guard asserts the LIVE
# collection holds zero of them. The raw count delta is informational only.
# Read-only (one REST GET + one count POST); qdrant unreachable stays silent.

from tests.live_guard import (  # noqa: E402
    TEST_ORIGIN_KEY,
    TEST_ORIGIN_VALUE,
    assert_no_test_points_in_live,
    count_delta_note,
    live_marker_point_count,
    live_point_count,
)

# --- repo .env tripwire (session level) --------------------------------------
# 24 Oct incident: worker worktrees kept re-acquiring a live-pointing .env
# (byte-identical to .env.example; the live dbname default). Culprit (bisected):
# test_dev_start_stops_a_never_healthy_server runs the REAL dev-start.sh, whose
# check_env_file does `cp .env.example .env` when .env is missing — so any full
# suite in a provisioned worktree without .env recreated it. No test may create
# or modify files in the repo root: .env is snapshotted here (existence +
# sha256) and the session fails loudly on any change. dev-start's developer
# behavior (create when missing) is untouched — real-script tests point it at a
# temp env file via BRAIN_ENV_FILE instead.


def _repo_env_state() -> tuple[bool, str | None]:
    env_file = _REPO_ROOT / ".env"
    if not env_file.exists():
        return (False, None)
    return (True, hashlib.sha256(env_file.read_bytes()).hexdigest())


def pytest_sessionstart(session: pytest.Session) -> None:
    session.config._live_points_before = live_point_count(  # type: ignore[attr-defined]
        os.environ.get("QDRANT_URL", "http://localhost:27333")
    )
    session.config._repo_env_before = _repo_env_state()  # type: ignore[attr-defined]


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    qdrant_url = os.environ.get("QDRANT_URL", "http://localhost:27333")
    try:
        assert_no_test_points_in_live(live_marker_point_count(qdrant_url))
    except AssertionError as exc:
        print(f"\nERROR: {exc}")
        session.testsfailed = max(session.testsfailed or 0, 1)
    before = getattr(session.config, "_live_points_before", None)
    after = live_point_count(qdrant_url)
    note = count_delta_note(before, after)
    if note:
        print(f"\nNOTE: {note}")
    env_before = getattr(session.config, "_repo_env_before", None)
    if env_before is not None and _repo_env_state() != env_before:
        # testsfailed is already computed into the exit status by the time
        # sessionfinish runs — the loud exit needs the exit hook escape.
        from pytest import exit as pytest_exit  # noqa: PLC0415

        pytest_exit(
            "REPO_ROOT/.env was created or modified during this test session "
            "— no test may write to the repo root. Real-script tests must "
            "point the env-file seam (BRAIN_ENV_FILE) at a temp file.",
            returncode=101,
        )


@pytest.fixture(autouse=True)
def _tag_test_vector_writes(monkeypatch: pytest.MonkeyPatch):
    """Every vector a TEST writes through the real store carries the
    test-origin marker in its payload — the live guard counts those markers
    instead of raw point counts (an external live write then cannot fail a
    session, while a test writing into the live collection still is caught).
    Test-side only: the production upsert path is untouched (the fixture
    wraps QdrantVectorStore.upsert_many for the duration of each test)."""
    from brain.ai.vectorstore import QdrantVectorStore

    real_upsert_many = QdrantVectorStore.upsert_many

    def tagged_upsert_many(self, points):
        tagged = [
            (item_id, vector, {**dict(payload), TEST_ORIGIN_KEY: TEST_ORIGIN_VALUE})
            for item_id, vector, payload in points
        ]
        return real_upsert_many(self, tagged)

    monkeypatch.setattr(QdrantVectorStore, "upsert_many", tagged_upsert_many)

# F3: the attachments-core fixture (tests/test_attachments.py) shared with the
# poller test modules. Module-level alias — pytest collects fixture objects by
# value, so the same fixture instance is available as `svc_env` everywhere.
from tests.test_attachments import svc_env_fixture as svc_env  # noqa: E402,F401
