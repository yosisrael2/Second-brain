#!/usr/bin/env bash
# Start the local dev stack: postgres + qdrant, schema, optionally reindex, uvicorn.
# Stop with scripts/dev-stop.sh. State lives in .dev/ (gitignored).
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
# shellcheck source=lib.sh
# shellcheck disable=SC1091  # dynamic path: lib.sh sits next to this script
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

PORT="${PORT:-27800}"
FORCE_PORT=0
RELOAD=1
REINDEX=0
# Overridable for tests so a test run never touches the real .dev state.
LOG="${DEV_LOG_FILE:-.dev/uvicorn.log}"
PID_FILE="${DEV_PID_FILE:-.dev/uvicorn.pid}"

usage() {
  cat <<'EOF'
Usage: scripts/dev-start.sh [--no-reload] [--reindex] [--force-port] [--help]

Starts postgres + qdrant (docker compose), applies migrations, then runs
uvicorn in the background. State (pid, log) lives in .dev/.

Self-healing: a previous instance of our uvicorn (pid file or orphan on the
port) is stopped automatically, so running dev-start twice just restarts.

  --no-reload   run uvicorn without --reload
  --reindex     run 'brain reindex' after migrations
  --force-port  kill a FOREIGN process holding the port without asking
                (never docker/rancher/colima proxies or infra ports)
  --help        show this help

Environment: PORT (default 27800), COMPOSE_PROJECT_NAME (default
second-brain-dev, .env may set it), DOCKER_CONTEXT (falls back to the
rancher-desktop context), HEALTH_TIMEOUT_S (default 30).
EOF
}

die() { printf 'dev-start: %s\n' "$*" >&2; exit 1; }

check_docker() {
  resolve_docker || die "docker daemon not reachable (start Rancher Desktop / Docker Desktop first)"
}

check_python_env() {
  if [ ! -x .venv/bin/python ]; then
    say "python: no .venv found — creating one (python3.11)"
    python3.11 -m venv .venv || die "python3.11 not found; install it first"
    .venv/bin/pip install -q -e '.[dev]' || die "pip install -e '.[dev]' failed"
  fi
}

check_env_file() {
  # BRAIN_ENV_FILE: the tests' seam (like load_env_file's optional path) —
  # a test run points it at a temp file so no test ever creates or modifies
  # the repo checkout's .env (a live-pointing .env kept reappearing in
  # worker worktrees; the culprit was this function via a real-script test).
  local env_file="${BRAIN_ENV_FILE:-.env}"
  if [ ! -f "$env_file" ]; then
    cp .env.example "$env_file"
    say "env: created $env_file from .env.example — fill in what you need (JEV_API_KEY for classification; nothing required for LLM/embeddings). Values are never printed by this script."
    # The example defaults to the LIVE database name. Never print the URL or
    # any value — only the bare dbname comparison below.
    local url db q
    url="$(grep -E '^DATABASE_URL=' "$env_file" | head -1 | cut -d= -f2- || true)"
    q="'"
    url="${url%\"}"
    url="${url#\"}"
    url="${url%"$q"}"
    url="${url#"$q"}"
    db="${url##*/}"
    db="${db%%\?*}"
    if [ "$db" = "brain" ]; then
      say "env: WARNING: the created env file points at database 'brain' (the live database) — in a git worktree set a *_test dbname before running anything against it"
    fi
  fi
  load_env_file "$env_file"
  warn_if_legacy_ports
  if grep -q '^JEV_API_KEY=..*' "$env_file"; then
    say "jev: API key set"
  else
    say "jev: API key NOT set (items stay in inbox; search skips rerank)"
  fi
  if command -v claude >/dev/null 2>&1; then
    say "claude: CLI present (/ask uses your Claude Code login; if /ask returns 502, run 'claude' once and /login)"
  else
    say "claude: CLI NOT installed (warning only; /ask returns 502 until it is installed and logged in)"
  fi
}

pg_check() {
  # Returns 0 when a usable postgres answers on DATABASE_URL (ours or reused).
  # DATABASE_URL travels via the environment: no quoting/interpolation traps
  # (e.g. apostrophes in passwords).
  DATABASE_URL_PLAIN="$(pg_url)" .venv/bin/python -c "
import os, psycopg
psycopg.connect(os.environ['DATABASE_URL_PLAIN'], connect_timeout=3).close()
" 2>/dev/null
}

compose_holders() {
  # Container names of RUNNING compose containers publishing $1.
  "${DOCKER[@]}" ps --format '{{.Names}}\t{{.Ports}}' | grep -E ":$1->" | cut -f1 || true
}

