#!/usr/bin/env bash
# auto-deploy.sh — pull-based deploy for the Linux host (deploy/README.md).
#
# Run by second-brain-deploy.timer every few minutes. When origin/<branch>
# has moved past the checkout it:
#   1. backs up Postgres (pg_dump -Fc, 14 days kept)
#   2. fast-forwards the checkout, reinstalls the package, runs
#      `alembic upgrade head`, refreshes changed systemd units
#   3. restarts the services and waits for /ops/health (api ok)
#   4. on a failed health check rolls the CODE back to the previous sha —
#      but only when no migration file changed in the range: a schema change
#      is never undone automatically (never `alembic downgrade`); it alerts
# and reports every outcome to the Telegram allowlist.
#
# Exit codes: 0 = nothing to do / deployed / handled (rolled back, alerted);
# 1 = transient (fetch failed): the timer simply tries again.
#
# Test seams (tests never touch a real host): DEPLOY_ROOT, DEPLOY_BRANCH,
# DEPLOY_BACKUP_DIR, DEPLOY_HEALTH_URL, DEPLOY_HEALTH_TRIES,
# DEPLOY_HEALTH_SLEEP, DEPLOY_SERVICES, DEPLOY_UNIT_DIR, VENV_BIN; docker,
# systemctl and curl resolve through PATH.
set -uo pipefail

# The pull below rewrites this file; bash reads scripts incrementally, so run
# from a private copy.
if [ -z "${AUTO_DEPLOY_COPY:-}" ]; then
  copy="$(mktemp "${TMPDIR:-/tmp}/auto-deploy.XXXXXX")"
  cp "${BASH_SOURCE[0]}" "$copy"
  AUTO_DEPLOY_COPY="$copy" AUTO_DEPLOY_SELF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)" \
    exec bash "$copy" "$@"
fi
trap 'rm -f "$AUTO_DEPLOY_COPY"' EXIT

ROOT="${DEPLOY_ROOT:-$(cd "$AUTO_DEPLOY_SELF_DIR/.." && pwd -P)}"
cd "$ROOT" || exit 1
BRANCH="${DEPLOY_BRANCH:-main}"
BACKUP_DIR="${DEPLOY_BACKUP_DIR:-/var/backups/second-brain}"
HEALTH_URL="${DEPLOY_HEALTH_URL:-http://127.0.0.1:27800/ops/health}"
HEALTH_TRIES="${DEPLOY_HEALTH_TRIES:-30}"
HEALTH_SLEEP="${DEPLOY_HEALTH_SLEEP:-3}"
SERVICES="${DEPLOY_SERVICES:-second-brain-api second-brain-telegram second-brain-email}"
UNIT_DIR="${DEPLOY_UNIT_DIR:-/etc/systemd/system}"
VENV_BIN="${VENV_BIN:-$ROOT/.venv/bin}"
# A sha that failed is not retried on every tick (no deploy loop); a newer
# commit, or deleting this file, tries again.
FAILED_FILE="${DEPLOY_STATE_DIR:-$ROOT/.dev}/deploy-failed-sha"

# shellcheck source=lib.sh
# shellcheck disable=SC1091  # dynamic path: lib.sh sits next to this script
source "$ROOT/scripts/lib.sh"
load_env_file "$ROOT/.env"  # TELEGRAM_BOT_TOKEN / TELEGRAM_ALLOWED_USER_IDS for notify

log() { printf '%s auto-deploy: %s\n' "$(date '+%F %T')" "$*"; }

