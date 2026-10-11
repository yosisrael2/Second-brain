#!/usr/bin/env bash
# Stop the dev stack: uvicorn (pid file), optionally compose stop / volume purge.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

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
EOF
}

say() { printf '%s\n' "$*"; }

stop_server() {
  if [ ! -f "$PID_FILE" ]; then
    say "uvicorn: not running (no pid file)"
    return 0
  fi
  local pid
  pid="$(cat "$PID_FILE")"
  local cmd
  cmd="$(ps -p "$pid" -o command= 2>/dev/null || true)"
  if [[ "$cmd" != *uvicorn*brain.main:app* ]]; then
    say "uvicorn: pid $pid in $PID_FILE is not our server (now: ${cmd:-<gone>}); leaving it alone"
    rm -f "$PID_FILE"
    return 0
  fi
  kill "$pid"
  for _ in $(seq 1 20); do
    kill -0 "$pid" 2>/dev/null || break
    sleep 0.5
  done
  if kill -0 "$pid" 2>/dev/null; then
    say "uvicorn: pid $pid did not exit; sending SIGKILL"
    kill -9 "$pid" 2>/dev/null || true
  fi
  rm -f "$PID_FILE"
  say "uvicorn: stopped"
}

stop_compose() {
  say "docker: compose stop (project ${COMPOSE_PROJECT_NAME:-second-brain-dev})"
  docker compose stop
}

purge_volumes() {
  say "WARNING: --purge-data deletes the postgres and qdrant volumes (ALL stored items and vectors)."
  read -r -p "Type PURGE to continue: " answer
  if [ "$answer" != "PURGE" ]; then
    die "aborted (volumes untouched)"
  fi
  say "docker: compose down -v"
  docker compose down -v
}

die() { printf 'dev-stop: %s\n' "$*" >&2; exit 1; }

while [ $# -gt 0 ]; do
  case "$1" in
    --down) DOWN=1 ;;
    --purge-data) PURGE=1; DOWN=1 ;;
    --help) usage; exit 0 ;;
    *) usage >&2; die "unknown flag: $1" ;;
  esac
  shift
done

stop_server
if [ "$PURGE" -eq 1 ]; then
  purge_volumes
elif [ "$DOWN" -eq 1 ]; then
  stop_compose
fi
