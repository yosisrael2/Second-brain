#!/usr/bin/env bash
# run-bot.sh — foreground launcher for the Telegram bot (launchd contract).
#
# com.secondbrain.telegram runs this in the foreground; it maps the bot's
# exit codes onto the launchd KeepAlive contract (KeepAlive SuccessfulExit
# = false: launchd restarts ONLY a non-zero exit):
#
#   0   bot disabled (no token)          -> exit 0, NO restart
#   78 (EX_CONFIG: 409 conflict, bad     exit 0, NO restart — a second
#       token, blocked bot)                      poller would 409 forever
#   1 (transient) or a crash/signal      -> exit != 0 (launchd restarts)
#
# The underlying codes are preserved in the log line so the exit-code contract
# stays observable.
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
cd "$ROOT" || exit 1
# Test seam: BRAIN_BIN overrides the binary path (tests never touch the venv).
BRAIN="${BRAIN_BIN:-$ROOT/.venv/bin/brain}"

# The bot builds a vector store and DB sessions at construction, and the
# construction-time live-store guard refuses the live store without an
# explicit opt-in — the bot IS the real service, so opt in here (same
# contract as run-server.sh).
export BRAIN_ALLOW_LIVE=1
# launchd log files must stream, not buffer in 4kB blocks (run-server.sh does
# the same; without it the bot's log can read as empty while it runs fine).
export PYTHONUNBUFFERED=1

[ -x "$BRAIN" ] || {
  echo "run-bot: $BRAIN missing — provision the venv first" >&2
  exit 0  # EX_CONFIG-shaped, but as 0: a missing venv is permanent; never restart-loop
}

"$BRAIN" telegram
rc=$?
case "$rc" in
  0|78)
    echo "run-bot: brain telegram exited $rc (permanent; not restarting)"
    exit 0
    ;;
  *)
    echo "run-bot: brain telegram exited $rc (transient; launchd will restart)" >&2
    exit "$rc"
    ;;
esac
