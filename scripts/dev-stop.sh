#!/usr/bin/env bash
# Stop the dev stack: uvicorn (pid file), optionally compose stop / volume purge.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
# shellcheck source=lib.sh
# shellcheck disable=SC1091  # dynamic path: lib.sh sits next to this script
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

PID_FILE=".dev/uvicorn.pid"
DOWN=0
PURGE=0

usage() {
  cat <<'EOF'
Usage: scripts/dev-stop.sh [--down] [--purge-data] [--help]

  (default)     stop the uvicorn dev server (pid from .dev/uvicorn.pid)
  --down        also stop the docker compose services (postgres + qdrant)
  --purge-data  with --down: also DELETE docker volumes (postgres + qdrant data);
                asks for explicit confirmation
  --help        show this help

The compose project name resolves exactly like dev-start.sh (.env honored,
default second-brain-dev) and is printed before any docker call.
EOF
}

say() { printf '%s\n' "$*"; }
die() { printf 'dev-stop: %s\n' "$*" >&2; exit 1; }

is_our_server() {
  # $1 = pid; true when the process is the uvicorn this script started.
  local cmd
  cmd="$(ps -p "$1" -o command= 2>/dev/null || true)"
  [[ "$cmd" == *uvicorn*brain.main:app* ]]
}

stop_server() {
  if [ ! -f "$PID_FILE" ]; then
    say "uvicorn: not running (no pid file)"
    return 0
  fi
  local pid
  pid="$(cat "$PID_FILE")"
  if ! is_our_server "$pid"; then
    local cmd
    cmd="$(ps -p "$pid" -o command= 2>/dev/null || true)"
    say "uvicorn: pid $pid in $PID_FILE is not our server (now: ${cmd:-<gone>}); leaving it alone"
    rm -f "$PID_FILE"
    return 0
  fi
  # Kill the whole tree first (uvicorn --reload runs a child worker), then the
  # supervisor; escalate to SIGKILL only if something survives.
  pkill -TERM -P "$pid" 2>/dev/null || true
  kill -TERM "$pid" 2>/dev/null || true
  local _
  for _ in $(seq 1 20); do
    if ! kill -0 "$pid" 2>/dev/null; then break; fi
    if ! pgrep -P "$pid" >/dev/null 2>&1 && ! kill -0 "$pid" 2>/dev/null; then break; fi
    sleep 0.5
  done
  if kill -0 "$pid" 2>/dev/null || pgrep -P "$pid" >/dev/null 2>&1; then
    say "uvicorn: pid $pid survived TERM; sending SIGKILL to the tree"
    pkill -KILL -P "$pid" 2>/dev/null || true
    kill -KILL "$pid" 2>/dev/null || true
  fi
  rm -f "$PID_FILE"
  say "uvicorn: stopped"
}

stop_compose() {
  say "docker: compose stop (project ${COMPOSE_PROJECT_NAME})"
  "${DOCKER[@]}" compose stop
}

purge_volumes() {
  local volumes
  volumes="$("${DOCKER[@]}" compose config --volumes | tr '\n' ' ')"
  say "WARNING: --purge-data deletes compose project '${COMPOSE_PROJECT_NAME}' volumes:"
  for volume in $volumes; do
    say "  ${COMPOSE_PROJECT_NAME}_${volume}"
  done
  say "ALL stored items and vectors in that project are gone."
  read -r -p "Type PURGE to continue: " answer
  if [ "$answer" != "PURGE" ]; then
    die "aborted (volumes untouched)"
  fi
  say "docker: compose down -v (project ${COMPOSE_PROJECT_NAME})"
  "${DOCKER[@]}" compose down -v
}

while [ $# -gt 0 ]; do
  case "$1" in
    --down) DOWN=1 ;;
    --purge-data) PURGE=1; DOWN=1 ;;
    --help) usage; exit 0 ;;
    *) usage >&2; die "unknown flag: $1" ;;
  esac
  shift
done

if [ "$PURGE" -eq 1 ] || [ "$DOWN" -eq 1 ]; then
  resolve_docker || die "docker daemon not reachable (needed for --down/--purge-data)"
  resolve_compose_project >/dev/null
  say "compose project: ${COMPOSE_PROJECT_NAME}"
fi

stop_server
if [ "$PURGE" -eq 1 ]; then
  purge_volumes
elif [ "$DOWN" -eq 1 ]; then
  stop_compose
fi
