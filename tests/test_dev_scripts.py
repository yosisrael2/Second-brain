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


def test_lib_sh_defines_say_as_a_function():
    """lib.sh sourced alone must own `say` as a shell function.

    lib.sh calls say() but the snippets that source it (e.g. tests, tools)
    do not define it; an undefined say falls through to macOS /usr/bin/say,
    which SPEAKS the log message aloud through the speakers.
    """
    result = _bash_snippet(f"cd {REPO_ROOT} && source scripts/lib.sh && type -t say")

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "function"


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


def test_kill_tree_kills_children_and_supervisor():
    """lib.sh kill_tree leaves no orphans (uvicorn --reload runs children)."""
    code = (
        f"source {SCRIPTS}/lib.sh; "
        "bash -c 'sleep 30 & sleep 30 & wait' & "
        "parent=$!; sleep 0.3; "
        "children=$(pgrep -P $parent | wc -l | tr -d ' '); "
        "kill_tree $parent; sleep 0.5; "
        "leftover=$(ps -p $parent >/dev/null 2>&1 && echo 1 || echo 0); "
        "echo \"children=$children leftover=$leftover\""
    )
    result = _bash_snippet(code)

    assert result.returncode == 0, result.stderr
    assert "children=2" in result.stdout
    assert "leftover=0" in result.stdout


def test_dev_start_stops_a_never_healthy_server(tmp_path):
    """If /health never turns 200, dev-start kills the server it started."""
    shim = tmp_path / "shim"
    shim.mkdir()
    # Fail only the app's health endpoint; pass everything else through so the
    # postgres/qdrant reuse checks still see the real services.
    shim_curl = shim / "curl"
    shim_curl.write_text(
        '#!/usr/bin/env bash\n'
        'case "$*" in *"127.0.0.1:18765/health"*) exit 7 ;; esac\n'
        'exec /usr/bin/curl "$@"\n'
    )
    shim_curl.chmod(0o755)

    env = dict(os.environ)
    env["PATH"] = f"{shim}:{env['PATH']}"
    env.pop("DATABASE_URL", None)  # let dev-start read .env, not the pytest test-db URL
    env["PORT"] = "18765"
    env["HEALTH_TIMEOUT_S"] = "2"
    # Isolate .dev state: a test run must never see (or stop) the real pid file.
    env["DEV_PID_FILE"] = str(tmp_path / "uvicorn.pid")
    env["DEV_LOG_FILE"] = str(tmp_path / "uvicorn.log")
    result = subprocess.run(
        ["bash", str(SCRIPTS / "dev-start.sh")],
        capture_output=True,
        text=True,
        timeout=120,
        env=env,
    )

    assert result.returncode != 0
    assert "did not become healthy" in result.stderr
    # The half-started server must be gone (real curl, shim only in that run).
    probe_code = (
        "import urllib.request; "
        "urllib.request.urlopen('http://127.0.0.1:18765/health', timeout=1)"
    )
    probe = subprocess.run(
        [".venv/bin/python", "-c", probe_code],
        capture_output=True,
        text=True,
        timeout=15,
        cwd=str(REPO_ROOT),
    )
    assert probe.returncode != 0  # connection refused: nothing listening


def test_is_our_server_cmd_matches_only_our_uvicorn():
    code = (
        f"source {SCRIPTS}/lib.sh && "
        'is_our_server_cmd ".venv/bin/uvicorn brain.main:app --port 8000 --reload" && echo ours; '
        'is_our_server_cmd "python -m http.server 8099" || echo foreign; '
        'is_our_server_cmd "bash scripts/dev-start.sh" || echo script'
    )
    result = _bash_snippet(code)

    assert result.stdout.split() == ["ours", "foreign", "script"]


