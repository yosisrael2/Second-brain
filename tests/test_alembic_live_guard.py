"""Alembic must never touch the LIVE database by accident (PR #53 review follow-up).

Root-cause class (incident 2026-10-24): a worktree .env is a byte-copy of
``.env.example``, whose ``DATABASE_URL`` default is the live dbname on the live
port; any bare ``alembic upgrade`` run from such a CWD migrated LIVE. The app
itself is guarded at engine construction (#45: ``assert_not_live_database``),
but ``src/brain/db/migrations/env.py`` built its OWN engine via
``engine_from_config`` and never checked — the hole this file pins shut.

The guard must hold in BOTH alembic modes:
- online (default): refuses BEFORE any connection is attempted;
- offline (``--sql``): refuses before a single line of SQL is emitted.
``BRAIN_ALLOW_LIVE=1`` is the explicit deploy-path opt-in (same var the
run-*.sh launchd scripts already export).

Safety of these tests on the live machine: every live-shaped URL uses a
WRONG password — if the guard ever regressed, the connection dies at
password auth and no SQL can run (the test then fails loudly, exactly what
a guard regression test must do). The offline tests never open a connection
at all. (Note: offline ``--sql`` over the whole chain is independently
broken on main — the datetime-repair migration d2f6a9c4e7b1 calls
op.get_bind() at upgrade time — which is why the opt-in and test-url cases
below run ONLINE instead.)
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]

# Live-shaped on purpose: same host/port/dbname as the real service. The
# password is deliberately WRONG so that a guard regression fails at auth
# instead of ever reaching a migration. NEVER put the real password here.
_LIVE_SHAPED_URL = "postgresql+psycopg://brain:guard-red-herring@localhost:27432/brain"


def _run_alembic(*args: str, env_overrides: dict[str, str]) -> subprocess.CompletedProcess[str]:
    """Run alembic in a fresh subprocess with a controlled environment.

    Subprocess on purpose: env.py reads settings from the environment, and a
    fresh interpreter proves the real CLI path (no in-process caching).
    """
    env = {
        **os.environ,
        **env_overrides,
    }
    env.pop("BRAIN_ALLOW_LIVE", None)
    env.update(env_overrides)
    return subprocess.run(
        [sys.executable, "-m", "alembic", "-c", str(_REPO_ROOT / "alembic.ini"), *args],
        capture_output=True,
        text=True,
        cwd=_REPO_ROOT,
        env=env,
        timeout=120,
    )


def test_alembic_offline_refuses_the_live_database_url() -> None:
    """`alembic upgrade head --sql` against a live-dbname URL must refuse.

    RED TODAY (pre-guard): the command exited 0 and PRINTED the migration SQL
    for the live URL — alembic env.py had no live-store check at all.
    """
    result = _run_alembic(
        "upgrade", "head", "--sql",
        env_overrides={"DATABASE_URL": _LIVE_SHAPED_URL},
    )
    assert result.returncode != 0, (
        "alembic offline mode happily generated SQL for the LIVE database "
        f"(exit 0). stderr was: {result.stderr!r}"
    )
    assert "refusing to start against the LIVE store" in result.stderr
    assert "BRAIN_ALLOW_LIVE" in result.stderr  # the message must name the opt-in


def test_alembic_online_refuses_the_live_database_url_before_connecting() -> None:
    """Online mode must refuse BEFORE connecting (the incident's exact hole).

    RED TODAY (pre-guard): env.py connected and failed later (auth), never
    having checked. The wrong password keeps a regression harmless: the
    connection dies at auth, no migration can run, and the assertion below
    fails for the right reason.
    """
    result = _run_alembic(
        "upgrade", "head",
        env_overrides={"DATABASE_URL": _LIVE_SHAPED_URL},
    )
    assert result.returncode != 0
    assert "refusing to start against the LIVE store" in result.stderr, (
        "expected the #45 live-store guard message; got: "
        f"{(result.stdout + result.stderr)[-2000:]!r}"
    )
    assert "BRAIN_ALLOW_LIVE" in result.stderr


def test_alembic_online_allow_live_opt_in_still_reaches_the_database() -> None:
    """BRAIN_ALLOW_LIVE=1 = the deploy path: the guard must NOT refuse.

    Safe on any machine: the URL is live-shaped but the password is wrong, so
    the proof that the guard LET IT THROUGH is the AUTH failure (a real
    connection attempt was made) — and an auth failure can never run a
    migration. Asserting on the error kind is what pins 'opt-in works'.
    """
    result = _run_alembic(
        "upgrade", "head",
        env_overrides={"DATABASE_URL": _LIVE_SHAPED_URL, "BRAIN_ALLOW_LIVE": "1"},
    )
    assert result.returncode != 0  # it DID try to connect (and auth failed)
    assert "refusing to start against the LIVE store" not in result.stderr
    assert "password authentication failed" in result.stderr


def test_alembic_online_test_database_url_still_works() -> None:
    """The suite's own *_test URL keeps working without any opt-in.

    Same in-process command API conftest itself uses (tests/conftest.py
    _upgrade_head): against the suite DATABASE_URL this is a no-op at head —
    tests/CI must be unaffected by the guard.
    """
    from alembic import command
    from alembic.config import Config

    cfg = Config(str(_REPO_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(_REPO_ROOT / "src/brain/db/migrations"))
    command.upgrade(cfg, "head")  # raises on refusal; no-op when already at head
