#!/usr/bin/env bash
# Start the local dev stack: postgres + qdrant, schema, optionally reindex, uvicorn.
# Stop with scripts/dev-stop.sh. State lives in .dev/ (gitignored).
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

PORT="${PORT:-8000}"
COMPOSE_PROJECT_NAME="${COMPOSE_PROJECT_NAME:-second-brain-dev}"
export COMPOSE_PROJECT_NAME

RELOAD=1
REINDEX=0
LOG=".dev/uvicorn.log"
PID_FILE=".dev/uvicorn.pid"
DOCKER=(docker)

usage() {
  cat <<'EOF'
Usage: scripts/dev-start.sh [--no-reload] [--reindex] [--help]

Starts postgres + qdrant (docker compose), applies migrations, then runs
uvicorn in the background. State (pid, log) lives in .dev/.

  --no-reload   run uvicorn without --reload
  --reindex     run 'brain reindex' after migrations
  --help        show this help

Environment: PORT (default 8000), COMPOSE_PROJECT_NAME (default second-brain-dev),
DOCKER_CONTEXT (falls back to the rancher-desktop context).
EOF
}

say() { printf '%s\n' "$*"; }
die() { printf 'dev-start: %s\n' "$*" >&2; exit 1; }

port_listener() {
  lsof -nP -iTCP:"$1" -sTCP:LISTEN 2>/dev/null | tail -n +2 || true
}

compose_holders() {
  # Container names of RUNNING compose containers publishing $1.
  "${DOCKER[@]}" ps --format '{{.Names}}\t{{.Ports}}' | grep -E "[^>]$1->" | cut -f1 || true
}

check_docker() {
  if ! docker info >/dev/null 2>&1; then
    if docker --context rancher-desktop info >/dev/null 2>&1; then
      say "docker: daemon reachable via context 'rancher-desktop' (export DOCKER_CONTEXT=rancher-desktop to silence)"
      DOCKER=(docker --context rancher-desktop)
    else
      die "docker daemon not reachable (start Rancher Desktop / Docker Desktop first)"
    fi
  fi
}

check_python_env() {
  if [ ! -x .venv/bin/python ]; then
    say "python: no .venv found — creating one (python3.11)"
    python3.11 -m venv .venv || die "python3.11 not found; install it first"
    .venv/bin/pip install -q -e '.[dev]' || die "pip install -e '.[dev]' failed"
  fi
}

load_env_file() {
  # Export .env vars into the environment without printing values; real
  # environment wins (never overridden here). uvicorn re-reads .env itself.
  while IFS='=' read -r key value; do
    [[ "$key" =~ ^[A-Z_][A-Z0-9_]*$ ]] || continue
    [ -n "${!key:-}" ] && continue
    value="${value%\"}"
    value="${value#\"}"
    value="${value%\'}"
    value="${value#\'}"
    export "$key=$value"
  done < <(grep -E '^[A-Z_][A-Z0-9_]*=' .env || true)
}

check_env_file() {
  if [ ! -f .env ]; then
    cp .env.example .env
    say "env: created .env from .env.example — fill in what you need (JEV_API_KEY for classification; nothing required for LLM/embeddings). Values are never printed by this script."
  fi
  load_env_file
  if grep -q '^JEV_API_KEY=..*' .env; then
    say "jev: API key set"
  else
    say "jev: API key NOT set (items stay in inbox; search skips rerank)"
  fi
  if command -v claude >/dev/null 2>&1 && claude -p ok >/dev/null 2>&1; then
    say "claude: login ok (/ask will use it)"
  else
    say "claude: NOT logged in (warning only; /ask returns 502 until you run 'claude' and /login)"
  fi
}

pg_url() {
  # DATABASE_URL is a SQLAlchemy URL (postgresql+psycopg://...); psycopg wants plain postgresql://
  echo "${DATABASE_URL:-postgresql+psycopg://brain:brain@localhost:5432/brain}" | sed 's/+psycopg//'
}

pg_port() {
  local port
  port="$(echo "${DATABASE_URL:-postgresql+psycopg://brain:brain@localhost:5432/brain}" \
    | sed -E 's/.*:([0-9]+)\/.*/\1/')"
  [[ "$port" =~ ^[0-9]+$ ]] && echo "$port" || echo 5432
}

check_postgres() {
  # Returns 0 when a usable postgres answers on DATABASE_URL (ours or reused).
  if .venv/bin/python -c "
import sys, psycopg
psycopg.connect('$(pg_url)', connect_timeout=3).close()
" 2>/dev/null; then
    return 0
  fi
  return 1
}

