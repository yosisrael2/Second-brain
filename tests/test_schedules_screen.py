"""Behavior test for the cockpit Scheduled tab: the REAL served index.html +
app.js + schedules.js run in jsdom against a small fetch stub of /schedules.

Covers: empty state -> New task (weekly preset, live preview) -> card with
the schedule in words, user text rendered as text (dir="auto") -> edit via
the weekly-days and daily presets -> a rejected custom cron shows the 422
detail and keeps the draft -> pause / resume / run now -> run history with
outputs -> delete (confirmed). Skips (as a skip, never a pass) without
node/jsdom, unless CI is set — same policy as tests/_jsdom_screen.py.
"""

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from brain.main import create_app

_REPO_ROOT = Path(__file__).resolve().parents[1]

_RUNNER = Path(__file__).parent / "_schedules_screen_runner.mjs"


def test_scheduled_tab_create_edit_pause_run_history_delete() -> None:
    node = shutil.which("node")
    if node is None:
        if os.environ.get("CI"):
            raise AssertionError("node not available but CI is set")
        pytest.skip("node not available")
    client = TestClient(create_app())
    schedules_js = client.get("/cockpit/schedules.js")
    assert schedules_js.status_code == 200
    script = (
        _RUNNER.read_text()
        .replace("__HTML__", json.dumps(client.get("/cockpit").text))
        .replace("__APP_JS__", json.dumps(client.get("/cockpit/app.js").text))
        .replace("__SCHEDULES_JS__", json.dumps(schedules_js.text))
    )
    result = subprocess.run(
        [node, "--input-type=module", "-"],
        input=script,
        capture_output=True,
        text=True,
        timeout=120,
        cwd=_REPO_ROOT,
    )
    lines = [line for line in result.stdout.splitlines() if line.startswith("{")]
    assert lines, f"runner printed no JSON: {result.stdout[-1000:]} {result.stderr[-1000:]}"
    payload = json.loads(lines[-1])
    if payload.get("skipped"):
        if os.environ.get("CI"):
            raise AssertionError(f"jsdom missing but CI is set ({payload['skipped']})")
        pytest.skip(payload["skipped"])
    assert len(payload["checks"]) == 32, payload["checks"]  # every step ran
    failed = [check for check in payload["checks"] if not check["pass"]]
    assert not failed, failed


def test_cockpit_never_renders_schedule_data_with_innerhtml() -> None:
    source = (_REPO_ROOT / "src/brain/cockpit/schedules.js").read_text()
    assert "innerHTML" not in source.replace("never innerHTML", "")
