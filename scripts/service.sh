#!/usr/bin/env bash
# service.sh — install/uninstall/status/restart/logs for the launchd agents.
#
# Installs per-user LaunchAgents that keep the stack up across logins:
#   com.secondbrain.api       scripts/run-server.sh   (foreground uvicorn)
#   com.secondbrain.telegram  scripts/run-bot.sh      (long-poll bot; exit-code
#                             contract: 0/78 -> no restart, 1 -> restart)
#   com.secondbrain.email     scripts/run-email.sh    (Gmail label poller; same
#                             contract; needs re-auth pauses INSIDE the poller)
#   com.secondbrain.whatsapp  scripts/run-whatsapp.sh (WhatsApp poller)
#
# All agents get COMPOSE_PROJECT_NAME=second-brain so compose-scoped checks
# (the cockpit docker panel) always look at the service project, never at the
# dev one (second-brain-dev). Secrets stay OUT of the plists: the app reads
# .env itself via Settings(env_file=".env") with WorkingDirectory at the repo
# root.
#
# install TAKES OVER from manual processes first: it stops our dev uvicorn
# (pid file + orphans, per lib.sh ownership rules), our nohup telegram bot
# (.dev/telegram.pid + orphans; a second getUpdates poller means 409 for
# both) and any manual WhatsApp poller (.dev/whatsapp.pid + orphans; a
# second poller races the shared cursor table) BEFORE bootstrapping.
#
# Usage: scripts/service.sh install|uninstall|status|restart|logs [api|telegram|email|connectors|whatsapp]
#
# Test seams (never needed interactively): PLIST_DIR, LOG_DIR, PORT, LINES,
# BRAIN_BIN, UVICORN_BIN — overridable so tests never touch the real
# LaunchAgents, the real docker, or the user's Telegram.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
cd "$ROOT"
# shellcheck source=lib.sh
# shellcheck disable=SC1091  # dynamic path: lib.sh sits next to this script
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

LABEL_API="com.secondbrain.api"
LABEL_TG="com.secondbrain.telegram"
LABEL_EMAIL="com.secondbrain.email"
LABEL_CONNECTORS="com.secondbrain.connectors"
LABEL_WA="com.secondbrain.whatsapp"
PLIST_DIR="${PLIST_DIR:-$HOME/Library/LaunchAgents}"
LOG_DIR="${LOG_DIR:-$HOME/Library/Logs/second-brain}"
PORT="${PORT:-27800}"
BRAIN="${BRAIN_BIN:-$ROOT/.venv/bin/brain}"
UVICORN="${UVICORN_BIN:-$ROOT/.venv/bin/uvicorn}"
RUN_SERVER="$ROOT/scripts/run-server.sh"
RUN_BOT="$ROOT/scripts/run-bot.sh"
RUN_EMAIL="$ROOT/scripts/run-email.sh"
RUN_CONNECTORS="$ROOT/scripts/run-connectors.sh"
RUN_WHATSAPP="$ROOT/scripts/run-whatsapp.sh"
TG_PID_FILE="$ROOT/.dev/telegram.pid"
WA_PID_FILE="$ROOT/.dev/whatsapp.pid"
GUI_DOMAIN="gui/$(id -u)"
# PATH for the service environment: brew (both arches), Rancher Desktop's CLI
# shims (docker), then the system. The cockpit docker check needs `docker`.
SERVICE_PATH="/opt/homebrew/bin:$HOME/.rd/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"

email_configured() {
  # Email is OPT-IN: the agent is installed only when a Gmail label id is
  # configured (real environment wins over .env). Without it the poller
  # would start, exit 78->0 and stay down — reading as a permanently-dead
  # background service in the cockpit. Install skips it instead.
  load_env_file
  [ -n "${GMAIL_LABEL_ID:-}" ]
}

connectors_configured() {
  # Connectors are OPT-IN the same way: the agent runs only when
  # BRAIN_CONNECTORS contains a non-email connector (email keeps its own
  # agent, run-email.sh). Without one the scheduler would loop doing
  # nothing — install skips it instead and removes any stale plist.
  load_env_file
  [ -n "${BRAIN_CONNECTORS:-}" ] || return 1
  local part
  local old_ifs="$IFS"
  IFS=','
  for part in ${BRAIN_CONNECTORS}; do
    part="$(printf '%s' "$part" | tr -d '[:space:]')"
    if [ -n "$part" ] && [ "$part" != "email" ]; then
      IFS="$old_ifs"
      return 0
    fi
  done
  IFS="$old_ifs"
  return 1
}

