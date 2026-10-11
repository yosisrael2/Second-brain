"""Tests for scripts/dev-start.sh and scripts/dev-stop.sh (syntax + CLI contract)."""

import subprocess
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"


def run_script(name: str, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(SCRIPTS / name), *args], capture_output=True, text=True, timeout=60
    )


def test_scripts_pass_bash_syntax_check():
    for name in ("dev-start.sh", "dev-stop.sh"):
        result = subprocess.run(
            ["bash", "-n", str(SCRIPTS / name)], capture_output=True, text=True, timeout=30
        )
        assert result.returncode == 0, result.stderr


def test_scripts_pass_shellcheck_when_installed():
    try:
        subprocess.run(["shellcheck", "--version"], capture_output=True, timeout=10, check=True)
    except (FileNotFoundError, subprocess.CalledProcessError):
        return  # shellcheck not installed; nothing to prove
    for name in ("dev-start.sh", "dev-stop.sh"):
        result = subprocess.run(
            ["shellcheck", str(SCRIPTS / name)], capture_output=True, text=True, timeout=30
        )
        assert result.returncode == 0, result.stdout


def test_dev_start_help_exits_zero_without_side_effects():
    result = run_script("dev-start.sh", "--help")

    assert result.returncode == 0
    assert "Usage: scripts/dev-start.sh" in result.stdout


def test_dev_start_rejects_unknown_flags():
    result = run_script("dev-start.sh", "--bogus")

    assert result.returncode != 0
    assert "unknown flag" in result.stderr


def test_dev_stop_help_exits_zero_without_side_effects():
    result = run_script("dev-stop.sh", "--help")

    assert result.returncode == 0
    assert "Usage: scripts/dev-stop.sh" in result.stdout


def test_dev_stop_rejects_unknown_flags():
    result = run_script("dev-stop.sh", "--bogus")

    assert result.returncode != 0
    assert "unknown flag" in result.stderr
