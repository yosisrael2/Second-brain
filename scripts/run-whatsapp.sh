#!/usr/bin/env bash
# run-whatsapp.sh — foreground launcher for the WhatsApp poller (launchd contract).
#
# com.secondbrain.whatsapp runs this in the foreground; it maps the poller's
# exit codes onto the KeepAlive contract (KeepAlive SuccessfulExit = false:
# launchd restarts ONLY a non-zero exit):
#
#   0   poller disabled (no WHATSAPP_BRIDGE_DB configured)
#                                       -> exit 0, NO restart
#   78 (EX_CONFIG: live-store guard    -> exit 0, NO restart — a permanent
#       refusal, bridge store missing)         condition would loop forever
#   1 (transient) or a crash/signal    -> exit != 0 (launchd restarts)
#
# The underlying codes are preserved in the log line so the exit-code contract
# stays observable.
#
# THIS SCRIPT IS THE ONLY PLACE that exports BRAIN_ALLOW_LIVE=1 and
# WHATSAPP_BRIDGE_DB for the poller: the launchd path IS the sanctioned live
# writer; a bare worktree `brain whatsapp` is refused by the same
# brain.config.assert_not_live_store guard as the API and `brain enrich`.
# The bridge store is opened READ-ONLY by the poller (never written, never
# sent to) — the variable only says WHERE to read.
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
cd "$ROOT" || exit 1
# Test seam: BRAIN_BIN overrides the binary path (tests never touch the venv).
BRAIN="${BRAIN_BIN:-$ROOT/.venv/bin/brain}"

[ -x "$BRAIN" ] || {
  echo "run-whatsapp: $BRAIN missing — provision the venv first" >&2
  exit 0  # EX_CONFIG-shaped, but as 0: a missing venv is permanent; never restart-loop
}

# shellcheck source=lib.sh
# shellcheck disable=SC1091  # dynamic path: lib.sh sits next to this script
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
load_env_file  # fills unset vars from .env (DATABASE_URL, WHATSAPP_BRIDGE_DB, ...); real env wins

# The launchd path IS the sanctioned live writer: the same one-definition
# guard (brain.config.assert_not_live_store) lets THIS process through while
# a bare worktree 'brain whatsapp' refuses.
export BRAIN_ALLOW_LIVE=1
# Where the poller READS the bridge store (read-only; env/.env override wins).
# HOME-less environments (some CI/launchd contexts) fall through: the CLI then
# reports EX_CONFIG 'not configured' instead of guessing a path.
if [ -z "${WHATSAPP_BRIDGE_DB:-}" ] && [ -n "${HOME:-}" ]; then
  export WHATSAPP_BRIDGE_DB="$HOME/git/whatsapp-mcp/whatsapp-bridge/store/messages.db"
fi

"$BRAIN" whatsapp "$@"
rc=$?
case "$rc" in
  0|78)
    echo "run-whatsapp: brain whatsapp exited $rc (permanent; not restarting)"
    exit 0
    ;;
  *)
    echo "run-whatsapp: brain whatsapp exited $rc (transient; launchd will restart)" >&2
    exit "$rc"
    ;;
esac