usage() {
  cat <<EOF
Usage: scripts/service.sh install|uninstall|status|restart|logs [api|telegram|email|connectors|whatsapp]

install    stop our manual instances, write the plists, bootstrap them
           (email is opt-in: only installed when GMAIL_LABEL_ID is set;
           connectors is opt-in: only installed when BRAIN_CONNECTORS has a
           non-email connector)
uninstall  bootout all agents and remove the plists (logs are kept)
status     per-agent state/pid/last-exit, plus sleep + docker notes
restart    kickstart -k all installed agents (skips the rest)
logs [c]   tail the last ${LINES:-50} lines (component: api, telegram,
           email, connectors, whatsapp, or all)

Agents: ${LABEL_API}, ${LABEL_TG}, ${LABEL_EMAIL}, ${LABEL_CONNECTORS}, ${LABEL_WA}
Plists: ${PLIST_DIR}   Logs: ${LOG_DIR}
EOF
}

die() { printf 'service: %s\n' "$*" >&2; exit 1; }

# --- takeover: manual processes must die before launchd starts its own -------

is_our_bot() {
  # Same ownership proof as is_our_instance: command matches AND the process
  # cwd is this repo. Undeterminable cwd => NOT ours (conservative).
  local pid="$1" cmd cwd
  cmd="$(ps -p "$pid" -o command= 2>/dev/null || true)"
  printf '%s' "$cmd" | grep -q "brain telegram" || return 1
  cwd="$(process_cwd "$pid")"
  [ -n "$cwd" ] && [ "$cwd" = "$LIB_ROOT" ]
}

is_our_poller() {
  # Same ownership proof for a manual WhatsApp poller ('brain whatsapp').
  local pid="$1" cmd cwd
  cmd="$(ps -p "$pid" -o command= 2>/dev/null || true)"
  printf '%s' "$cmd" | grep -q "brain whatsapp" || return 1
  cwd="$(process_cwd "$pid")"
  [ -n "$cwd" ] && [ "$cwd" = "$LIB_ROOT" ]
}

stop_whatsapp_poller() {
  # Stop the pid-file poller and any orphan of ours — never other clones'
  # pollers or unrelated 'whatsapp' strings. Two pollers share one cursor
  # table and could race the same overlap window into duplicate items.
  local pid_file="$1" pid cmd orphan
  if [ -f "$pid_file" ]; then
    pid="$(cat "$pid_file")"
    if is_our_poller "$pid"; then
      say "takeover: stopping manual whatsapp poller (pid $pid)"
      kill_tree "$pid"
      wait_gone "$pid" || say "warning: poller pid $pid survived TERM+KILL"
    else
      cmd="$(ps -p "$pid" -o command= 2>/dev/null || true)"
      say "takeover: whatsapp pid file stale (pid ${pid:-?} is not our poller: ${cmd:-<gone>}); ignoring"
    fi
    rm -f "$pid_file"
  fi
  for orphan in $(pgrep -f "brain whatsapp" || true); do
    [ "$orphan" = "$$" ] && continue
    is_our_poller "$orphan" || continue
    say "takeover: stopping orphan whatsapp poller (pid $orphan)"
    kill_tree "$orphan"
    wait_gone "$orphan" || say "warning: orphan poller pid $orphan survived TERM+KILL"
  done
}

stop_telegram_bot() {
  # Stop the pid-file bot (nohup ... & echo $! > .dev/telegram.pid) and any
  # orphan of ours — never other clones' bots or unrelated "telegram" strings.
  local pid_file="$1" pid cmd orphan
  if [ -f "$pid_file" ]; then
    pid="$(cat "$pid_file")"
    if is_our_bot "$pid"; then
      say "takeover: stopping manual bot (pid $pid)"
      kill_tree "$pid"
      wait_gone "$pid" || say "warning: bot pid $pid survived TERM+KILL"
    else
      cmd="$(ps -p "$pid" -o command= 2>/dev/null || true)"
      say "takeover: bot pid file stale (pid ${pid:-?} is not our bot: ${cmd:-<gone>}); ignoring"
    fi
    rm -f "$pid_file"
  fi
  for orphan in $(pgrep -f "brain telegram" || true); do
    [ "$orphan" = "$$" ] && continue
    is_our_bot "$orphan" || continue
    say "takeover: stopping orphan bot (pid $orphan)"
    kill_tree "$orphan"
    wait_gone "$orphan" || say "warning: orphan bot pid $orphan survived TERM+KILL"
  done
}

