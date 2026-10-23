"""T1: a pytest session must start from a clean *_test database.

The test database persists across pytest sessions (created once, migrated,
never dropped). Residue from an INTERRUPTED earlier session (a run killed
mid-test, before the per-test teardowns ran) used to leak into the next
run: the email-attachments test asserted with ``query(Attachment).one()``
on the whole table, so one stray row failed it exactly once — a heisenbug
(the failing run's own teardown cleaned the residue up behind it).

Two fixes, proven here:
- the session-scoped ``database`` fixture TRUNCATEs the app tables after
  migrations, guarded by ``assert_test_database`` (connector tables only
  when present), so a fresh session never inherits residue;
- the email test scopes its row lookup to its own parent item.

The residue tests plant a stray row and run a target test in a SUBPROCESS
(fresh session, fresh conftest) — the only honest way to observe a
session-start reset from inside a session.
"""

import os
import subprocess
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]

_EMPTY_TABLE_TEST = (
    "tests/test_db_session_reset.py::TestSessionReset"
    "::test_attachments_table_is_empty_at_session_start"
)
_EMAIL_TEST = (
    "tests/services/test_attachments_email.py::TestEmailAttachments"
    "::test_attachment_bytes_captured_with_source_email"
)


def _plant_stray_attachment() -> tuple[object, object]:
    """One parent item + one attachment row, mimicking interrupted-session residue."""
    from brain.db.models import Attachment, MemoryItem
    from brain.db.session import SessionLocal

    with SessionLocal() as session:
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


def _delete_planted(parent_id: object, att_id: object) -> None:
    """Remove the planted rows so the rest of THIS session stays clean."""
    from brain.db.models import Attachment, MemoryItem
    from brain.db.session import SessionLocal

    with SessionLocal() as session:
        session.query(Attachment).filter(Attachment.id == att_id).delete()
        session.query(MemoryItem).filter(MemoryItem.id == parent_id).delete()
        session.commit()


def _run_pytest_in_fresh_session(node_id: str) -> subprocess.CompletedProcess:  # type: ignore[type-arg]
    """One pytest run = one session: the subprocess must see a clean DB."""
    return subprocess.run(  # noqa: S603 - fixed argv, no shell
        [sys.executable, "-m", "pytest", "-q", "--timeout=60", "--timeout-method=thread", node_id],
        cwd=_REPO_ROOT,
        env=dict(os.environ),
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )


class TestSessionReset:
    def test_attachments_table_is_empty_at_session_start(self, database: None):
        """The assertion target of the subprocess run: zero residue at start."""
        from brain.db.models import Attachment
        from brain.db.session import SessionLocal

        with SessionLocal() as session:
            assert session.query(Attachment).count() == 0

    def test_session_start_truncate_clears_residue(self):
        """A stray row planted 'before the session' must not survive into it."""
        parent_id, att_id = _plant_stray_attachment()
        try:
            result = _run_pytest_in_fresh_session(_EMPTY_TABLE_TEST)
        finally:
            _delete_planted(parent_id, att_id)
        assert result.returncode == 0, (
            "residue from an interrupted session leaked into a fresh session:\n"
            f"{result.stdout}\n{result.stderr}"
        )

    def test_email_attachment_test_survives_stray_rows(self):
        """End to end: with residue present, the flaky test must still pass."""
        parent_id, att_id = _plant_stray_attachment()
        try:
            result = _run_pytest_in_fresh_session(_EMAIL_TEST)
        finally:
            _delete_planted(parent_id, att_id)
        assert result.returncode == 0, (
            "the email-attachments test breaks on residue from an earlier session:\n"
            f"{result.stdout}\n{result.stderr}"
        )
