# shellcheck shell=bash
# scripts/lib.sh — shared env/compose-project/uvicorn-lifecycle helpers for
# dev-start.sh and dev-stop.sh, so both always agree on which compose project
# they talk to and how the app process is stopped.

LIB_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# User-facing log line: plain stdout. Defined HERE so lib.sh is self-sufficient
# when sourced alone (tests/tools source it in a bare `bash -c`): an undefined
# `say` falls through to macOS /usr/bin/say and SPEAKS the message aloud.
say() { printf '%s\n' "$*"; }

# Resolve the docker CLI (falls back to the Rancher Desktop context).
resolve_docker() {
  DOCKER=(docker)
  if ! docker info >/dev/null 2>&1; then
    if docker --context rancher-desktop info >/dev/null 2>&1; then
      # DOCKER is consumed by the sourcing script.
      # shellcheck disable=SC2034
      DOCKER=(docker --context rancher-desktop)
      say "docker: daemon reachable via context 'rancher-desktop' (export DOCKER_CONTEXT=rancher-desktop to silence)"
    else
      return 1
    fi
  fi
}

# Export .env vars into the environment without printing values; real
# environment wins (never overridden here). Differences from dotenv tools:
# only plain KEY=VALUE lines, surrounding quotes stripped, inline '#' comments
# and trailing whitespace are NOT stripped — keep values comment-free.
load_env_file() {
  [ -f .env ] || return 0
  local key value
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

# Pin the compose project: .env may set COMPOSE_PROJECT_NAME, the real
# environment beats .env, default otherwise. Both scripts MUST go through
# this so start/stop never disagree (a mismatched 'down -v' deletes the
# wrong project's volumes).
resolve_compose_project() {
  load_env_file
  : "${COMPOSE_PROJECT_NAME:=second-brain-dev}"
  export COMPOSE_PROJECT_NAME
  printf '%s' "$COMPOSE_PROJECT_NAME"
}

port_listener() {
  lsof -nP -iTCP:"$1" -sTCP:LISTEN 2>/dev/null | tail -n +2 || true
}

is_our_server_cmd() {
  # $1 = command line; true when it is the app's uvicorn (any pid).
  printf '%s' "$1" | grep -q "uvicorn brain.main:app"
}

process_cwd() {
  # Working directory of pid $1 (empty when it cannot be determined).
  lsof -a -p "$1" -d cwd -Fn 2>/dev/null | sed -n 's/^n//p' | head -1
}

is_our_instance() {
  # $1 = pid; true ONLY for a uvicorn of THIS repo clone: command matches AND
  # the process cwd is this repo. Command matching alone (even with a port)
  # would kill other clones' or the user's instances — the cwd is the
  # ownership proof. Undeterminable cwd => NOT ours (conservative).
  local pid="$1" cmd cwd
  cmd="$(ps -p "$pid" -o command= 2>/dev/null || true)"
  is_our_server_cmd "$cmd" || return 1
  cwd="$(process_cwd "$pid")"
  [ -n "$cwd" ] && [ "$cwd" = "$LIB_ROOT" ]
}

is_protected_holder() {
  # $1 = command line; true for infra proxies we must never kill
  # (docker/vpnkit/rancher/colima port forwarders). Case-insensitive.
  printf '%s' "$1" | tr '[:upper:]' '[:lower:]' | grep -qE 'docker|vpnkit|rancher|colima'
}

wait_gone() {
  # True once pid $1 no longer exists (polls up to ~10s).
  local pid="$1" _
  for _ in $(seq 1 20); do
    kill -0 "$pid" 2>/dev/null || return 0
    sleep 0.5
  done
  return 1
}

# Stop the dev uvicorn: the pid-file instance and any orphan of ours on $2
# without a valid pid file. A pid counts as ours ONLY per is_our_instance
# (command match AND cwd == this repo) — other clones' or user-started
# instances are never killed. Never touches containers or volumes.
# $1 = pid file path, $2 = app port.
stop_uvicorn_app() {
  local pid_file="$1" port="$2" pid cmd orphan
  if [ -f "$pid_file" ]; then
    pid="$(cat "$pid_file")"
    if is_our_instance "$pid"; then
      say "stopping previous instance (pid $pid)"
      kill_tree "$pid"
      wait_gone "$pid" || say "warning: pid $pid survived TERM+KILL"
      rm -f "$pid_file"
    else
      cmd="$(ps -p "$pid" -o command= 2>/dev/null || true)"
      say "pid file stale (pid ${pid:-?} is not our server: ${cmd:-<gone>}); ignoring"
      rm -f "$pid_file"
    fi
  fi
  for orphan in $(pgrep -f "uvicorn brain.main:app --port $port( |$)" || true); do
    [ "$orphan" = "$$" ] && continue
    is_our_instance "$orphan" || continue
    say "stopping orphan instance (pid $orphan)"
    kill_tree "$orphan"
    wait_gone "$orphan" || say "warning: orphan pid $orphan survived TERM+KILL"
  done
}

# TERM the whole tree (children first — uvicorn --reload runs a child worker),
# then the pid itself; escalate to SIGKILL if anything survives.
kill_tree() {
  local pid="$1"
  pkill -TERM -P "$pid" 2>/dev/null || true
  kill -TERM "$pid" 2>/dev/null || true
  local _
  for _ in $(seq 1 20); do
    kill -0 "$pid" 2>/dev/null || return 0
    sleep 0.5
  done
  pkill -KILL -P "$pid" 2>/dev/null || true
  kill -KILL "$pid" 2>/dev/null || true
}

pg_url() {
  # DATABASE_URL is a SQLAlchemy URL (postgresql+psycopg://...); psycopg wants plain postgresql://
  local url="${DATABASE_URL:-postgresql+psycopg://brain:brain@localhost:5432/brain}"
  echo "${url//+psycopg/}"
}

pg_port() {
  local url="${DATABASE_URL:-postgresql+psycopg://brain:brain@localhost:5432/brain}"
  local port="${url##*:}"
  port="${port%%/*}"
  [[ "$port" =~ ^[0-9]+$ ]] && echo "$port" || echo 5432
}

qdrant_port() {
  local url="${QDRANT_URL:-http://localhost:6333}"
  local port="${url##*:}"
  port="${port%%/*}"
  [[ "$port" =~ ^[0-9]+$ ]] && echo "$port" || echo 6333
}