takeover() {
  # Stop every manual instance of ours so the launchd bot is the ONLY
  # getUpdates poller (a second one means 409 Conflict for both) and the
  # launchd poller the ONLY WhatsApp poller (a second one races the cursor).
  stop_telegram_bot "$TG_PID_FILE"
  stop_uvicorn_app "$ROOT/.dev/uvicorn.pid" "$PORT"
  stop_whatsapp_poller "$WA_PID_FILE"
}

# --- environment checks (never fatal; services survive both) -----------------

docker_check() {
  if command -v docker >/dev/null 2>&1 && docker info >/dev/null 2>&1; then
    say "docker: reachable (containers restart: unless-stopped come back at login)"
  else
    say "docker: NOT reachable — set the docker app to start at login; the API will report postgres/qdrant down until it is" >&2
  fi
}

sleep_check() {
  # launchd jobs stall while the Mac sleeps. SleepDisabled=1 or sleep=0
  # (pmset "never") both mean the Mac never sleeps -> OK.
  local out
  out="$(pmset -g 2>/dev/null || true)"
  if printf '%s' "$out" | grep -Eq '^[[:space:]]*SleepDisabled[[:space:]]+1'; then
    say "sleep: OK (SleepDisabled=1 — the Mac never sleeps)"
  elif printf '%s' "$out" | grep -Eq '^[[:space:]]*sleep[[:space:]]+0([[:space:]]|\()'; then
    say "sleep: OK (sleep timer 0 — the Mac never sleeps)"
  else
    say "sleep: WARNING — the Mac can sleep; launchd services stall while it does" >&2
  fi
}

# --- plist generation ---------------------------------------------------------

write_plist() {
  # $1 = label, $2 = program (absolute), $3 = log base name, $4... = args.
  # Optional THROTTLE (seconds) overrides the default ThrottleInterval.
  local label="$1" program="$2" logbase="$3" throttle="${THROTTLE:-10}"
  shift 3
  local args="" a
  for a in "$@"; do
    args+="        <string>${a}</string>"$'\n'
  done
  mkdir -p "$PLIST_DIR" "$LOG_DIR"
  cat > "$PLIST_DIR/${label}.plist" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>${label}</string>
    <key>ProgramArguments</key>
    <array>
        <string>${program}</string>
${args}    </array>
    <key>WorkingDirectory</key>
    <string>${LIB_ROOT}</string>
    <key>EnvironmentVariables</key>
    <dict>
        <key>COMPOSE_PROJECT_NAME</key>
        <string>second-brain</string>
        <key>PATH</key>
        <string>${SERVICE_PATH}</string>
    </dict>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <dict>
        <key>SuccessfulExit</key>
        <false/>
    </dict>
    <key>ThrottleInterval</key>
    <integer>${throttle}</integer>
    <key>StandardOutPath</key>
    <string>${LOG_DIR}/${logbase}.log</string>
    <key>StandardErrorPath</key>
    <string>${LOG_DIR}/${logbase}.err.log</string>
</dict>
</plist>
EOF
}

bootout_label() {
  launchctl bootout "${GUI_DOMAIN}/$1" 2>/dev/null || true
  # Wait (bounded) for launchd to actually drop the label: bootstrapping a
  # label launchd still holds is the classic '5: Input/output error' race.
  local i=0
  while [ "$i" -lt "${BOOTOUT_WAIT_TRIES:-10}" ]; do
    launchctl print "${GUI_DOMAIN}/$1" >/dev/null 2>&1 || return 0
    sleep 0.5
    i=$((i + 1))
  done
}

bootstrap_label() {
  # launchctl bootstrap races a just-finished bootout ('5: Input/output
  # error'); retry ONLY that error, up to 3 tries, then report failure.
  local label="$1" attempt=1 out
  while :; do
    if out="$(launchctl bootstrap "$GUI_DOMAIN" "$PLIST_DIR/${label}.plist" 2>&1)"; then
      printf '  %-28s bootstrapped\n' "$label"
      return 0
    fi
    if [ "$attempt" -ge 3 ] || ! printf '%s' "$out" | grep -q "Input/output error"; then
      printf '  %-28s FAILED: %s\n' "$label" "$out" >&2
      return 1
    fi
    sleep 1.5
    attempt=$((attempt + 1))
  done
}

