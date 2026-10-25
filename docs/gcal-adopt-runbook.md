# Calendar adopt runbook (gcal, one-time)

For the user, running on the live install. Goal: point the idempotency
ledger at the already-captured pasted calendar one-liners so the **first
real gcal sync does not duplicate them**. Read this once, run two
commands, check three things.

## Before you start

- gcal has **no cursor yet** on the live DB (verified 25 Oct). Adopt runs
  **before the first sync only** — a sync would create a cursor and adopt
  then refuses. Do not run `connectors sync gcal` before adopting.
- The deploy must include main **2ddd581 or later** (it carries the sync
  window fix #114 and the OAuth CLI fix #108).
- Google consent must already exist (it does — the 25 Oct dry-run fetched
  with it). If it ever complains with exit 78 / auth error:
  `brain connectors auth google --client-secret <path-to-desktop-secret.json>`
  (the JSON stays outside the repo; only the refresh token goes to the
  keychain).

## The window (why 10,644 became a few hundred)

`BRAIN_GCAL_PAST_DAYS` (default **7**) and `BRAIN_GCAL_FUTURE_DAYS`
(default **60**) bound every fetch: a fresh walk delivers instances from
`now − PAST` to the date-quantized edge `now + FUTURE`, and nothing
beyond. The first **real** adopt/sync freezes the past cutoff into the
cursor permanently — choose it deliberately:

- Set `BRAIN_GCAL_PAST_DAYS` to the SMALLEST value that covers the oldest
  pasted event you still care about. The pastes are from October, so ~30
  is the starting point. Do not reach for a big number "just in case":
  the past cutoff is frozen forever at the first REAL run, and a year of
  past instances of every weekly/daily series can be thousands of records
  fetched on every re-list.
- Dry-runs persist nothing, so the env is free to experiment with.
  The **real** run locks it in. After that, changing it has no effect on
  the frozen cutoff (new cursors only).
- Before the real run, look at the dry-run's **fetched count**: if it is
  in the thousands, lower `BRAIN_GCAL_PAST_DAYS` until it is sane, then
  re-run the dry-run.

## Step 1 — dry-run (writes NOTHING)

```bash
cd /Users/yos.i/git/Second-brain   # the live install
export BRAIN_ALLOW_LIVE=1          # sanctioned live-store opt-in
export BRAIN_CONNECTORS=gcal       # the connector must be enabled
export QDRANT_COLLECTION=brain_items  # every live CLI command needs the live collection
export BRAIN_GCAL_PAST_DAYS=30     # smallest value covering the oldest pasted event
# BRAIN_GCAL_FUTURE_DAYS defaults to 60
.venv/bin/brain connectors adopt gcal --dry-run
```

**EXAMPLE OUTPUT — not your data** (shape only, from a fakes-only
rehearsal; your numbers will differ):

```
adopt gcal: 9 record(s) fetched, 9 would adopt, 0 ambiguous,
0 new on first sync, 0 user-deleted, 0 already adopted
  WOULD ADOPT (9)
    <event-id> (v="…") -> item <id> [B]
    …
```

**How to read it:**

- Only pastes stored with `source=calendar` are candidates at all.
  Announcement-style pastes (prose like "אוסף בשער ב-8:45") were captured
  as **notes** and never enter the pool — they can NOT adopt, by design
  (an exact-text match against rendered event one-liners can never hit
  prose, and a fuzzy matcher would risk pointing the ledger at the wrong
  item).
- `WOULD ADOPT [B]` = literal Google-web-UI copies whose stored one-liner
  matches the adapter's rendering of the same event (title + local time +
  minute + optional `Location:`/`מקום:` line — the label fix).
- Events older than your `BRAIN_GCAL_PAST_DAYS` are not fetched at all —
  they can't adopt in this run. Re-run the dry-run with a wider
  `BRAIN_GCAL_PAST_DAYS` if the counts look short.
- Which of your pasted events fall in which bucket is EXACTLY what this
  report tells you — compare its counts against your own list before
  running for real.

## Step 2 — the real run (same command, no `--dry-run`)

```bash
export BRAIN_ALLOW_LIVE=1 BRAIN_CONNECTORS=gcal QDRANT_COLLECTION=brain_items
export BRAIN_GCAL_PAST_DAYS=30   # whatever your checked dry-run used — frozen from here on
.venv/bin/brain connectors adopt gcal
```

Same fetch, same report, plus: ledger rows written, one `ops_events` entry,
`brain connectors list` now shows a **cursor present** for gcal. Exit 0.

If anything looks wrong before a sync has run:
`brain connectors adopt gcal --undo` removes the ledger rows it wrote
(it refuses rows a sync has since touched or that the user deleted
through — it never destroys those).

## Step 3 — first sync, then check

```bash
export BRAIN_ALLOW_LIVE=1 BRAIN_CONNECTORS=gcal QDRANT_COLLECTION=brain_items
export BRAIN_GCAL_PAST_DAYS=30
.venv/bin/brain connectors sync gcal
```

NOTE: nothing syncs gcal on a schedule yet — the connectors launchd agent
is not installed, and installing it is a separate decision. Until then,
gcal only syncs when you run the command above by hand.

Check:

1. **No duplicates**: for every `WOULD ADOPT` line, the item count for
   that event stays 1 (the sync upserts the adopted item in place).
2. `brain connectors list`: gcal `last ok` updated, cursor present.
3. Future coverage: instances inside the rolling 60-day future window
   arrive on sync; something scheduled beyond it appears at the first
   daily re-list after it enters the window (nothing is silently lost —
   the window re-lists when its edge advances).

## Announcement-style pastes (source=note)

Announcement-derived captures are **not calendar candidates and never
will be** — that is the agreed design, not a failure. Which of your
pastes these are shows up as the gap between your pasted list and the
dry-run's candidate count. Options: leave them as notes (recommended;
once memory-layer extraction goes live, reconciliation can propose
supersedes), or delete them yourself. Nothing adopt-side will touch
them.

## Failure modes

| Symptom | Meaning | Action |
| --- | --- | --- |
| exit 78, "not enabled" | `BRAIN_CONNECTORS` lacks gcal | add it (see Step 1) |
| exit 78, auth/consent | no Google refresh token | `connectors auth google` (above) |
| "cursor already exists" | a sync ran before adopt | adopt is no longer possible for this ledger; talk to the manager before any manual DB change |
| fail-fast DB-stamp error (pytest only) | n/a — adopt is not pytest | n/a |
| report shows `AMBIGUOUS` | 2+ items match one record exactly | adopt skips them; resolve the duplicate first, re-run |
