#!/usr/bin/env bash
# run-connectors.sh — foreground scheduler for the data-source connectors
# (launchd contract for com.secondbrain.connectors).
#
# OPT-IN like the email agent: service.sh installs the agent only when
# BRAIN_CONNECTORS contains a non-email connector. email is NEVER synced here
# — com.secondbrain.email (run-email.sh) owns the Gmail poller.
#
# One pass = one `brain connectors sync <name>` cycle per enabled connector
# (the same code path as the CLI; per-connector failure isolation: a failing
# source never blocks the others in the same pass). Between passes the script
# sleeps BRAIN_CONNECTORS_INTERVAL_S (default 300).
#
# Pass-level exit mapping — the run-email.sh KeepAlive contract
# (KeepAlive SuccessfulExit = false: launchd restarts ONLY a non-zero exit):
#
#   every attempted connector exited 78 (auth/config: needs consent) -> exit 0
#       NO restart — permanent until the user acts (mirrors run-email's
#       EX_CONFIG handling; mixed ok+78 keeps looping: the ok ones keep
#       syncing, isolation first)
#   any transient failure (1/crash)                                  -> exit 1
#       launchd restarts with its throttle backoff; the next pass re-runs
#       every connector (idempotent by external_id/version)
#   otherwise (all ok)                                               -> sleep, loop
#
# The launchd path IS the real service: BRAIN_ALLOW_LIVE=1 is exported (the
# live-store startup guard refuses bare worktree runs otherwise). Secrets
# (keychain refresh tokens) never appear here or in the plist: the runner
# reads them via the login keychain itself.
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
cd "$ROOT" || exit 1
# Test seam: BRAIN_BIN overrides the binary path (tests never touch the venv).
BRAIN="${BRAIN_BIN:-$ROOT/.venv/bin/brain}"

[ -x "$BRAIN" ] || {
  echo "run-connectors: $BRAIN missing — provision the venv first" >&2
  exit 0  # EX_CONFIG-shaped, but as 0: a missing venv is permanent; never restart-loop
}

# shellcheck source=lib.sh
# shellcheck disable=SC1091  # dynamic path: lib.sh sits next to this script
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
load_env_file "${BRAIN_ENV_FILE:-$ROOT/.env}"  # fills unset vars; real env wins
# The launchd path IS the real service: opt in to the live-store startup guard.
export BRAIN_ALLOW_LIVE=1
export COMPOSE_PROJECT_NAME="${COMPOSE_PROJECT_NAME:-second-brain}"
export PYTHONUNBUFFERED=1

INTERVAL="${BRAIN_CONNECTORS_INTERVAL_S:-300}"
case "$INTERVAL" in ''|*[!0-9]*) INTERVAL=300 ;; esac  # a junk value must not crash sleep
# Test seam (never set in production): BRAIN_CONNECTORS_PASSES bounds the
# pass count (default 0 = loop forever). The loop test uses the real
# forever-loop; one-pass mapping tests set 1 so they stay synchronous.
PASSES="${BRAIN_CONNECTORS_PASSES:-0}"
case "$PASSES" in ''|*[!0-9]*) PASSES=0 ;; esac

# The enabled non-email connectors: comma list, whitespace-tolerant.
names=""
if [ -n "${BRAIN_CONNECTORS:-}" ]; then
  old_ifs="$IFS"
  IFS=','
  for part in ${BRAIN_CONNECTORS}; do
    part="$(printf '%s' "$part" | tr -d '[:space:]')"
    [ -z "$part" ] && continue
    [ "$part" = "email" ] && continue  # email keeps its own agent
    names="$names $part"
  done
  IFS="$old_ifs"
  names="${names# }"
fi
if [ -z "$names" ]; then
  echo "run-connectors: no non-email connector in BRAIN_CONNECTORS — nothing to do"
  exit 0
fi

pass=0
while :; do
  pass=$((pass + 1))
  had_transient=0
  all_permanent=1
  for name in $names; do
    "$BRAIN" connectors sync "$name"
    rc=$?
    case "$rc" in
      0) all_permanent=0 ;;
      78) ;;  # permanent for THIS connector (needs consent); the others keep going
      *) all_permanent=0; had_transient=1 ;;
    esac
  done
  if [ "$had_transient" -eq 1 ]; then
    echo "run-connectors: transient failure in pass (launchd will restart with backoff)" >&2
    exit 1
  fi
  if [ "$all_permanent" -eq 1 ]; then
    echo "run-connectors: every connector needs user action (78) — not restarting"
    exit 0
  fi
  # Bounded passes = test seam only; production loops forever (launchd keeps
  # the agent alive across the sleep).
  if [ "$PASSES" -gt 0 ] && [ "$pass" -ge "$PASSES" ]; then
    exit 0
  fi
  sleep "$INTERVAL"
done
