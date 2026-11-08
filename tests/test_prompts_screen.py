"""Behavior test for the cockpit Prompts tab: the REAL served index.html +
app.js + prompts.js run in jsdom against a small fetch stub of /prompts.

Covers the editor loop end to end: list -> select -> edit -> a rejected save
shows the server's 422 detail and keeps the draft -> a valid save -> reset to
default -> restore from history; plus the memory file with Hebrew text in a
dir="auto" textarea. Skips (as a skip, never a pass) without node/jsdom,
unless CI is set — same policy as tests/_jsdom_screen.py.
"""

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from brain.main import create_app
from brain.services.prompts import get_spec

_REPO_ROOT = Path(__file__).resolve().parents[1]

_RUNNER = Path(__file__).parent / "_prompts_screen_runner.mjs"


def test_prompts_tab_edit_save_reset_restore() -> None:
    node = shutil.which("node")
    if node is None:
        if os.environ.get("CI"):
            raise AssertionError("node not available but CI is set")
        pytest.skip("node not available")
    client = TestClient(create_app())
    defaults = {
        "memory_file": "",
        "whatsapp_triage": get_spec("whatsapp_triage").default,
    }
    script = (
        _RUNNER.read_text()
        .replace("__HTML__", json.dumps(client.get("/cockpit").text))
        .replace("__APP_JS__", json.dumps(client.get("/cockpit/app.js").text))
        .replace("__PROMPTS_JS__", json.dumps(client.get("/cockpit/prompts.js").text))
        .replace("__DEFAULTS__", json.dumps(defaults))
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
    assert len(payload["checks"]) == 23, payload["checks"]  # every step ran
    failed = [check for check in payload["checks"] if not check["pass"]]
    assert not failed, failed
