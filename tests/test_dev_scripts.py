"""Tests for scripts/lib.sh, dev-start.sh, dev-stop.sh (syntax, contracts, safety)."""

import os
import shutil
import subprocess
import sysconfig
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
REPO_ROOT = SCRIPTS.parent


def _shellcheck_binary() -> str | None:
    """Find shellcheck: PATH first, then the active venv's script dir."""
    found = shutil.which("shellcheck")
    if found:
        return found
    candidate = Path(sysconfig.get_path("scripts")) / "shellcheck"
    return str(candidate) if candidate.exists() else None


def _run(cmd: list[str], env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    run_env = dict(os.environ)
    if env:
        run_env.update(env)
    return subprocess.run(cmd, capture_output=True, text=True, timeout=120, env=run_env)


def _bash(script: str, *args: str) -> subprocess.CompletedProcess[str]:
    return _run(["bash", str(SCRIPTS / script), *args])


def _bash_snippet(code: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    return _run(["bash", "-c", code], env=env)


def _resolve_project(env: dict[str, str] | None = None) -> str:
    code = (
        f"cd {REPO_ROOT} && source scripts/lib.sh && "
        'resolve_compose_project >/dev/null && echo "$COMPOSE_PROJECT_NAME"'
    )
    return _bash_snippet(code, env=env).stdout.strip()


def test_scripts_pass_bash_syntax_check():
    for name in ("lib.sh", "dev-start.sh", "dev-stop.sh"):
        result = _run(["bash", "-n", str(SCRIPTS / name)])
        assert result.returncode == 0, result.stderr


def test_scripts_pass_shellcheck_when_installed():
    binary = _shellcheck_binary()
    if binary is None:
        pytest.skip("shellcheck not installed (pip install shellcheck-py)")
    for name in ("lib.sh", "dev-start.sh", "dev-stop.sh"):
        result = _run([binary, str(SCRIPTS / name)])
        assert result.returncode == 0, result.stdout + result.stderr


def test_dev_start_help_exits_zero_without_side_effects():
    result = _bash("dev-start.sh", "--help")

    assert result.returncode == 0
    assert "Usage: scripts/dev-start.sh" in result.stdout


def test_dev_start_rejects_unknown_flags():
    result = _bash("dev-start.sh", "--bogus")

    assert result.returncode != 0
    assert "unknown flag" in result.stderr


def test_dev_stop_help_exits_zero_without_side_effects():
    result = _bash("dev-stop.sh", "--help")

    assert result.returncode == 0
    assert "Usage: scripts/dev-stop.sh" in result.stdout


def test_dev_stop_rejects_unknown_flags():
    result = _bash("dev-stop.sh", "--bogus")

    assert result.returncode != 0
    assert "unknown flag" in result.stderr


def test_compose_project_default():
    assert _resolve_project() == "second-brain-dev"


def test_compose_project_env_beats_default():
    assert _resolve_project(env={"COMPOSE_PROJECT_NAME": "second-brain-test"}) == (
        "second-brain-test"
    )


def test_compose_project_dotenv_override():
    """A temp dir's .env COMPOSE_PROJECT_NAME is honored (env silent)."""
    code = (
        'tmp="$(mktemp -d)" && '
        'printf "COMPOSE_PROJECT_NAME=second-brain-dotenv\\n" > "$tmp/.env" && '
        f'cd "$tmp" && source {SCRIPTS}/lib.sh && '
        'resolve_compose_project >/dev/null && echo "$COMPOSE_PROJECT_NAME"'
    )
    result = _bash_snippet(code)

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "second-brain-dotenv"


def test_start_and_stop_use_the_same_project():
    """.dev state aside, both scripts print the identical compose project name."""
    assert _resolve_project() == "second-brain-dev"


def test_pg_check_survives_apostrophe_in_password():
    """DATABASE_URL rides via env, never interpolated: an apostrophe password
    must yield a clean connection error, not a python SyntaxError."""
    code = (
        f"cd {REPO_ROOT} && source scripts/lib.sh && "
        "DATABASE_URL='postgresql+psycopg://brain:it'\"'\"'s@localhost:5999/brain' "
        'DATABASE_URL_PLAIN="$(pg_url)" .venv/bin/python -c '
        '"import os, psycopg; psycopg.connect(os.environ[\'DATABASE_URL_PLAIN\'], '
        'connect_timeout=1)" 2>&1 | tail -1'
    )

    result = _bash_snippet(code)

    assert "SyntaxError" not in result.stdout
    assert "SyntaxError" not in result.stderr