# Telegram notice to every allowed user. The token travels through curl's
# config on stdin, never in argv (visible in ps).
notify() {
  log "$1"
  [ -n "${TELEGRAM_BOT_TOKEN:-}" ] || return 0
  local id
  for id in ${TELEGRAM_ALLOWED_USER_IDS//,/ }; do
    printf 'url = "https://api.telegram.org/bot%s/sendMessage"\n' "$TELEGRAM_BOT_TOKEN" \
      | curl -s -m 15 -K - --data-urlencode "chat_id=$id" --data-urlencode "text=$1" \
        >/dev/null 2>&1 || true
  done
}

install_and_restart() {
  "$VENV_BIN/pip" install -q -e '.[gmail,telegram]' || return 1
  # shellcheck disable=SC2086  # SERVICES is a word list on purpose
  systemctl restart $SERVICES
}

healthy() {
  local i body
  for ((i = 1; i <= HEALTH_TRIES; i++)); do
    body="$(curl -s -m 10 "$HEALTH_URL" 2>/dev/null || true)"
    if printf '%s' "$body" | python3 -c \
      'import json,sys; sys.exit(json.load(sys.stdin)["components"]["api"]["status"] != "ok")' \
      2>/dev/null; then
      return 0
    fi
    sleep "$HEALTH_SLEEP"
  done
  return 1
}

if command -v flock >/dev/null 2>&1; then  # Linux; absent on macOS dev boxes
  exec 9>"${TMPDIR:-/tmp}/second-brain-deploy.lock"
  flock -n 9 || { log "another deploy is running"; exit 0; }
fi

git fetch -q origin "$BRANCH" || { log "git fetch failed (transient)"; exit 1; }
old="$(git rev-parse HEAD)"
new="$(git rev-parse "origin/$BRANCH")"
[ "$old" = "$new" ] && exit 0
if [ "$(cat "$FAILED_FILE" 2>/dev/null)" = "$new" ]; then
  exit 0  # already failed once and reported
fi
mark_failed() { mkdir -p "$(dirname "$FAILED_FILE")" && printf '%s\n' "$new" >"$FAILED_FILE"; }
if ! git merge-base --is-ancestor "$old" "$new"; then
  notify "⚠️ Deploy skipped: the server checkout ${old:0:7} is not behind origin/$BRANCH ${new:0:7} (local changes?). Fix by hand."
  exit 0
fi
subject="$(git log -1 --format=%s "$new")"
log "deploying ${old:0:7} -> ${new:0:7}: $subject"

mkdir -p "$BACKUP_DIR"
dump="$BACKUP_DIR/pre-${new:0:7}-$(date +%Y%m%d-%H%M).dump"
if ! docker compose exec -T db pg_dump -U brain -Fc brain >"$dump" || [ ! -s "$dump" ]; then
  rm -f "$dump"
  mark_failed
  notify "❌ Deploy of ${new:0:7} stopped: the database backup failed. Nothing changed. Retry: rm $FAILED_FILE"
  exit 0
fi
find "$BACKUP_DIR" -name 'pre-*.dump' -mtime +14 -delete 2>/dev/null || true

migrations="$(git diff --name-only "$old" "$new" -- src/brain/db/migrations/versions)"
units="$(git diff --name-only "$old" "$new" -- deploy/systemd)"

git merge -q --ff-only "$new" || {
  mark_failed
  notify "❌ Deploy of ${new:0:7} failed: git could not fast-forward. Nothing changed."
  exit 0
}
if [ -n "$units" ]; then
  cp deploy/systemd/second-brain-*.service deploy/systemd/second-brain-*.timer "$UNIT_DIR"/ 2>/dev/null
  systemctl daemon-reload
fi

if "$VENV_BIN/pip" install -q -e '.[gmail,telegram]' && "$VENV_BIN/alembic" upgrade head \
  && { # shellcheck disable=SC2086
       systemctl restart $SERVICES; } && healthy; then
  rm -f "$FAILED_FILE"
  notify "✅ Deployed ${new:0:7}: $subject"
  exit 0
fi

if [ -n "$migrations" ]; then
  notify "❌ Deploy of ${new:0:7} is unhealthy and it changed the database schema, so I did NOT roll back. Backup: $dump. Check: journalctl -u second-brain-api"
  exit 0
fi
mark_failed
log "rolling back to ${old:0:7}"
git reset -q --hard "$old"
if install_and_restart && healthy; then
  notify "↩️ Deploy of ${new:0:7} failed the health check; rolled back to ${old:0:7}. ($subject)"
else
  notify "❌ Deploy of ${new:0:7} failed AND the rollback to ${old:0:7} is unhealthy. Check the server: journalctl -u second-brain-api"
fi
exit 0
