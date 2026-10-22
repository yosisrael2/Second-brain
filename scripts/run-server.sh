#!/usr/bin/env bash
# run-server.sh — foreground launcher for the API (launchd service contract).
#
# com.secondbrain.api runs this script in the FOREGROUND: stdout/stderr ARE
# the service log (launchd redirects them to ~/Library/Logs/second-brain/).
# Uvicorn logs to stderr by default; PYTHONUNBUFFERED=1 keeps lines flowing
# without buffering delays. Never daemonize here — launchd owns the lifecycle.
#
# The repo's .venv must exist (scripts/dev-start.sh creates it once; the venv
# is shared between dev and service use). Secrets come from .env via
# Settings(env_file=".env") with WorkingDirectory pinned to the repo root, so
# no secret ever lands in the launchd plist.
#
# Usage: scripts/run-server.sh            (PORT env overrides, default 27800)
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
cd "$ROOT"
# shellcheck source=lib.sh
# shellcheck disable=SC1091  # dynamic path: lib.sh sits next to this script
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

PORT="${PORT:-27800}"
# Test seams (tests never touch the real venv): UVICORN_BIN/BRAIN_BIN override
# the binary paths; everything else runs the real code path.
UVICORN="${UVICORN_BIN:-$ROOT/.venv/bin/uvicorn}"

[ -x "$UVICORN" ] || {
  say "run-server: $UVICORN missing — provision the venv first (scripts/dev-start.sh does)" >&2
  # Exit 0, not 78: KeepAlive SuccessfulExit=false restarts ANY non-zero exit,
  # and a missing venv is permanent — restart-looping would be wrong.
  exit 0
}

load_env_file  # fills unset vars from .env (DATABASE_URL, QDRANT_URL, ...); real env wins
export PYTHONUNBUFFERED=1

exec "$UVICORN" brain.main:app --host 127.0.0.1 --port "$PORT"