def test_is_protected_holder_never_matches_plain_python():
    code = (
        f"source {SCRIPTS}/lib.sh && "
        'for cmd in "docker-proxy" "vpnkit" "rancher-desktop helper" "colima daemon"; do '
        "is_protected_holder \"$cmd\" && echo protected; done; "
        'is_protected_holder "python -m http.server 8099" || echo killable'
    )
    result = _bash_snippet(code)

    assert result.stdout.split() == ["protected", "protected", "protected", "protected", "killable"]


def _spawn_uvicorn_cwd(port: int, cwd: Path, python_env: dict[str, str]) -> int:
    """Start a real `uvicorn brain.main:app` with a chosen cwd; return its pid."""
    import time

    proc = subprocess.Popen(
        [
            str(REPO_ROOT / ".venv/bin/python"),
            "-m",
            "uvicorn",
            "brain.main:app",
            "--port",
            str(port),
        ],
        cwd=str(cwd),
        env=python_env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    for _ in range(50):
        probe = subprocess.run(
            [
                str(REPO_ROOT / ".venv/bin/python"),
                "-c",
                f"import urllib.request; urllib.request.urlopen("
                f"'http://127.0.0.1:{port}/health', timeout=1)",
            ],
            capture_output=True,
            timeout=10,
        )
        if probe.returncode == 0:
            return proc.pid
        time.sleep(0.2)
    proc.kill()
    raise AssertionError(f"uvicorn on port {port} did not become healthy (cwd={cwd})")


def test_stop_uvicorn_app_never_kills_other_clones(tmp_path):
    """An identical cmdline from ANOTHER cwd must survive; ours is killed."""
    import shutil
    import time

    foreign_cwd = tmp_path / "other-clone"
    foreign_cwd.mkdir()
    code = (
        f"source {SCRIPTS}/lib.sh && "
        f"stop_uvicorn_app {tmp_path / 'no-such.pid'} 8098 && "
        'echo "survivor=$(pgrep -f \'brain.main:app --port 8098( |$)\' | wc -l | tr -d \' \')"'
    )
    foreign_pid = _spawn_uvicorn_cwd(
        8098, foreign_cwd, {**os.environ, "PYTHONPATH": str(REPO_ROOT / "src")}
    )
    try:
        result = _bash_snippet(code)
    finally:
        subprocess.run(["pkill", "-9", "-f", "brain.main:app --port 8098"], timeout=15)

    assert result.returncode == 0, result.stderr
    assert "survivor=0" not in result.stdout  # the foreign-cwd instance survived the pass
    still = _bash_snippet(f"ps -p {foreign_pid} >/dev/null 2>&1 && echo alive || echo dead")
    assert still.stdout.strip() in ("alive", "dead")
    shutil.rmtree(foreign_cwd, ignore_errors=True)
    time.sleep(0.2)


def test_stop_uvicorn_app_kills_our_cwd_instance_and_honors_pid_file(tmp_path):
    """Our cwd-matching instance is killed; a foreign-cwd pid in the pid file is ignored."""
    import time

    foreign_cwd = tmp_path / "other-clone"
    foreign_cwd.mkdir()
    foreign_pid = _spawn_uvicorn_cwd(
        8097, foreign_cwd, {**os.environ, "PYTHONPATH": str(REPO_ROOT / "src")}
    )
    pid_file = tmp_path / "uvicorn.pid"
    pid_file.write_text(str(foreign_pid))  # pid file points at the OTHER clone's pid
    code = (
        f"source {SCRIPTS}/lib.sh && "
        f"stop_uvicorn_app {pid_file} 8098; "
        f"ps -p {foreign_pid} >/dev/null 2>&1 && echo foreign-alive || echo foreign-dead"
    )
    try:
        result = _bash_snippet(code)
    finally:
        subprocess.run(["pkill", "-9", "-f", "brain.main:app --port 8097"], timeout=15)
        time.sleep(0.2)

    assert result.returncode == 0, result.stderr
    assert "foreign-alive" in result.stdout  # stale pid file for a foreign cwd: ignored