install() {
  [ -x "$BRAIN" ] || die "$BRAIN missing — provision the venv first (scripts/dev-start.sh does)"
  [ -x "$UVICORN" ] || die "$UVICORN missing — provision the venv first"
  [ -x "$RUN_SERVER" ] && [ -x "$RUN_BOT" ] && [ -x "$RUN_EMAIL" ] \
    && [ -x "$RUN_CONNECTORS" ] && [ -x "$RUN_WHATSAPP" ] || \
    die "scripts/run-server.sh, run-bot.sh, run-email.sh, run-connectors.sh and run-whatsapp.sh must be executable"
  [ -f "$ROOT/.env" ] || say "env: WARNING — no .env at $ROOT; services run on defaults (telegram disabled until a token exists)"

  say "takeover: stopping manual instances (no duplicate bots, no 409)"
  takeover

  docker_check
  sleep_check

  say "install: writing plists to $PLIST_DIR"
  local install_email=0
  if email_configured; then
    install_email=1
  fi
  local install_connectors=0
  if connectors_configured; then
    install_connectors=1
  fi
  write_plist "$LABEL_API" "$RUN_SERVER" "api"
  write_plist "$LABEL_TG" "$RUN_BOT" "telegram"
  if [ "$install_email" -eq 1 ]; then
    write_plist "$LABEL_EMAIL" "$RUN_EMAIL" "email"
  else
    # Opt-out must leave no stale agent: an old plist would keep running (or
    # bootstrap and exit 78->0) — a permanently-down background service.
    bootout_label "$LABEL_EMAIL"
    rm -f "$PLIST_DIR/${LABEL_EMAIL}.plist"
    say "email: skipped (GMAIL_LABEL_ID not set)"
  fi
  if [ "$install_connectors" -eq 1 ]; then
    write_plist "$LABEL_CONNECTORS" "$RUN_CONNECTORS" "connectors"
  else
    # Same opt-out hygiene as email: no stale agent looping on nothing.
    bootout_label "$LABEL_CONNECTORS"
    rm -f "$PLIST_DIR/${LABEL_CONNECTORS}.plist"
    say "connectors: skipped (no non-email connector in BRAIN_CONNECTORS)"
  fi
  THROTTLE=30 write_plist "$LABEL_WA" "$RUN_WHATSAPP" "whatsapp"

  # Pre-create the log files mode 600: launchd writes into the existing inode
  # (preserving the mode) instead of creating them with the default umask.
  for f in api api.err telegram telegram.err whatsapp whatsapp.err; do
    touch "$LOG_DIR/$f.log" 2>/dev/null || true
  done
  if [ "$install_email" -eq 1 ]; then
    for f in email email.err; do
      touch "$LOG_DIR/$f.log" 2>/dev/null || true
    done
  fi
  if [ "$install_connectors" -eq 1 ]; then
    for f in connectors connectors.err; do
      touch "$LOG_DIR/$f.log" 2>/dev/null || true
    done
  fi
  chmod 600 "$LOG_DIR"/*.log 2>/dev/null || true

  say "install: bootstrapping agents"
  bootout_label "$LABEL_API"
  bootout_label "$LABEL_TG"
  if [ "$install_email" -eq 1 ]; then
    bootout_label "$LABEL_EMAIL"
  fi
  if [ "$install_connectors" -eq 1 ]; then
    bootout_label "$LABEL_CONNECTORS"
  fi
  bootout_label "$LABEL_WA"
  local failed=0
  bootstrap_label "$LABEL_API" || failed=1
  bootstrap_label "$LABEL_TG" || failed=1
  if [ "$install_email" -eq 1 ]; then
    bootstrap_label "$LABEL_EMAIL" || failed=1
  fi
  if [ "$install_connectors" -eq 1 ]; then
    bootstrap_label "$LABEL_CONNECTORS" || failed=1
  fi
  bootstrap_label "$LABEL_WA" || failed=1
  if [ "$failed" -ne 0 ]; then
    die "install: one or more agents failed to bootstrap (see above); re-run scripts/service.sh install"
  fi

  sleep 2
  say "install: done — agents are starting (RunAtLoad)"
  say "  logs:     $LOG_DIR/"
  say "  status:   scripts/service.sh status"
  say "  uninstall: scripts/service.sh uninstall"
}

uninstall() {
  say "uninstall: booting out agents (their processes get SIGTERM from launchd)"
  bootout_label "$LABEL_API"
  bootout_label "$LABEL_TG"
  bootout_label "$LABEL_EMAIL"
  bootout_label "$LABEL_CONNECTORS"
  bootout_label "$LABEL_WA"
  rm -f "$PLIST_DIR/${LABEL_API}.plist" "$PLIST_DIR/${LABEL_TG}.plist" \
    "$PLIST_DIR/${LABEL_EMAIL}.plist" "$PLIST_DIR/${LABEL_CONNECTORS}.plist" \
    "$PLIST_DIR/${LABEL_WA}.plist"
  say "uninstall: plists removed; logs remain in $LOG_DIR"
}

print_label_status() {
  # $1 = label; echoes "state=<..> pid=<..> last_exit=<..>" (empty if absent).
  local out state pid last_exit
  out="$(launchctl print "${GUI_DOMAIN}/$1" 2>/dev/null || true)"
  if [ -z "$out" ]; then
    printf '  %-28s not installed\n' "$1"
    return 1
  fi
  state="$(printf '%s\n' "$out" | sed -n 's/^[[:space:]]*state = //p' | head -1)"
  pid="$(printf '%s\n' "$out" | sed -n 's/^[[:space:]]*pid = //p' | head -1)"
  last_exit="$(printf '%s\n' "$out" | sed -n 's/^[[:space:]]*last exit code = //p' | head -1)"
  printf '  %-28s state=%s pid=%s last_exit=%s\n' "$1" "${state:-?}" "${pid:--}" "${last_exit:--}"
  [ "$state" = "running" ]
}

status() {
  say "launchd agents (${GUI_DOMAIN}):"
  local ok=0
  print_label_status "$LABEL_API" || ok=1
  print_label_status "$LABEL_TG" || ok=1
  if email_configured; then
    print_label_status "$LABEL_EMAIL" || ok=1
  else
    printf '  %-28s skipped (GMAIL_LABEL_ID not set)\n' "$LABEL_EMAIL"
  fi
  if connectors_configured; then
    print_label_status "$LABEL_CONNECTORS" || ok=1
  else
    printf '  %-28s skipped (no non-email connector in BRAIN_CONNECTORS)\n' "$LABEL_CONNECTORS"
  fi
  print_label_status "$LABEL_WA" || ok=1
  sleep_check
  docker_check
  if [ "$ok" -eq 0 ]; then
    say "status: all agents running"
  else
    say "status: NOT all agents running" >&2
  fi
  return "$ok"
}

restart() {
  say "restart: kickstart -k installed agents (labels with no plist are skipped)"
  local failed=0 label
  for label in "$LABEL_API" "$LABEL_TG" "$LABEL_EMAIL" "$LABEL_CONNECTORS" "$LABEL_WA"; do
    if [ ! -f "$PLIST_DIR/${label}.plist" ]; then
      printf '  %-28s skipped (not installed)\n' "$label"
      continue
    fi
    if launchctl kickstart -k "${GUI_DOMAIN}/${label}" 2>/dev/null; then
      printf '  %-28s kicked\n' "$label"
    else
      printf '  %-28s FAILED\n' "$label" >&2
      failed=1
    fi
  done
  return "$failed"
}

logs() {
  local comp="${1:-all}" LINES="${LINES:-50}"
  case "$comp" in
    api) tail_log "api" ;;
    telegram) tail_log "telegram" ;;
    email) tail_log "email" ;;
    connectors) tail_log "connectors" ;;
    whatsapp) tail_log "whatsapp" ;;
    all) tail_log "api"; tail_log "telegram"; tail_log "email"; tail_log "connectors"; tail_log "whatsapp" ;;
    *) usage >&2; die "unknown component '$comp' (api|telegram|email|connectors|whatsapp|all)" ;;
  esac
}

tail_log() {
  local base="$LOG_DIR/$1" f
  say "── $1 (last ${LINES} lines) ──"
  for f in "$base.log" "$base.err.log"; do
    if [ -f "$f" ]; then
      tail -n "$LINES" "$f"
    else
      printf '  (%s: no log yet)\n' "$f"
    fi
  done
}

case "${1:-}" in
  install) install ;;
  uninstall) uninstall ;;
  status) status ;;
  restart) restart ;;
  logs) shift; logs "${1:-all}" ;;
  --help|-h) usage; exit 0 ;;
  *) usage >&2; die "unknown command '${1:-}' (install|uninstall|status|restart|logs)" ;;
esac
