"""T1: a pytest session must start from a clean *_test database.

The test database persists across pytest sessions, and residue from an
INTERRUPTED earlier session (a run killed mid-test, before the per-test
teardowns) used to leak into the next run: the email-attachments test
asserted with ``query(Attachment).one()`` on the whole table, so one stray
row failed it exactly once — a heisenbug (the failing run's own teardown
cleaned the residue up behind it). Seen once each by workers 7 and 10.

Two fixes, proven here:
- the session-scoped ``database`` fixture TRUNCATEs the app tables after
  migrations, guarded by ``assert_test_database`` (connector tables only
  when present), so a fresh session never inherits residue;
- the email test scopes its row lookup to its own parent item.

ISOLATION (review of #62): the residue tests plant rows and run a target
test in a SUBPROCESS — but that nested session start-truncates whatever
DATABASE_URL it gets. Pointing it at the parent's database would wipe
parent-seeded rows mid-run (demonstrated on 80d7cf4: a planted attachment
vanished across one nested run) — creating exactly the flake this file
cures. So the subprocess gets its OWN database: the parent URL with
``_test`` -> ``_sub_test``, created + migrated by the same conftest
helpers, with its own derived qdrant collection.
``test_subprocess_resets_never_touch_the_parent_database`` pins that: a
row the parent plants before the subprocess must still exist after it.
"""

import os
import subprocess
import sys
from pathlib import Path

from tests.conftest import assert_test_database

from brain.db.models import Attachment, MemoryItem
from brain.db.session import SessionLocal

_REPO_ROOT = Path(__file__).resolve().parents[1]

_EMPTY_TABLE_TEST = (
    "tests/test_db_session_reset.py::TestSessionReset"
    "::test_attachments_table_is_empty_at_session_start"
)
_EMAIL_TEST = (
    "tests/services/test_attachments_email.py::TestEmailAttachments"
    "::test_attachment_bytes_captured_with_source_email"
)

_factories: dict[str, object] = {}


def _database_name(url: str) -> str:
    """Extract the database name from a SQLAlchemy/psycopg URL."""
    return url.rsplit("/", 1)[-1].split("?")[0]


def _sub_database_url() -> str:
    """The subprocess's own database: <parent db>_test -> <parent db>_sub_test."""
    url = os.environ["DATABASE_URL"]
    db_name = _database_name(url)
    assert db_name.endswith("_test"), f"parent test db {db_name!r} must end in _test"
    left, _, _rest = url.rpartition("/")
    return f"{left}/{db_name[: -len('_test')]}_sub_test"


def _factory_for(url: str):
    """A sessionmaker bound to `url`, refusing non-test databases (cached)."""
    assert_test_database(url)
    if url not in _factories:
        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker

        _factories[url] = sessionmaker(bind=create_engine(url))
    return _factories[url]


def _sub_factory():
    return _factory_for(_sub_database_url())


def _plant_stray_attachment(factory) -> tuple[object, object]:
    """One parent item + one attachment row, mimicking interrupted-session residue."""
    with factory() as session:
        parent = MemoryItem(raw_text="stray from an earlier session", status="inbox")
        session.add(parent)
        session.flush()
        att = Attachment(
            item_id=parent.id,
            parent_item_id=parent.id,
            sha256="0" * 64,
            mime="text/plain",
            filename="stray.txt",
            size_bytes=4,
            source="email",
            source_ref="stray-m1",
            status="done",
        )
        session.add(att)
        session.commit()
        return parent.id, att.id


def _delete_planted(factory, parent_id: object, att_id: object) -> None:
    """Remove the planted rows so the rest of THIS session stays clean."""
    with factory() as session:
        session.query(Attachment).filter(Attachment.id == att_id).delete()
        session.query(MemoryItem).filter(MemoryItem.id == parent_id).delete()
        session.commit()


def _run_pytest_in_sub_session(node_id: str) -> subprocess.CompletedProcess:
    """One pytest run = one session, on the subprocess's OWN database."""
    env = dict(os.environ)
    env["DATABASE_URL"] = _sub_database_url()
    env.pop("QDRANT_COLLECTION", None)  # the sub conftest derives its own
    return subprocess.run(  # noqa: S603 - fixed argv, no shell
        [sys.executable, "-m", "pytest", "-q", "--timeout=60", "--timeout-method=thread", node_id],
        cwd=_REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )


def _ensure_sub_database() -> None:
    """First subprocess run: conftest CREATE DATABASEs + migrates the sub DB."""
    result = _run_pytest_in_sub_session(_EMPTY_TABLE_TEST)
    assert result.returncode == 0, (
        f"could not prepare the subprocess database:\n{result.stdout}\n{result.stderr}"
    )


class TestSessionReset:
    def test_attachments_table_is_empty_at_session_start(self, database: None):
        """The assertion target of the subprocess run: zero residue at start."""
        with SessionLocal() as session:
            assert session.query(Attachment).count() == 0

    def test_session_start_truncate_clears_residue(self):
        """A stray row planted 'before the session' must not survive into it."""
        _ensure_sub_database()
        factory = _sub_factory()
        parent_id, att_id = _plant_stray_attachment(factory)
        try:
            result = _run_pytest_in_sub_session(_EMPTY_TABLE_TEST)
        finally:
            _delete_planted(factory, parent_id, att_id)
        assert result.returncode == 0, (
            "residue from an interrupted session leaked into a fresh session:\n"
            f"{result.stdout}\n{result.stderr}"
        )

    def test_email_attachment_test_survives_stray_rows(self):
        """End to end: with residue present, the flaky test must still pass."""
        _ensure_sub_database()
        factory = _sub_factory()
        parent_id, att_id = _plant_stray_attachment(factory)
        try:
            result = _run_pytest_in_sub_session(_EMAIL_TEST)
        finally:
            _delete_planted(factory, parent_id, att_id)
        assert result.returncode == 0, (
            "the email-attachments test breaks on residue from an earlier session:\n"
            f"{result.stdout}\n{result.stderr}"
        )

    def test_subprocess_resets_never_touch_the_parent_database(self):
        """The parent's rows must survive a subprocess that resets ITS OWN db.

        Regression for the #62 review finding: a nested session pointed at
        the parent's DATABASE_URL truncated the parent's tables mid-run (a
        planted attachment row vanished across one nested run).
        """
        parent_id, att_id = _plant_stray_attachment(SessionLocal)
        try:
            _ensure_sub_database()
            factory = _sub_factory()
            sub_parent_id, sub_att_id = _plant_stray_attachment(factory)
            try:
                result = _run_pytest_in_sub_session(_EMPTY_TABLE_TEST)
            finally:
                _delete_planted(factory, sub_parent_id, sub_att_id)
            assert result.returncode == 0, (
                f"subprocess run failed:\n{result.stdout}\n{result.stderr}"
            )
            with SessionLocal() as session:
                assert session.get(Attachment, att_id) is not None, (
                    "the subprocess reset wiped the PARENT database "
                    f"(attachment {att_id} gone)"
                )
        finally:
            _delete_planted(SessionLocal, parent_id, att_id)
