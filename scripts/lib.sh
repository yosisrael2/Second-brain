# shellcheck shell=bash
# scripts/lib.sh — shared env + compose-project resolution for dev scripts.
# Sourced (not executed) by dev-start.sh and dev-stop.sh so both always agree
# on which compose project they talk to.

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
