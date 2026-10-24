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
            [
                "python3",
                "-c",
                "import time; time.sleep(30)",
                "uvicorn brain.main:app",
                "--port",
                "27800",
            ],
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
        code = f'cd {alias} && source scripts/lib.sh && printf "%s" "$LIB_ROOT"'
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
    """PATH shim for launchctl: records invocations, fakes `print` output.

    bootout marks the label gone (a later `print` for it fails, like the real
    launchctl after a bootout); bootstrap can be told to fail N times with the
    classic '5: Input/output error' before succeeding (the install race)."""

    def __init__(
        self,
        tmp_path: Path,
        print_state: str = "running",
        bootstrap_failures: int = 0,
    ) -> None:
        self.bin = tmp_path / "stubbin"
        self.bin.mkdir(exist_ok=True)
        self.state = tmp_path / "stubstate"
        self.state.mkdir(exist_ok=True)
        self.log = tmp_path / "launchctl-calls.log"
        stub = self.bin / "launchctl"
        body = (
            "#!/usr/bin/env bash\n"
            f'echo "$@" >> {self.log}\n'
            f"STATE={self.state}\n"
            'case "$1" in\n'
            "  print)\n"
            '    label="${2##*/}"\n'
            '    [ -f "$STATE/gone-$label" ] && exit 3\n'
            f'    printf "  state = {print_state}\\n  pid = 4242\\n'
            f'  last exit code = 0\\n";;\n'
            "  bootout)\n"
            '    label="${2##*/}"\n'
            '    touch "$STATE/gone-$label"\n'
            "    exit 0;;\n"
            "  bootstrap)\n"
            '    case "$2" in\n'
            '      gui/[0-9]*) ;;  # launchctl bootstrap takes a DOMAIN target only\n'
            '      *) echo "bootstrap: $2 is not a domain target (gui/<uid>)" >&2; exit 1;;\n'
            '    esac\n'
            '    label="${3##*/}"\n'
            '    n=$(cat "$STATE/bfail-$label" 2>/dev/null || echo 0)\n'
            f'    if [ "$n" -lt {bootstrap_failures} ]; then\n'
            '      echo $((n + 1)) > "$STATE/bfail-$label"\n'
            '      echo "Bootstrap failed: 5: Input/output error" >&2\n'
            "      exit 5\n"
            "    fi\n"
            '    rm -f "$STATE/gone-$label"\n'
            "    exit 0;;\n"
            "  kickstart) exit 0;;\n"
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
        env = _service_env(tmp_path)
        plist_dir = tmp_path / "LaunchAgents"
        plist_dir.mkdir()
        for label in ("com.secondbrain.api", "com.secondbrain.telegram"):
            (plist_dir / f"{label}.plist").write_text("<plist/>\n")
        result = _run([str(SCRIPTS / "service.sh"), "restart"], env=env)
        assert result.returncode == 0, result.stdout + result.stderr
        calls = _calls(tmp_path)
        assert any("kickstart" in c and "-k" in c and "com.secondbrain.api" in c for c in calls)
        assert any(
            "kickstart" in c and "-k" in c and "com.secondbrain.telegram" in c for c in calls
        )

    def test_restart_skips_labels_without_plists(self, tmp_path):
        """'restart' must work BEFORE 'install': labels with no plist are
        skipped with a note instead of failing the whole command."""
        env = _service_env(tmp_path)
        plist_dir = tmp_path / "LaunchAgents"
        plist_dir.mkdir()
        (plist_dir / "com.secondbrain.api.plist").write_text("<plist/>\n")
        # telegram + whatsapp deliberately not installed
        result = _run([str(SCRIPTS / "service.sh"), "restart"], env=env)
        assert result.returncode == 0, result.stdout + result.stderr
        calls = _calls(tmp_path)
        assert any("kickstart" in c and "com.secondbrain.api" in c for c in calls)
        assert not any("com.secondbrain.telegram" in c for c in calls)
        assert not any("com.secondbrain.whatsapp" in c for c in calls)
        assert "skipped" in result.stdout
        assert "com.secondbrain.whatsapp" in result.stdout

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

    def test_bootstrap_target_is_the_domain_not_the_service(self, tmp_path):
        """launchctl bootstrap takes a DOMAIN (gui/<uid>) plus the plist path —
        NOT a service target (gui/<uid>/<label>, which is for bootout/print/
        kickstart). A service target here makes every bootstrap fail, i.e. the
        next install would boot out all agents and start none. The stub is
        strict about the argv shape so this stays caught."""
        env = _service_env(tmp_path)
        result = _run([str(SCRIPTS / "service.sh"), "install"], env=env)
        assert result.returncode == 0, result.stdout + result.stderr
        for call in _calls(tmp_path):
            if "bootstrap" not in call:
                continue
            target = call.split()[1]
            assert target.count("/") == 1 and target.startswith("gui/"), call

    def test_install_retries_bootstrap_after_the_launchd_race(self, tmp_path):
        """'Bootstrap failed: 5: Input/output error' is the classic bootout→
        bootstrap race; install must retry (bounded) instead of dying with the
        agents bootout'ed and nothing running (seen live 2026-10-23)."""
        stub = _LaunchctlStub(tmp_path, bootstrap_failures=1)
        env = stub.env()
        env.update(
            {
                "PLIST_DIR": str(tmp_path / "LaunchAgents"),
                "LOG_DIR": str(tmp_path / "Logs"),
            }
        )
        result = _run([str(SCRIPTS / "service.sh"), "install"], env=env)
        assert result.returncode == 0, result.stdout + result.stderr
        bootstraps = [c for c in _calls(tmp_path) if "bootstrap" in c]
        # EVERY label hit the race once and was retried (the stub counts per label).
        for label in (
            "com.secondbrain.api",
            "com.secondbrain.telegram",
            "com.secondbrain.whatsapp",
        ):
            assert len([c for c in bootstraps if label in c]) == 2, label

    def test_install_fails_loudly_when_bootstrap_never_succeeds(self, tmp_path):
        stub = _LaunchctlStub(tmp_path, bootstrap_failures=9)
        env = stub.env()
        env.update(
            {
                "PLIST_DIR": str(tmp_path / "LaunchAgents"),
                "LOG_DIR": str(tmp_path / "Logs"),
            }
        )
        result = _run([str(SCRIPTS / "service.sh"), "install"], env=env)
        assert result.returncode != 0
        assert "bootstrap" in (result.stdout + result.stderr).lower()
        # One label's failure must not abandon the others: 3 attempts each
        # (retry bound), then install reports failure.
        bootstraps = [c for c in _calls(tmp_path) if "bootstrap" in c]
        for label in (
            "com.secondbrain.api",
            "com.secondbrain.telegram",
            "com.secondbrain.whatsapp",
        ):
            assert len([c for c in bootstraps if label in c]) == 3, label

    def test_install_wait_for_bootout_is_bounded(self, tmp_path):
        """If launchd still reports the label after bootout, install must not
        hang: the wait is bounded (BOOTOUT_WAIT_TRIES) and bootstrap's retry
        covers the rest of the race."""
        stub = _LaunchctlStub(tmp_path, print_state="running")
        env = stub.env()
        env.update(
            {
                "PLIST_DIR": str(tmp_path / "LaunchAgents"),
                "LOG_DIR": str(tmp_path / "Logs"),
                "BOOTOUT_WAIT_TRIES": "1",
            }
        )
        result = _run([str(SCRIPTS / "service.sh"), "install"], env=env)
        assert result.returncode == 0, result.stdout + result.stderr
        bootstraps = [c for c in _calls(tmp_path) if "bootstrap" in c]
        # Email is opt-in and unset here: api, telegram, whatsapp bootstrap.
        assert len(bootstraps) == 3

    def test_side_process_scripts_log_unbuffered(self):
        """launchd log files must stream: PYTHONUNBUFFERED=1 everywhere
        (run-server.sh already had it; the poller/bot logs buffered in 4kB
        blocks, which read as 'empty logs' while the agents were fine)."""
        for script in ("run-server.sh", "run-bot.sh", "run-email.sh", "run-whatsapp.sh"):
            text = (SCRIPTS / script).read_text()
            assert "export PYTHONUNBUFFERED=1" in text, script


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


class TestRunEmailExitContract:
    """run-email.sh maps `brain gmail-poll` exit codes onto the KeepAlive
    contract: 0 (clean) and 78 (EX_CONFIG: no label id / no keychain token)
    become 0 (launchd: no restart); transient failures propagate (restart).
    needs-re-auth/label-missing are NOT exits — the poller pauses internally;
    they surface as 78 only when the agent cannot start at all."""

    def _run_wrapper(self, tmp_path, poller_rc):
        fake_bin = tmp_path / "bin-email"
        fake_bin.mkdir(exist_ok=True)
        fake_brain = fake_bin / "brain"
        fake_brain.write_text(
            f"#!/usr/bin/env bash\necho fake-gmail-poll rc={poller_rc}\nexit {poller_rc}\n"
        )
        fake_brain.chmod(0o755)
        return _run([str(SCRIPTS / "run-email.sh")], env={"BRAIN_BIN": str(fake_brain)})

    @pytest.mark.parametrize("poller_rc,expected", [(0, 0), (78, 0), (1, 1), (70, 70)])
    def test_exit_code_mapping(self, tmp_path, poller_rc, expected):
        result = self._run_wrapper(tmp_path, poller_rc)
        assert result.returncode == expected, result.stdout + result.stderr

    def test_wrapper_preserves_the_real_code_in_its_log_line(self, tmp_path):
        result = self._run_wrapper(tmp_path, 78)
        assert "exited 78" in result.stdout

    def test_missing_venv_exits_0_no_restart_loop(self, tmp_path):
        """A missing venv is permanent: exit 0, never restart-loop."""
        result = _run(
            [str(SCRIPTS / "run-email.sh")],
            env={"BRAIN_BIN": str(tmp_path / "no-such-brain")},
        )
        assert result.returncode == 0
        assert "missing" in result.stderr

    def test_exports_allow_live_and_compose_project(self, tmp_path):
        """The launchd email path is the real service: BRAIN_ALLOW_LIVE=1 (the
        live-store guard would refuse otherwise) and COMPOSE_PROJECT_NAME=second-brain."""
        fake_bin = tmp_path / "bin-email2"
        fake_bin.mkdir()
        fake_brain = fake_bin / "brain"
        fake_brain.write_text(
            'echo "ALLOW: ${BRAIN_ALLOW_LIVE-unset} COMPOSE: ${COMPOSE_PROJECT_NAME-unset}"\n'
        )
        fake_brain.chmod(0o755)
        result = _run([str(SCRIPTS / "run-email.sh")], env={"BRAIN_BIN": str(fake_brain)})
        assert result.returncode == 0, result.stderr
        assert "ALLOW: 1" in result.stdout
        assert "COMPOSE: second-brain" in result.stdout

    def test_sources_lib_and_loads_the_env_file(self, tmp_path):
        """run-email.sh must source lib.sh BEFORE calling load_env_file: with
        the source missing, every start printed 'load_env_file: command not
        found' and .env was silently skipped (review of #43 @ 5564e09).
        BRAIN_ENV_FILE is the test seam: the env file the launcher loads
        (production default: $ROOT/.env)."""
        env_file = tmp_path / "email.env"
        env_file.write_text("GMAIL_LABEL_ID=Label_from_env_file\n")
        fake_bin = tmp_path / "bin-email3"
        fake_bin.mkdir()
        fake_brain = fake_bin / "brain"
        fake_brain.write_text('echo "LABEL: ${GMAIL_LABEL_ID-unset}"\n')
        fake_brain.chmod(0o755)
        result = _run(
            [str(SCRIPTS / "run-email.sh")],
            env={
                "BRAIN_BIN": str(fake_brain),
                "BRAIN_ENV_FILE": str(env_file),
                "GMAIL_LABEL_ID": "",  # neutralize any inherited value
            },
        )
        assert result.returncode == 0, result.stdout + result.stderr
        assert "command not found" not in result.stderr, result.stderr
        assert "LABEL: Label_from_env_file" in result.stdout, result.stdout + result.stderr


class TestServiceEmailAgent:
    """service.sh manages FOUR agents: api, telegram, email, whatsapp —
    but email is OPT-IN: install writes/bootstraps it only when
    GMAIL_LABEL_ID is set (env or .env); otherwise the agent is skipped
    and no stale plist is left behind."""

    @need_venv
    def test_install_writes_and_bootstraps_four_labels(self, tmp_path):
        env = _service_env(tmp_path, extra={"GMAIL_LABEL_ID": "Label_test"})
        result = _run([str(SCRIPTS / "service.sh"), "install"], env=env)
        assert result.returncode == 0, result.stdout + result.stderr
        email_plist = tmp_path / "LaunchAgents" / "com.secondbrain.email.plist"
        assert email_plist.exists(), result.stdout
        text = email_plist.read_text()
        assert "<string>com.secondbrain.email</string>" in text
        assert str(SCRIPTS / "run-email.sh") in text
        # Secrets stay out: the keychain token NEVER appears in the plist.
        assert "GMAIL" not in text and "TOKEN" not in text
        calls = _calls(tmp_path)
        assert any("bootstrap" in c and "com.secondbrain.email" in c for c in calls)

    @need_venv
    def test_install_precreates_email_log_files_0600(self, tmp_path):
        env = _service_env(tmp_path, extra={"GMAIL_LABEL_ID": "Label_test"})
        result = _run([str(SCRIPTS / "service.sh"), "install"], env=env)
        assert result.returncode == 0, result.stdout + result.stderr
        log_dir = tmp_path / "Logs"
        for name in ("email.log", "email.err.log"):
            log = log_dir / name
            assert log.exists(), f"{name} not pre-created"
            assert log.stat().st_mode & 0o777 == 0o600, f"{name} not 0600"

    def test_uninstall_removes_the_email_plist(self, tmp_path):
        env = _service_env(tmp_path, extra={"GMAIL_LABEL_ID": "Label_test"})
        _run([str(SCRIPTS / "service.sh"), "install"], env=env)
        result = _run([str(SCRIPTS / "service.sh"), "uninstall"], env=env)
        assert result.returncode == 0, result.stdout + result.stderr
        assert not (tmp_path / "LaunchAgents" / "com.secondbrain.email.plist").exists()

    def test_install_skips_email_when_gmail_label_id_unset(self, tmp_path):
        """Email is OPT-IN: without GMAIL_LABEL_ID (env or .env) install must
        NOT write or bootstrap the email agent, must remove any stale email
        plist (a leftover agent would start, exit 78->0 and read as a
        permanently-down background service), and must say so (review of
        #43 @ 5564e09)."""
        env = _service_env(tmp_path, extra={"GMAIL_LABEL_ID": ""})
        plist_dir = tmp_path / "LaunchAgents"
        plist_dir.mkdir()
        (plist_dir / "com.secondbrain.email.plist").write_text("<stale/>\n")
        result = _run([str(SCRIPTS / "service.sh"), "install"], env=env)
        assert result.returncode == 0, result.stdout + result.stderr
        assert not (plist_dir / "com.secondbrain.email.plist").exists(), result.stdout
        assert "skipped" in result.stdout and "GMAIL_LABEL_ID" in result.stdout
        calls = _calls(tmp_path)
        assert not any("bootstrap" in c and "com.secondbrain.email" in c for c in calls)

    def test_install_installs_email_when_gmail_label_id_set(self, tmp_path):
        env = _service_env(tmp_path, extra={"GMAIL_LABEL_ID": "Label_test"})
        result = _run([str(SCRIPTS / "service.sh"), "install"], env=env)
        assert result.returncode == 0, result.stdout + result.stderr
        plist_dir = tmp_path / "LaunchAgents"
        assert (plist_dir / "com.secondbrain.email.plist").exists()
        assert any("bootstrap" in c and "com.secondbrain.email" in c for c in _calls(tmp_path))
        assert "GMAIL_LABEL_ID not set" not in result.stdout

    def test_status_skips_email_when_not_configured_without_failing(self, tmp_path):
        """'status' must not report the opt-out email agent as down: without
        GMAIL_LABEL_ID the email line says skipped and api/tg/whatsapp alone
        decide the exit code."""
        env = _service_env(tmp_path, extra={"GMAIL_LABEL_ID": ""})
        result = _run([str(SCRIPTS / "service.sh"), "status"], env=env)
        assert result.returncode == 0, result.stdout + result.stderr
        assert "GMAIL_LABEL_ID not set" in result.stdout

    def test_restart_kickstarts_the_email_agent(self, tmp_path):
        env = _service_env(tmp_path)
        plist_dir = tmp_path / "LaunchAgents"
        plist_dir.mkdir()
        (plist_dir / "com.secondbrain.email.plist").write_text("<plist/>\n")
        result = _run([str(SCRIPTS / "service.sh"), "restart"], env=env)
        assert result.returncode == 0, result.stdout + result.stderr
        assert any("kickstart" in c and "com.secondbrain.email" in c for c in _calls(tmp_path))

    def test_status_covers_the_email_agent(self, tmp_path):
        result = _run([str(SCRIPTS / "service.sh"), "status"], env=_service_env(tmp_path))
        assert result.returncode == 0, result.stdout + result.stderr
        assert "com.secondbrain.email" in result.stdout

    def test_logs_tails_the_email_component(self, tmp_path):
        env = _service_env(tmp_path)
        log_dir = tmp_path / "Logs"
        log_dir.mkdir()
        (log_dir / "email.log").write_text("email-poll-line\n")
        result = _run([str(SCRIPTS / "service.sh"), "logs", "email"], env=env)
        assert result.returncode == 0, result.stderr
        assert "email-poll-line" in result.stdout



class TestConnectorsAgentGating:
    """service.sh manages the connectors agent (com.secondbrain.connectors)
    OPT-IN like email: install writes/bootstraps it only when BRAIN_CONNECTORS
    contains a non-email connector (email keeps its own agent); otherwise the
    agent is skipped and no stale plist is left behind."""

    CONNECTORS_PLIST = "com.secondbrain.connectors.plist"

    @need_venv
    def test_install_writes_and_bootstraps_connectors_when_non_email_set(self, tmp_path):
        env = _service_env(tmp_path, extra={"BRAIN_CONNECTORS": "gcal,gtasks"})
        result = _run([str(SCRIPTS / "service.sh"), "install"], env=env)
        assert result.returncode == 0, result.stdout + result.stderr
        plist = tmp_path / "LaunchAgents" / self.CONNECTORS_PLIST
        assert plist.exists(), result.stdout
        text = plist.read_text()
        assert "<string>com.secondbrain.connectors</string>" in text
        assert str(SCRIPTS / "run-connectors.sh") in text
        # Config stays out of the plist: run-connectors.sh reads
        # BRAIN_CONNECTORS from the environment/.env at runtime.
        assert "gcal" not in text and "gtasks" not in text
        calls = _calls(tmp_path)
        assert any("bootstrap" in c and "com.secondbrain.connectors" in c for c in calls)

    @need_venv
    def test_install_precreates_connectors_log_files_0600(self, tmp_path):
        env = _service_env(tmp_path, extra={"BRAIN_CONNECTORS": "gcal"})
        result = _run([str(SCRIPTS / "service.sh"), "install"], env=env)
        assert result.returncode == 0, result.stdout + result.stderr
        log_dir = tmp_path / "Logs"
        for name in ("connectors.log", "connectors.err.log"):
            log = log_dir / name
            assert log.exists(), f"{name} not pre-created"
            assert log.stat().st_mode & 0o777 == 0o600, f"{name} not 0600"

    def test_install_skips_connectors_without_a_non_email_connector(self, tmp_path):
        """Without a non-email connector (unset / empty / only 'email') install
        must NOT write or bootstrap the agent, and must remove any stale plist
        (a leftover agent would loop forever doing nothing)."""
        plist_dir = tmp_path / "LaunchAgents"
        plist_dir.mkdir()
        stale = plist_dir / self.CONNECTORS_PLIST
        stale.write_text("<plist/>\n")
        for connectors in (None, "", "email"):
            extra = {} if connectors is None else {"BRAIN_CONNECTORS": connectors}
            env = _service_env(tmp_path, extra=extra)
            result = _run([str(SCRIPTS / "service.sh"), "install"], env=env)
            assert result.returncode == 0, result.stdout + result.stderr
            assert not plist_dir.joinpath(self.CONNECTORS_PLIST).exists(), result.stdout
            assert "connectors: skipped" in result.stdout
            assert any(
                "bootout" in c and "com.secondbrain.connectors" in c for c in _calls(tmp_path)
            )

    @need_venv
    def test_install_skips_connectors_when_only_email_set(self, tmp_path):
        env = _service_env(tmp_path, extra={"BRAIN_CONNECTORS": "email"})
        result = _run([str(SCRIPTS / "service.sh"), "install"], env=env)
        assert result.returncode == 0, result.stdout + result.stderr
        assert not (tmp_path / "LaunchAgents" / self.CONNECTORS_PLIST).exists()

    def test_uninstall_removes_the_connectors_plist(self, tmp_path):
        env = _service_env(tmp_path, extra={"BRAIN_CONNECTORS": "gcal"})
        _run([str(SCRIPTS / "service.sh"), "install"], env=env)
        result = _run([str(SCRIPTS / "service.sh"), "uninstall"], env=env)
        assert result.returncode == 0, result.stdout + result.stderr
        assert not (tmp_path / "LaunchAgents" / self.CONNECTORS_PLIST).exists()

    def test_restart_kickstarts_the_connectors_label(self, tmp_path):
        env = _service_env(tmp_path)
        plist_dir = tmp_path / "LaunchAgents"
        plist_dir.mkdir()
        (plist_dir / self.CONNECTORS_PLIST).write_text("<plist/>\n")
        result = _run([str(SCRIPTS / "service.sh"), "restart"], env=env)
        assert result.returncode == 0, result.stdout + result.stderr
        assert any(
            "kickstart" in c and "com.secondbrain.connectors" in c for c in _calls(tmp_path)
        )

    def test_status_reports_connectors_skipped_when_not_configured(self, tmp_path):
        env = _service_env(tmp_path)
        result = _run([str(SCRIPTS / "service.sh"), "status"], env=env)
        assert "com.secondbrain.connectors" in result.stdout


class TestRunConnectorsScript:
    """run-connectors.sh: one sync cycle per non-email connector per interval,
    the run-email.sh KeepAlive contract at the pass level (all-permanent -> 0
    no restart; any transient -> nonzero restart), per-connector isolation
    (one failing source never blocks the others), email NEVER synced here."""

    @staticmethod
    def _stub_brain(tmp_path, rc_by_name: dict[str, int], extra_line: str = "") -> str:
        """A fake `brain` that logs invocations and exits per connector name."""
        fake_bin = tmp_path / "bin-connectors"
        fake_bin.mkdir(exist_ok=True)
        cases = "".join(
            f'  "{name}") exit {rc} ;;\n' for name, rc in rc_by_name.items()
        )
        stub = fake_bin / "brain"
        stub.write_text(
            "#!/usr/bin/env bash\n"
            'printf "%s %s %s ALLOW=%s\\n" "$1" "${2-}" "${3-}" "${BRAIN_ALLOW_LIVE-unset}"'
            ' >> "$CONNECTORS_CALL_LOG"\n'
            "case \"${3-}\" in\n"
            f"{cases}"
            "  *) exit 0 ;;\n"
            "esac\n"
            + (extra_line or "")
        )
        stub.chmod(0o755)
        return str(stub)

    @staticmethod
    def _run_script(fake_bin: str, tmp_path, extra: dict[str, str] | None = None):
        log = tmp_path / "calls.log"
        env = {
            "BRAIN_BIN": fake_bin,
            "CONNECTORS_CALL_LOG": str(log),
            "BRAIN_CONNECTORS": "gcal,gtasks",
            "BRAIN_CONNECTORS_PASSES": "1",
        }
        if extra:
            env.update(extra)
        return _run([str(SCRIPTS / "run-connectors.sh")], env=env), log

    def test_passes_bash_syntax_check(self):
        result = _run(["bash", "-n", str(SCRIPTS / "run-connectors.sh")])
        assert result.returncode == 0, result.stderr

    def test_passes_shellcheck_when_installed(self):
        shellcheck = _shellcheck_binary()
        if shellcheck is None:
            pytest.skip("shellcheck not installed (pip install shellcheck-py)")
        result = _run([shellcheck, str(SCRIPTS / "run-connectors.sh")])
        assert result.returncode == 0, result.stdout + result.stderr

    def test_all_permanent_78_exits_0_no_restart(self, tmp_path):
        """Every connector says 78 (needs user consent): permanent — exit 0,
        launchd keeps the agent down (the run-email EX_CONFIG contract)."""
        fake_bin = self._stub_brain(tmp_path, {"gcal": 78, "gtasks": 78})
        result, _ = self._run_script(fake_bin, tmp_path)
        assert result.returncode == 0, result.stdout + result.stderr
        assert "78" in result.stdout

    def test_transient_failure_exits_nonzero_for_restart(self, tmp_path):
        fake_bin = self._stub_brain(tmp_path, {"gcal": 1, "gtasks": 0})
        result, _ = self._run_script(fake_bin, tmp_path)
        assert result.returncode == 1, result.stdout + result.stderr

    def test_one_failing_connector_does_not_block_the_other(self, tmp_path):
        """Per-connector isolation: gcal fails (transient), gtasks STILL synced
        in the same pass — then the pass-level mapping applies (exit 1)."""
        fake_bin = self._stub_brain(tmp_path, {"gcal": 1, "gtasks": 0})
        result, log = self._run_script(fake_bin, tmp_path)
        calls = log.read_text()
        assert "connectors sync gcal" in calls
        assert "connectors sync gtasks" in calls
        assert result.returncode == 1

    def test_email_is_never_synced_by_this_agent(self, tmp_path):
        """email keeps its own agent (run-email.sh); the connectors agent must
        never run a sync cycle for it."""
        fake_bin = self._stub_brain(tmp_path, {"email": 0, "gcal": 0})
        result, log = self._run_script(
            fake_bin, tmp_path, extra={"BRAIN_CONNECTORS": "email,gcal"}
        )
        calls = log.read_text()
        assert "sync email" not in calls
        assert "sync gcal" in calls
        assert result.returncode == 0

    def test_empty_or_email_only_list_exits_0_without_invoking_brain(self, tmp_path):
        for connectors in ("", "email"):
            fake_bin = self._stub_brain(tmp_path, {"gcal": 0})
            result, log = self._run_script(
                fake_bin, tmp_path, extra={"BRAIN_CONNECTORS": connectors}
            )
            assert result.returncode == 0, result.stdout + result.stderr
            assert not log.exists() or log.read_text() == ""

    def test_exports_allow_live_and_compose_project(self, tmp_path):
        """The launchd path IS the real service: BRAIN_ALLOW_LIVE=1 (the
        live-store guard would refuse otherwise) + COMPOSE_PROJECT_NAME."""
        fake_bin = self._stub_brain(tmp_path, {"gcal": 0})
        result, log = self._run_script(fake_bin, tmp_path)
        assert "ALLOW=1" in log.read_text()
        assert result.returncode == 0

    def test_sources_lib_and_loads_the_env_file(self, tmp_path):
        """lib.sh must be sourced BEFORE load_env_file (the #43 review trap);
        BRAIN_ENV_FILE is the test seam. The connector list comes from the
        env file when the real environment does not set it."""
        env_file = tmp_path / "connectors.env"
        env_file.write_text("BRAIN_CONNECTORS=gcal\n")
        fake_bin = self._stub_brain(tmp_path, {"gcal": 0})
        result, log = self._run_script(
            fake_bin,
            tmp_path,
            extra={
                "BRAIN_ENV_FILE": str(env_file),
                "BRAIN_CONNECTORS": "",  # neutralize any inherited value
            },
        )
        assert result.returncode == 0, result.stdout + result.stderr
        assert "command not found" not in result.stderr, result.stderr
        assert "sync gcal" in log.read_text()

    def test_loops_until_killed(self, tmp_path):
        """All-ok passes sleep BRAIN_CONNECTORS_INTERVAL_S and rerun: the stub
        log grows past one pass. (The script is killed by the test — launchd
        would keep it alive.)"""
        fake_bin = self._stub_brain(tmp_path, {"gcal": 0})
        log = tmp_path / "calls-loop.log"
        import signal
        import time

        env = {
            "BRAIN_BIN": fake_bin,
            "CONNECTORS_CALL_LOG": str(log),
            "BRAIN_CONNECTORS": "gcal",
            "BRAIN_CONNECTORS_INTERVAL_S": "1",
            # the REAL forever-loop: the passes seam deliberately NOT set —
            # this test proves the production loop actually loops.
        }
        env = {k: v for k, v in {**os.environ, **env}.items() if k != "BRAIN_CONNECTORS_PASSES"}
        proc = subprocess.Popen(
            [str(SCRIPTS / "run-connectors.sh")],
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            deadline = time.time() + 5
            while time.time() < deadline:
                if log.exists() and len(log.read_text().splitlines()) >= 2:
                    break
                time.sleep(0.1)
            assert log.exists() and len(log.read_text().splitlines()) >= 2, (
                "the agent did not run a second pass"
            )
        finally:
            proc.send_signal(signal.SIGTERM)
            proc.wait(timeout=10)

