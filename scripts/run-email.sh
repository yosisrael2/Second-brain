#!/usr/bin/env bash
# run-email.sh — foreground launcher for the Gmail poller (launchd contract).
#
# com.secondbrain.email runs this in the foreground; it maps `brain gmail-poll`
# exit codes onto the KeepAlive contract (KeepAlive SuccessfulExit = false:
# launchd restarts ONLY a non-zero exit):
#
#   0   clean exit                       -> exit 0, NO restart
#   78 (EX_CONFIG: no GMAIL_LABEL_ID,    -> exit 0, NO restart — config errors
#       no keychain token)                   are permanent until the user acts
#   1 (transient) or a crash/signal      -> exit != 0 (launchd restarts)
#
# "Needs re-auth" and "label missing" are NOT exits: the poller pauses
# internally (5-minute stalls, visible in the cockpit heartbeat) and keeps the
# agent alive, so a revoked token never flaps the agent.
#
# The underlying codes are preserved in the log line so the exit-code contract
# stays observable. Secrets (the keychain refresh token) never appear here or
# in the plist: gmail-poll reads them via the login keychain itself.
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
cd "$ROOT" || exit 1
# Test seam: BRAIN_BIN overrides the binary path (tests never touch the venv).
BRAIN="${BRAIN_BIN:-$ROOT/.venv/bin/brain}"

[ -x "$BRAIN" ] || {
  echo "run-email: $BRAIN missing — provision the venv first" >&2
  exit 0  # EX_CONFIG-shaped, but as 0: a missing venv is permanent; never restart-loop
}

# shellcheck source=lib.sh
# shellcheck disable=SC1091  # dynamic path: lib.sh sits next to this script
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
load_env_file "${BRAIN_ENV_FILE:-$ROOT/.env}"  # fills unset vars (DATABASE_URL, GMAIL_*); real env wins
# The launchd path IS the real service: opt in to the live-store startup guard
# (brain.config.assert_not_live_store refuses bare worktree uvicorns otherwise).
export BRAIN_ALLOW_LIVE=1
export COMPOSE_PROJECT_NAME="${COMPOSE_PROJECT_NAME:-second-brain}"
export PYTHONUNBUFFERED=1

"$BRAIN" gmail-poll
rc=$?
case "$rc" in
  0|78)
    echo "run-email: brain gmail-poll exited $rc (permanent; not restarting)"
    exit 0
    ;;
  *)
    echo "run-email: brain gmail-poll exited $rc (transient; launchd will restart)" >&2
    exit "$rc"
    ;;
esac