resolve_port_conflicts() {
  # Reuse healthy foreign compose containers; never touch them. Fail loudly otherwise.
  local pg_port qdrant_port start_db start_qdrant
  pg_port="$(pg_port)"
  qdrant_port="$(echo "${QDRANT_URL:-http://localhost:6333}" | sed -E 's/.*:([0-9]+)\/?$/\1/')"
  start_db=1
  start_qdrant=1

  if [ -n "$(port_listener "$pg_port")" ]; then
    if check_postgres; then
      local holders
      holders="$(compose_holders "$pg_port" | tr '\n' ' ')"
      say "postgres: reusing what already serves localhost:${pg_port}${holders:+ (held by: $holders)} — left untouched"
      start_db=0
    else
      say "port ${pg_port} is held by:" >&2
      port_listener "$pg_port" | sed 's/^/  /' >&2
      compose_holders "$pg_port" | sed 's/^/  compose container: /' >&2
      die "port ${pg_port} is taken by another project and DATABASE_URL does not work against it. Stop that project first (docker compose stop in its directory) or point DATABASE_URL at a free port."
    fi
  fi

  local qdrant_url="${QDRANT_URL:-http://localhost:6333}"
  if [ -n "$(port_listener "$qdrant_port")" ]; then
    if curl -sf "$qdrant_url/healthz" >/dev/null 2>&1; then
      local holders
      holders="$(compose_holders "$qdrant_port" | tr '\n' ' ')"
      say "qdrant: reusing what already serves ${qdrant_url}${holders:+ (held by: $holders)} — left untouched"
      start_qdrant=0
    else
      say "port ${qdrant_port} is held by:" >&2
      port_listener "$qdrant_port" | sed 's/^/  /' >&2
      die "port ${qdrant_port} is taken by something that is not a healthy Qdrant. Stop it or point QDRANT_URL elsewhere."
    fi
  fi

  START_SERVICES=()
  if [ "$start_db" -eq 1 ]; then START_SERVICES+=("db"); fi
  if [ "$start_qdrant" -eq 1 ]; then START_SERVICES+=("qdrant"); fi
}

start_infra() {
  resolve_port_conflicts
  if [ "${#START_SERVICES[@]}" -eq 0 ]; then
    say "docker: both services already served by existing healthy containers — nothing to start"
    return 0
  fi
  say "docker: starting ${START_SERVICES[*]} (project ${COMPOSE_PROJECT_NAME})..."
  "${DOCKER[@]}" compose up -d "${START_SERVICES[@]}"
  "${DOCKER[@]}" compose up -d --wait --wait-timeout 60 "${START_SERVICES[@]}" \
    || die "containers did not become healthy within 60s"
}

migrate() {
  say "db: alembic upgrade head"
  .venv/bin/alembic upgrade head || die "alembic failed"
}

start_server() {
  mkdir -p .dev
  if [ -f "$PID_FILE" ] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
    die "already running (pid $(cat "$PID_FILE")); use scripts/dev-stop.sh first"
  fi
  if [ -n "$(port_listener "$PORT")" ]; then
    port_listener "$PORT" | awk '{print "  held by pid", $2}' >&2
    die "port $PORT is busy"
  fi
  local reload_flag=""
  [ "$RELOAD" -eq 1 ] && reload_flag="--reload"
  say "uvicorn: starting on port $PORT (log: $LOG)"
  nohup .venv/bin/uvicorn brain.main:app --port "$PORT" $reload_flag > "$LOG" 2>&1 &
  echo $! > "$PID_FILE"
}

wait_for_health() {
  local base="http://127.0.0.1:${PORT}"
  for _ in $(seq 1 30); do
    if curl -sf "$base/health" >/dev/null 2>&1; then
      say "health: OK"
      say "API:      $base"
      say "API docs: $base/docs"
      say "log:      $LOG"
      say "stop:     scripts/dev-stop.sh"
      say "smoke:    curl -s -X POST ${base}/items -H 'Content-Type: application/json' -d '{\"raw_text\": \"Renew the TLS certificate before Friday\"}'"
      return 0
    fi
    if ! kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
      tail -5 "$LOG" >&2
      die "uvicorn exited during startup"
    fi
    sleep 1
  done
  tail -5 "$LOG" >&2
  die "server did not become healthy within 30s"
}

while [ $# -gt 0 ]; do
  case "$1" in
    --no-reload) RELOAD=0 ;;
    --reindex) REINDEX=1 ;;
    --help) usage; exit 0 ;;
    *) usage >&2; die "unknown flag: $1" ;;
  esac
  shift
done

check_docker
check_python_env
check_env_file
start_infra
migrate
if [ "$REINDEX" -eq 1 ]; then
  say "reindex: rebuilding vectors"
  .venv/bin/brain reindex || die "brain reindex failed (see message above)"
fi
start_server
wait_for_health