resolve_port_conflicts() {
  # Reuse healthy foreign compose containers; never touch them. Fail loudly otherwise.
  local pg qdrant start_db start_qdrant holders
  pg="$(pg_port)"
  qdrant="$(qdrant_port)"
  start_db=1
  start_qdrant=1

  if [ -n "$(port_listener "$pg")" ]; then
    if pg_check; then
      holders="$(compose_holders "$pg" | tr '\n' ' ')"
      say "postgres: reusing what already serves localhost:${pg}${holders:+ (held by: $holders)} — left untouched"
      start_db=0
    else
      say "port ${pg} is held by:" >&2
      port_listener "$pg" | sed 's/^/  /' >&2
      compose_holders "$pg" | sed 's/^/  compose container: /' >&2
      die "port ${pg} is taken by another project and DATABASE_URL does not work against it. Stop that project first (docker compose stop in its directory) or point DATABASE_URL at a free port."
    fi
  fi

  if [ -n "$(port_listener "$qdrant")" ]; then
    if curl -sf "${QDRANT_URL:-http://localhost:27333}/healthz" >/dev/null 2>&1; then
      holders="$(compose_holders "$qdrant" | tr '\n' ' ')"
      say "qdrant: reusing what already serves localhost:${qdrant}${holders:+ (held by: $holders)} — left untouched"
      start_qdrant=0
    else
      say "port ${qdrant} is held by:" >&2
      port_listener "$qdrant" | sed 's/^/  /' >&2
      die "port ${qdrant} is taken by something that is not a healthy Qdrant. Stop it or point QDRANT_URL elsewhere."
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

free_app_port() {
  # Make sure $PORT is free for the app: stop OUR holders automatically, ask
  # (or require --force-port) for foreign ones, never touch protected ones.
  local listener pid cmd answer
  listener="$(port_listener "$PORT")"
  if [ -z "$listener" ]; then
    say "port $PORT: free"
    return 0
  fi
  pid="$(printf '%s\n' "$listener" | head -1 | awk '{print $2}')"
  cmd="$(ps -p "$pid" -o command= 2>/dev/null || true)"
  say "port $PORT is held by pid $pid: $cmd"
  if [ "$PORT" = "$(pg_port)" ] || [ "$PORT" = "$(qdrant_port)" ]; then
    die "port $PORT is an infrastructure port (postgres/qdrant); never killed. Choose another PORT."
  fi
  if is_protected_holder "$cmd"; then
    die "port $PORT is held by a protected process (pid $pid): $cmd — never killed. Choose another PORT."
  fi
  if printf '%s' "$cmd" | grep -q "uvicorn brain.main:app"; then
    say "port $PORT: held by our app (pid $pid) — stopping it"
    kill_tree "$pid"
  elif [ "$FORCE_PORT" -eq 1 ]; then
    say "port $PORT: killing foreign holder (pid $pid) (--force-port)"
    kill_tree "$pid"
  elif [ -t 0 ]; then
    printf 'kill pid %s (%s)? [y/N] ' "$pid" "$cmd"
    read -r answer || answer=""
    if [[ "$answer" =~ ^[yY]$ ]]; then
      kill_tree "$pid"
    else
      die "port $PORT stays busy; stop pid $pid manually or pass --force-port"
    fi
  else
    die "port $PORT is held by pid $pid ($cmd); refusing to kill non-interactively. Stop it manually or pass --force-port"
  fi
  for _ in $(seq 1 20); do
    if [ -z "$(port_listener "$PORT")" ]; then
      say "port $PORT: free"
      return 0
    fi
    sleep 0.5
  done
  die "port $PORT still busy after TERM+KILL (holder was pid $pid: $cmd)"
}

start_server() {
  mkdir -p .dev
  if [ -f "$PID_FILE" ] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
    die "already running (pid $(cat "$PID_FILE")); use scripts/dev-stop.sh first"
  fi
  if [ -n "$(port_listener "$PORT")" ]; then
    die "port $PORT is still busy (unresolved holder)"  # pragma-ish: free_app_port guarantees free
  fi
  local reload_flag=""
  [ "$RELOAD" -eq 1 ] && reload_flag="--reload"
  say "uvicorn: starting on port $PORT (log: $LOG)"
  nohup .venv/bin/uvicorn brain.main:app --port "$PORT" $reload_flag > "$LOG" 2>&1 &
  echo $! > "$PID_FILE"
}

wait_for_health() {
  local base="http://127.0.0.1:${PORT}"
  local timeout="${HEALTH_TIMEOUT_S:-30}"
  for _ in $(seq 1 "$timeout"); do
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
  # Never leave a half-started server behind.
  kill_tree "$(cat "$PID_FILE")"
  rm -f "$PID_FILE"
  tail -5 "$LOG" >&2
  die "server did not become healthy within ${timeout}s (stopped it)"
}

while [ $# -gt 0 ]; do
  case "$1" in
    --no-reload) RELOAD=0 ;;
    --reindex) REINDEX=1 ;;
    --force-port) FORCE_PORT=1 ;;
    --help) usage; exit 0 ;;
    *) usage >&2; die "unknown flag: $1" ;;
  esac
  shift
done

check_docker
check_python_env
check_env_file
resolve_compose_project >/dev/null
say "compose project: ${COMPOSE_PROJECT_NAME}"
stop_uvicorn_app "$PID_FILE" "$PORT"
free_app_port
start_infra
migrate
if [ "$REINDEX" -eq 1 ]; then
  say "reindex: rebuilding vectors"
  .venv/bin/brain reindex || die "brain reindex failed (see message above)"
fi
start_server
wait_for_health
