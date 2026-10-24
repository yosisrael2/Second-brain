"""Offline `alembic --sql` must render the whole migration chain (manager ticket).

``alembic upgrade head --sql`` NEVER connects (offline mode renders SQL to
stdout). Migrations that call ``op.get_bind()`` die there with
``AttributeError: 'NoneType' object has no attribute 'execute'`` because no
bind exists offline — worker_8 hit this on d2f6a9c4e7b1 while writing #70.
These tests run the renderer in a subprocess against a *_test URL (offline
mode does not connect — the URL only satisfies settings) and assert the whole
chain renders with exit 0.
"""

import os
import subprocess
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
# my own *_test database; offline mode never opens a connection anyway
_TEST_URL = "postgresql+psycopg://brain:brain@localhost:27432/brain_w5_test"


def _render(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=_ROOT,
        env={**os.environ, "DATABASE_URL": _TEST_URL},
        capture_output=True,
        text=True,
        timeout=120,
    )


def test_alembic_upgrade_head_sql_renders_the_whole_chain_offline():
    proc = _render("upgrade", "head", "--sql")

    assert proc.returncode == 0, (
        f"offline --sql failed (rc={proc.returncode}):\n{proc.stderr[-2000:]}"
    )
    assert "CREATE TABLE" in proc.stdout or "ALTER TABLE" in proc.stdout, (
        "offline --sql rendered no schema SQL"
    )


def test_alembic_downgrade_sql_renders_offline_through_the_guarded_migration():
    """a1e5f7b9c3d2's downgrade guards on live rows via op.get_bind() — the
    offline renderer must survive it too (same AttributeError pattern)."""
    proc = _render("downgrade", "head:c7d91e4a2b58", "--sql")

    assert proc.returncode == 0, (
        f"offline downgrade --sql failed (rc={proc.returncode}):\n{proc.stderr[-2000:]}"
    )
    assert proc.stdout.strip(), "offline downgrade --sql rendered nothing"
