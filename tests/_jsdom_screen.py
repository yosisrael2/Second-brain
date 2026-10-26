"""Behavior harness for the Memory screen: the REAL served files
(index.html + app.js + memory.js) executed in jsdom by node.

pytest collects these as normal tests; each hands a SCENARIO (JS) to the
runner, which boots the screen against a fetch stub and reports JSON
assertions. Skips (loudly, as a skip — not a pass) when node or jsdom is
unavailable, so CI without jsdom still runs the service/API tests.
"""

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from brain.main import create_app

_RUNNER = Path(__file__).parent / "_jsdom_screen_runner.mjs"


def screen_behavior(scenario: str, fixtures: dict) -> dict:
    """Run `scenario` against the real served screen in jsdom.

    The scenario is async JS with `fixtures` in scope; it must end with
    `return {...}` inside an async IIFE the runner wraps around it. The
    runner prints one JSON line: {"ok": bool, "checks": [...], ...}.
    """
    node = shutil.which("node")
    if node is None:
        if os.environ.get("CI"):
            raise AssertionError(
                "node not available but CI is set — the screen-behavior tests "
                "must never silently skip in CI"
            )
        pytest.skip("node not available")
    client = TestClient(create_app())
    html = client.get("/cockpit").text
    app_js = client.get("/cockpit/app.js").text
    memory_js = client.get("/cockpit/memory.js").text
    script = _RUNNER.read_text()
    script = script.replace("__HTML__", json.dumps(html))
    script = script.replace("__APP_JS__", json.dumps(app_js))
    script = script.replace("__MEMORY_JS__", json.dumps(memory_js))
    script = script.replace("__FIXTURES__", json.dumps(fixtures))
    script = script.replace("__SCENARIO__", scenario)
    result = subprocess.run(
        [node, "--input-type=module", "-"],
        input=script,
        capture_output=True,
        text=True,
        timeout=120,
    )
    if result.returncode != 0:
        tail = result.stdout[-2000:]
        errs = result.stderr[-2000:]
        raise AssertionError(f"jsdom runner failed:\n{tail}\n{errs}")
    lines = [line for line in result.stdout.splitlines() if line.startswith("{")]
    if not lines:
        out = result.stdout[-1000:]
        errs = result.stderr[-500:]
        raise AssertionError(f"runner printed no JSON: {out} {errs}")
    payload = json.loads(lines[-1])
    if payload.get("skipped"):
        if os.environ.get("CI"):
            raise AssertionError(
                f"jsdom missing but CI is set — the screen-behavior tests "
                f"must never silently skip in CI ({payload['skipped']})"
            )
        pytest.skip(payload["skipped"])
    if payload.get("failed"):
        raise AssertionError(payload["failed"])
    return payload


def assert_check(payload: dict, name: str) -> None:
    """Fail with the runner's detail when a check did not pass."""
    for check in payload.get("checks", []):
        if check["name"] == name:
            assert check["pass"], f"{name}: {check.get('detail', '')}"
            return
    raise AssertionError(f"check {name!r} never ran")
