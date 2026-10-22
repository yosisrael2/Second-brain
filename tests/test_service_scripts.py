"""Tests for the launchd service layer: lib.sh ownership under symlinked paths,
run-server.sh (foreground API launcher), run-bot.sh (KeepAlive exit-code
contract), and service.sh (install/uninstall/status/restart/logs).

All external state is stubbed via PATH shims (launchctl, docker, pmset) and
BRAIN_BIN/UVICORN_BIN overrides; tests never install anything, never touch
launchd, and never talk to the real Telegram API.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sysconfig
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
REPO_ROOT = SCRIPTS.parent

need_venv = pytest.mark.skipif(
    not (REPO_ROOT / ".venv/bin/brain").exists(),
    reason="REPO_ROOT/.venv is not provisioned (service.sh install preflights it)",
)


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


def _bash_snippet(code: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    return _run(["bash", "-c", code], env=env)


class TestScriptHygiene:
    def test_pass_bash_syntax_check(self):
        for name in ("run-server.sh", "run-bot.sh", "service.sh"):
            result = _run(["bash", "-n", str(SCRIPTS / name)])
            assert result.returncode == 0, f"{name}: {result.stderr}"

    def test_pass_shellcheck_when_installed(self):
        binary = _shellcheck_binary()
        if binary is None:
            pytest.skip("shellcheck not installed (pip install shellcheck-py)")
        for name in ("run-server.sh", "run-bot.sh", "service.sh"):
            result = _run([binary, str(SCRIPTS / name)])
            assert result.returncode == 0, f"{name}: {result.stdout}{result.stderr}"

    def test_service_help_exits_zero_without_side_effects(self):
        result = _run([str(SCRIPTS / "service.sh"), "--help"])
        assert result.returncode == 0
        assert "Usage: scripts/service.sh" in result.stdout


class TestLibShPhysicalRoot:
    """is_our_instance must recognize instances even when the repo is reached
    through a symlink (macOS: /tmp -> /private/tmp). lsof reports the PHYSICAL
    cwd; LIB_ROOT built with a logical pwd compares unequal and our own
    instances stop being ours."""

    def test_is_our_instance_matches_physical_cwd_of_symlinked_repo(self, tmp_path):
        repo = tmp_path / "repo"
        (repo / "scripts").mkdir(parents=True)
        shutil.copy(SCRIPTS / "lib.sh", repo / "scripts" / "lib.sh")
        # A symlinked alias of the repo, the way /tmp aliases /private/tmp.
        alias = tmp_path / "alias"
        alias.symlink_to(repo)
        # Fake server: real process whose command line matches our uvicorn
        # pattern, cwd = repo (physical).
        proc = subprocess.Popen(
            ["python3", "-c", "import time; time.sleep(30)",
             "uvicorn brain.main:app", "--port", "27800"],
            cwd=repo,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            # lib.sh is sourced THROUGH the symlinked alias, as a user or
            # script launched via /tmp/... would.
            code = (
                f"source {alias}/scripts/lib.sh && "
                f"if is_our_instance {proc.pid}; then echo OURS; else echo FOREIGN; fi"
            )
            result = _bash_snippet(code)
            assert result.returncode == 0, result.stderr
            assert "OURS" in result.stdout, (
                f"our own instance (cwd {repo}, reached via symlink {alias}) is "
                f"not recognized as ours: {result.stdout.strip()!r}"
            )
        finally:
            proc.kill()
            proc.wait()

    def test_lib_root_is_physical(self, tmp_path):
        """LIB_ROOT resolves symlinks (pwd -P), matching what lsof reports."""
        repo = tmp_path / "repo2"
        (repo / "scripts").mkdir(parents=True)
        shutil.copy(SCRIPTS / "lib.sh", repo / "scripts" / "lib.sh")
        alias = tmp_path / "alias2"
        alias.symlink_to(repo)
        code = f"cd {alias} && source scripts/lib.sh && " 'printf "%s" "$LIB_ROOT"'
        result = _bash_snippet(code)
        assert result.returncode == 0, result.stderr
        assert result.stdout == str(repo), (
            f"LIB_ROOT is logical ({result.stdout!r}); lsof reports physical "
            f"({repo}) — is_our_instance cwd comparison can never match"
        )


class TestRunBotExitContract:
    """run-bot.sh maps bot exit codes onto the KeepAlive contract:
    0 (disabled) and 78 (config/409) become 0 (launchd: no restart);
    everything else propagates (launchd: restart)."""

    def _run_wrapper(self, tmp_path, bot_rc):
        fake_bin = tmp_path / "bin"
        fake_bin.mkdir(exist_ok=True)
        fake_brain = fake_bin / "brain"
        fake_brain.write_text(f"#!/usr/bin/env bash\necho fake-bot rc={bot_rc}\nexit {bot_rc}\n")
        fake_brain.chmod(0o755)
        return _run([str(SCRIPTS / "run-bot.sh")], env={"BRAIN_BIN": str(fake_brain)})

    @pytest.mark.parametrize("bot_rc,expected", [(0, 0), (78, 0), (1, 1), (70, 70)])
    def test_exit_code_mapping(self, tmp_path, bot_rc, expected):
        result = self._run_wrapper(tmp_path, bot_rc)
        assert result.returncode == expected, result.stdout + result.stderr

    def test_wrapper_preserves_the_real_code_in_its_log_line(self, tmp_path):
        result = self._run_wrapper(tmp_path, 78)
        assert "exited 78" in result.stdout


class TestRunServer:
    def test_missing_venv_exits_0_no_restart_loop(self, tmp_path):
        """A missing venv is permanent: exit 0 so KeepAlive SuccessfulExit=false
        does NOT restart-loop (any non-zero exit would be restarted)."""
        result = _run(
            [str(SCRIPTS / "run-server.sh")],
            env={"UVICORN_BIN": str(tmp_path / "no-such-uvicorn")},
        )
        assert result.returncode == 0
        assert "missing" in result.stderr

    def test_runs_uvicorn_foreground_on_port(self, tmp_path):
        """run-server.sh execs uvicorn with the requested port and stays in
        the foreground (exec replaces the shell — no child to orphan)."""
        fake_bin = tmp_path / "bin2"
        fake_bin.mkdir()
        fake_uvicorn = fake_bin / "uvicorn"
        fake_uvicorn.write_text(
            "#!/usr/bin/env bash\n"
            'echo "ARGV: $*"; echo "CWD: $PWD"; echo "UNBUFFERED: ${PYTHONUNBUFFERED-unset}"\n'
        )
        fake_uvicorn.chmod(0o755)
        result = _run(
            [str(SCRIPTS / "run-server.sh")],
            env={"UVICORN_BIN": str(fake_uvicorn), "PORT": "27999"},
        )
        assert result.returncode == 0, result.stderr
        assert "--port 27999" in result.stdout
        assert "--host 127.0.0.1" in result.stdout
        assert "brain.main:app" in result.stdout
        assert "UNBUFFERED: 1" in result.stdout
        assert f"CWD: {REPO_ROOT}" in result.stdout

    def test_run_server_exports_allow_live(self, tmp_path):
        """run-server.sh is the launchd path: it MUST export BRAIN_ALLOW_LIVE=1
        so the live-store startup guard (brain.config.assert_not_live_store)
        lets the real service through, while a bare worktree uvicorn refuses."""
        fake_bin = tmp_path / "bin3"
        fake_bin.mkdir()
        fake_uvicorn = fake_bin / "uvicorn"
        fake_uvicorn.write_text('echo "ALLOW: ${BRAIN_ALLOW_LIVE-unset}"\n')
        fake_uvicorn.chmod(0o755)
        result = _run(
            [str(SCRIPTS / "run-server.sh")],
            env={"UVICORN_BIN": str(fake_uvicorn), "PORT": "27998"},
        )
        assert result.returncode == 0, result.stderr
        assert "ALLOW: 1" in result.stdout


class _LaunchctlStub:
    """PATH shim for launchctl: records invocations, fakes `print` output."""

    def __init__(self, tmp_path: Path, print_state: str = "running") -> None:
        self.bin = tmp_path / "stubbin"
        self.bin.mkdir(exist_ok=True)
        self.log = tmp_path / "launchctl-calls.log"
        stub = self.bin / "launchctl"
        body = (
            "#!/usr/bin/env bash\n"
            f'echo "$@" >> {self.log}\n'
            'case "$1" in\n'
            f'  print) printf "  state = {print_state}\\n  pid = 4242\\n'
            f'  last exit code = 0\\n";;\n'
            "  bootstrap|bootout|kickstart) exit 0;;\n"
            "  *) exit 0;;\n"
            "esac\n"
        )
        stub.write_text(body)
        stub.chmod(0o755)

    def env(self) -> dict[str, str]:
        return {"PATH": f"{self.bin}:{os.environ['PATH']}"}


def _calls(tmp_path: Path) -> list[str]:
    log = tmp_path / "launchctl-calls.log"
    return log.read_text().splitlines() if log.exists() else []


def _service_env(tmp_path: Path, extra: dict[str, str] | None = None) -> dict[str, str]:
    """Hermetic service.sh environment: plists/logs into tmp, PATH stubbed."""
    env = _LaunchctlStub(tmp_path).env()
    env.update(
        {
            "PLIST_DIR": str(tmp_path / "LaunchAgents"),
            "LOG_DIR": str(tmp_path / "Logs"),
        }
    )
    if extra:
        env.update(extra)
    return env


class TestServiceInstall:
    @need_venv
    def test_install_writes_plists_and_bootstraps_both_labels(self, tmp_path):
        env = _service_env(tmp_path)
        result = _run([str(SCRIPTS / "service.sh"), "install"], env=env)
        assert result.returncode == 0, result.stdout + result.stderr
        plist_dir = tmp_path / "LaunchAgents"
        api = plist_dir / "com.secondbrain.api.plist"
        tg = plist_dir / "com.secondbrain.telegram.plist"
        assert api.exists() and tg.exists(), result.stdout
        api_text = api.read_text()
        assert "<string>com.secondbrain.api</string>" in api_text
        assert str(SCRIPTS / "run-server.sh") in api_text
        assert str(SCRIPTS / "run-bot.sh") in tg.read_text()
        # Secrets stay out of the plist: no token name, let alone value.
        assert "TELEGRAM_BOT_TOKEN" not in api_text
        for plist_text in (api_text, tg.read_text()):
            assert "<string>second-brain</string>" in plist_text  # COMPOSE_PROJECT_NAME
            assert "<key>SuccessfulExit</key>" in plist_text
            assert "<false/>" in plist_text  # KeepAlive: exit 0 => no restart
            assert str(tmp_path / "Logs") in plist_text
            assert "<key>WorkingDirectory</key>" in plist_text
        calls = _calls(tmp_path)
        assert any("bootstrap" in c and "com.secondbrain.api" in c for c in calls)
        assert any("bootstrap" in c and "com.secondbrain.telegram" in c for c in calls)

    @need_venv
    @need_venv
    def test_install_precreates_log_files_0600(self, tmp_path):
        """The four log files are pre-created mode 600 BEFORE bootstrap: launchd
        writes into the existing inode (preserving the mode), so service logs
        are never world-readable."""
        env = _service_env(tmp_path)
        result = _run([str(SCRIPTS / "service.sh"), "install"], env=env)
        assert result.returncode == 0, result.stdout + result.stderr
        log_dir = tmp_path / "Logs"
        for name in ("api.log", "api.err.log", "telegram.log", "telegram.err.log"):
            log = log_dir / name
            assert log.exists(), f"{name} not pre-created"
            assert log.stat().st_mode & 0o777 == 0o600, f"{name} not 0600"

    def test_install_takes_over_manual_bot_without_duplicates(self, tmp_path):
        """A running nohup bot (pid file + our cwd) is DEAD after install —
        launchd's bot must be the only getUpdates poller (else: 409)."""
        import time

        dev = REPO_ROOT / ".dev"
        dev.mkdir(exist_ok=True)
        pid_file = dev / "telegram.pid"
        bot = subprocess.Popen(
            ["bash", "-c", "exec -a 'brain telegram' sleep 30"],
            cwd=REPO_ROOT,  # ownership proof: cwd == repo
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        time.sleep(0.3)
        pid_file.write_text(str(bot.pid))
        try:
            env = _service_env(tmp_path)
            result = _run([str(SCRIPTS / "service.sh"), "install"], env=env)
            assert result.returncode == 0, result.stdout + result.stderr
            assert "takeover: stopping manual bot" in result.stdout
            # poll() reaps: not None => the bot process is gone (no zombie,
            # no duplicate poller, no 409).
            assert bot.poll() is not None, (
                f"manual bot survived install — duplicate poller, 409 ahead\n"
                f"--- stdout ---\n{result.stdout}\n--- stderr ---\n{result.stderr}"
            )
            assert not pid_file.exists()
        finally:
            bot.kill()
            bot.wait()
            pid_file.unlink(missing_ok=True)

    @need_venv
    def test_install_reports_stale_bot_pid_file_without_killing_foreign_pid(self, tmp_path):
        """A pid file pointing at a NON-bot process is ignored, not killed."""
        dev = REPO_ROOT / ".dev"
        dev.mkdir(exist_ok=True)
        pid_file = dev / "telegram.pid"
        sleeper = subprocess.Popen(["sleep", "30"])
        pid_file.write_text(str(sleeper.pid))
        try:
            env = _service_env(tmp_path)
            result = _run([str(SCRIPTS / "service.sh"), "install"], env=env)
            assert result.returncode == 0, result.stdout + result.stderr
            assert "bot pid file stale" in result.stdout
            assert sleeper.poll() is None  # untouched: still running
        finally:
            sleeper.kill()
            sleeper.wait()
            pid_file.unlink(missing_ok=True)


class TestServiceOtherCommands:
    def test_uninstall_boots_out_and_removes_plists(self, tmp_path):
        env = _service_env(tmp_path)
        _run([str(SCRIPTS / "service.sh"), "install"], env=env)
        result = _run([str(SCRIPTS / "service.sh"), "uninstall"], env=env)
        assert result.returncode == 0, result.stdout + result.stderr
        assert not (tmp_path / "LaunchAgents" / "com.secondbrain.api.plist").exists()
        assert not (tmp_path / "LaunchAgents" / "com.secondbrain.telegram.plist").exists()
        calls = _calls(tmp_path)
        assert sum("bootout" in c for c in calls) >= 2  # install pre-cleans, uninstall removes

    def test_status_exit_zero_when_all_running(self, tmp_path):
        result = _run([str(SCRIPTS / "service.sh"), "status"], env=_service_env(tmp_path))
        assert result.returncode == 0, result.stdout + result.stderr
        assert "state=running" in result.stdout

    def test_status_exit_nonzero_when_not_installed(self, tmp_path):
        stub = _LaunchctlStub(tmp_path)  # env var PLIST_DIR/LOG_DIR still set below
        stub.bin.joinpath("launchctl").write_text(
            "#!/usr/bin/env bash\n"
            f'echo "$@" >> {stub.log}\n'
            'echo "Could not find service" >&2\nexit 3\n'
        )
        stub.bin.joinpath("launchctl").chmod(0o755)
        env = stub.env()
        env["PLIST_DIR"] = str(tmp_path / "LaunchAgents")
        env["LOG_DIR"] = str(tmp_path / "Logs")
        result = _run([str(SCRIPTS / "service.sh"), "status"], env=env)
        assert result.returncode != 0
        assert "not installed" in result.stdout

    def test_restart_kickstarts_both_labels(self, tmp_path):
        result = _run([str(SCRIPTS / "service.sh"), "restart"], env=_service_env(tmp_path))
        assert result.returncode == 0, result.stdout + result.stderr
        calls = _calls(tmp_path)
        assert any("kickstart" in c and "-k" in c and "com.secondbrain.api" in c for c in calls)
        assert any(
            "kickstart" in c and "-k" in c and "com.secondbrain.telegram" in c for c in calls
        )

    def test_logs_tails_both_components(self, tmp_path):
        env = _service_env(tmp_path)
        log_dir = tmp_path / "Logs"
        log_dir.mkdir()
        (log_dir / "api.log").write_text("api-start-line\n")
        (log_dir / "telegram.err.log").write_text("telegram-error-line\n")
        result = _run([str(SCRIPTS / "service.sh"), "logs"], env=env)
        assert result.returncode == 0, result.stderr
        assert "api-start-line" in result.stdout
        assert "telegram-error-line" in result.stdout

    def test_unknown_command_fails(self, tmp_path):
        result = _run([str(SCRIPTS / "service.sh"), "bogus"], env=_service_env(tmp_path))
        assert result.returncode != 0
        assert "unknown command" in result.stderr


class TestEnvironmentChecks:
    def test_sleep_ok_when_sleepdisabled(self, tmp_path):
        stub = _LaunchctlStub(tmp_path)
        pmset = stub.bin / "pmset"
        pmset.write_text('#!/usr/bin/env bash\nprintf " SleepDisabled\\t\\t1\\n"\n')
        pmset.chmod(0o755)
        result = _run([str(SCRIPTS / "service.sh"), "status"], env=_service_env(tmp_path))
        assert "sleep: OK" in result.stdout, result.stdout

    def test_sleep_ok_when_sleep_timer_zero(self, tmp_path):
        stub = _LaunchctlStub(tmp_path)
        pmset = stub.bin / "pmset"
        pmset.write_text(
            '#!/usr/bin/env bash\nprintf " sleep\\t\\t0 (sleep prevented by caffeinate)\\n"\n'
        )
        pmset.chmod(0o755)
        result = _run([str(SCRIPTS / "service.sh"), "status"], env=_service_env(tmp_path))
        assert "sleep: OK" in result.stdout

    def test_sleep_warns_when_mac_can_sleep(self, tmp_path):
        stub = _LaunchctlStub(tmp_path)
        pmset = stub.bin / "pmset"
        pmset.write_text(
            '#!/usr/bin/env bash\nprintf " SleepDisabled\\t\\t0\\n sleep\\t\\t45\\n"\n'
        )
        pmset.chmod(0o755)
        result = _run([str(SCRIPTS / "service.sh"), "status"], env=_service_env(tmp_path))
        assert "sleep: WARNING" in result.stderr

    @need_venv
    def test_docker_unreachable_warns_not_dies(self, tmp_path):
        """Docker down at install is a warning (containers come back when the
        daemon starts at login); install must still complete."""
        stub = _LaunchctlStub(tmp_path)
        docker = stub.bin / "docker"
        docker.write_text("#!/usr/bin/env bash\nexit 1\n")  # docker info fails
        docker.chmod(0o755)
        result = _run([str(SCRIPTS / "service.sh"), "install"], env=_service_env(tmp_path))
        assert result.returncode == 0, result.stdout + result.stderr
        assert "docker: NOT reachable" in result.stderr
