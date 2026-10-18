#!/usr/bin/env bash
# shellcheck shell=bash disable=SC1091
# Seed (or re-seed) the 8 synthetic demo items via the running app's API.
# Idempotent: an item is skipped when an item with the same raw_text exists.
# Refuses to do anything when the app is not healthy. Never talks to the DB
# directly — whatever DATABASE_URL the app uses is what gets seeded.
set -u

say() { printf '%s\n' "$*"; }
die() { printf 'seed-demo: %s\n' "$*" >&2; exit 1; }

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib.sh
source "$SCRIPT_DIR/lib.sh"

PORT="${PORT:-8000}"
BASE="http://127.0.0.1:${PORT}"

say "app: checking $BASE/health"
if ! curl --max-time 5 -sf "$BASE/health" >/dev/null 2>&1; then
  die "app is not healthy on :$PORT — start it first (scripts/dev-start.sh)"
fi

seed_one() {
  # $1 = raw_text; skips when an item with identical raw_text already exists.
  local raw_text="$1" escaped
  escaped="${raw_text//\\/\\\\}"
  escaped="${escaped//\"/\\\"}"
  if curl --max-time 5 -s "$BASE/items?limit=100" | grep -qF "$raw_text"; then
    say "item: already present — skipped"
    return 0
  fi
  if ! curl --max-time 5 -sf -X POST "$BASE/items" -H 'Content-Type: application/json' \
      -d "{\"raw_text\": \"$escaped\"}" >/dev/null; then
    say "item: FAILED to create: $raw_text"
    return 1
  fi
  say "item: created"
}

say "seeding demo items (idempotent) via $BASE"
seed_one "Renew the TLS certificate before Friday"
seed_one "Dentist appointment Tuesday 10:00"
seed_one "Ideas for the offsite agenda: hacking time, roadmap, team rituals"
seed_one "Buy oat milk and espresso beans"
seed_one "Follow up with vendor about the data-processing agreement"
seed_one "Weekly review: check open loops, calendar, and project list"
seed_one "Draft the quarterly OKRs draft for the platform team"
seed_one "Read the paper on retrieval-augmented generation and take notes"

total="$(curl --max-time 5 -s "$BASE/items?limit=100" | grep -o '"id"' | wc -l | tr -d ' ')"
say "done: $total item(s) now on the app"
