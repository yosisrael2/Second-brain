"""scripts/auto-deploy.sh against real git repos and stubbed host tools.

A bare "origin" and a "server" clone stand in for GitHub and /opt/second-brain.
docker, systemctl, curl (health + Telegram), pip and alembic are PATH/VENV_BIN
stubs that append to one call log, so each test asserts what the deploy DID.
Nothing here touches a real host, database or service manager.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"

HEALTHY = json.dumps({"components": {"api": {"status": "ok"}}})
UNHEALTHY = json.dumps({"components": {"api": {"status": "down"}}})


def _git(cwd: Path, *args: str) -> str:
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@t",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@t",
    }
    out = subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, check=True, env=env
    )
    return out.stdout.strip()


def _stub(path: Path, body: str) -> None:
    path.write_text("#!/usr/bin/env bash\n" + body)
    path.chmod(0o755)


class World:
    def __init__(self, tmp: Path) -> None:
        self.tmp = tmp
        self.log = tmp / "calls.log"
        self.health = tmp / "health.json"
        self.origin = tmp / "origin.git"
        self.dev = tmp / "dev"
        self.root = tmp / "server"
        self.bin = tmp / "bin"
        self.venv = tmp / "venv-bin"
        for d in (self.bin, self.venv, tmp / "units", tmp / "tmp"):
            d.mkdir()
        _git(tmp, "init", "-q", "--bare", "-b", "main", str(self.origin))
        _git(tmp, "clone", "-q", str(self.origin), str(self.dev))
        (self.dev / "scripts").mkdir()
        for name in ("auto-deploy.sh", "lib.sh"):
            shutil.copy(SCRIPTS / name, self.dev / "scripts" / name)
        (self.dev / "src/brain/db/migrations/versions").mkdir(parents=True)
        (self.dev / "src/brain/db/migrations/versions/a.py").write_text("# a\n")
        (self.dev / "deploy/systemd").mkdir(parents=True)
        (self.dev / "deploy/systemd/second-brain-api.service").write_text("[Unit]\n")
        (self.dev / "app.txt").write_text("v1\n")
        self.first = self.commit("first")
        _git(tmp, "clone", "-q", str(self.origin), str(self.root))
        self.health.write_text(HEALTHY)
        log = self.log
        _stub(
            self.bin / "docker",
            f'echo "docker $*" >>{log}\n[ "${{DOCKER_FAIL:-}}" = 1 ] && exit 1\necho DUMPDATA\n',
        )
        _stub(self.bin / "systemctl", f'echo "systemctl $*" >>{log}\n')
        _stub(
            self.bin / "curl",
            f'if [[ " $* " == *" -K - "* ]]; then cat >/dev/null; '
            f'echo "notify $*" >>{log}; exit 0; fi\ncat {self.health}\n',
        )
        _stub(self.venv / "pip", f'echo "pip $*" >>{log}\n')
        _stub(self.venv / "alembic", f'echo "alembic $*" >>{log}\n')

    def commit(self, message: str, files: dict[str, str] | None = None) -> str:
        for rel, text in (files or {"app.txt": message + "\n"}).items():
            path = self.dev / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text)
        _git(self.dev, "add", "-A")
        _git(self.dev, "commit", "-q", "-m", message)
        _git(self.dev, "push", "-q", "origin", "HEAD:main")
        return _git(self.dev, "rev-parse", "HEAD")

    def run(self, **env: str) -> subprocess.CompletedProcess[str]:
        run_env = {
            **os.environ,
            "PATH": f"{self.bin}:{os.environ['PATH']}",
            "DEPLOY_ROOT": str(self.root),
            "DEPLOY_BACKUP_DIR": str(self.tmp / "backups"),
            "DEPLOY_STATE_DIR": str(self.tmp / "state"),
            "DEPLOY_UNIT_DIR": str(self.tmp / "units"),
            "DEPLOY_HEALTH_TRIES": "2",
            "DEPLOY_HEALTH_SLEEP": "0",
            "VENV_BIN": str(self.venv),
            "TMPDIR": str(self.tmp / "tmp"),
            "TELEGRAM_BOT_TOKEN": "123:abc",
            "TELEGRAM_ALLOWED_USER_IDS": "7",
            **env,
        }
        return subprocess.run(
            ["bash", str(self.root / "scripts/auto-deploy.sh")],
            capture_output=True,
            text=True,
            timeout=60,
            env=run_env,
        )

    def calls(self) -> str:
        return self.log.read_text() if self.log.exists() else ""

    def head(self) -> str:
        return _git(self.root, "rev-parse", "HEAD")


@pytest.fixture
def world(tmp_path: Path) -> World:
    if shutil.which("git") is None:
        pytest.skip("git not installed")
    return World(tmp_path)


def test_up_to_date_does_nothing(world: World) -> None:
    result = world.run()
    assert result.returncode == 0, result.stderr
    assert world.calls() == ""


def test_new_commit_backs_up_migrates_restarts_and_reports(world: World) -> None:
    new = world.commit("feat: thing")

    result = world.run()

    assert result.returncode == 0, result.stderr
    assert world.head() == new
    calls = world.calls().splitlines()
    order = [c.split()[0] + " " + c.split()[1] for c in calls]
    assert order[:5] == [
        "docker compose",
        "pip install",
        "alembic upgrade",
        "systemctl restart",
        "notify -s",
    ]
    assert "second-brain-api second-brain-telegram second-brain-email" in calls[3]
    assert "✅ Deployed" in calls[4] and "feat: thing" in calls[4]
    assert "123:abc" not in world.calls()  # the token never reaches argv
    dumps = list((world.tmp / "backups").glob(f"pre-{new[:7]}-*.dump"))
    assert len(dumps) == 1 and dumps[0].read_text() == "DUMPDATA\n"


def test_unhealthy_deploy_rolls_back_code_and_is_not_retried(world: World) -> None:
    new = world.commit("feat: broken")
    world.health.write_text(UNHEALTHY)

    world.run()

    assert world.head() == world.first  # rolled back
    assert "↩️" not in world.calls()  # rollback is unhealthy too in this world
    assert "rollback" in world.calls() and "❌" in world.calls()

    world.log.unlink()
    world.health.write_text(HEALTHY)
    world.run()
    assert world.calls() == ""  # the failed sha is skipped on the next tick
    assert (world.tmp / "state/deploy-failed-sha").read_text().strip() == new


def test_rollback_reports_success_when_old_code_is_healthy(world: World) -> None:
    world.commit("feat: broken")
    # healthy only on the 3rd probe: the 2 tries for the new code fail, the
    # rollback's first probe passes
    counter = world.tmp / "n"
    counter.write_text("0")
    from textwrap import dedent

    _stub(
        world.bin / "curl",
        dedent(
            f"""\
            if [[ " $* " == *" -K - "* ]]; then cat >/dev/null; echo "notify $*" >>{world.log}; exit 0; fi
            n=$(( $(cat {counter}) + 1 )); echo $n >{counter}
            if [ $n -ge 3 ]; then echo '{HEALTHY}'; else echo '{UNHEALTHY}'; fi
            """
        ),
    )

    world.run()

    assert world.head() == world.first
    assert "↩️ Deploy of" in world.calls()


def test_schema_change_is_never_rolled_back(world: World) -> None:
    new = world.commit("feat: new table", {"src/brain/db/migrations/versions/b.py": "# b\n"})
    world.health.write_text(UNHEALTHY)

    world.run()

    assert world.head() == new  # code stays: the schema already moved
    assert "did NOT roll back" in world.calls()
    assert "downgrade" not in world.calls()


def test_failed_backup_changes_nothing(world: World) -> None:
    world.commit("feat: thing")

    world.run(DOCKER_FAIL="1")

    assert world.head() == world.first
    assert "backup failed" in world.calls()
    assert "pip" not in world.calls() and "restart" not in world.calls()


def test_changed_units_are_installed_and_reloaded(world: World) -> None:
    world.commit("deploy: unit", {"deploy/systemd/second-brain-api.service": "[Unit]\n#x\n"})

    world.run()

    assert (world.tmp / "units/second-brain-api.service").read_text() == "[Unit]\n#x\n"
    assert "systemctl daemon-reload" in world.calls()


def test_diverged_checkout_is_left_alone(world: World) -> None:
    (world.root / "app.txt").write_text("local\n")
    _git(world.root, "commit", "-q", "-am", "local edit")
    local = world.head()
    world.commit("feat: thing")

    world.run()

    assert world.head() == local
    assert "Deploy skipped" in world.calls()
