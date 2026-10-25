# Enabling connectors on live (per-source runbook)

This is the go-live runbook per connector. One-time Google Cloud and consent
setup lives in [connectors-setup.md](connectors-setup.md) — do that first.
This document assumes the setup is DONE and the stack is deployed.

Current state on main:

| Connector | Name (`BRAIN_CONNECTORS`) | Legacy items to adopt | Available |
| --- | --- | --- | --- |
| Google Calendar | `gcal` | yes — pasted `calendar` items | now |
| Google Tasks | `gtasks` | none (nothing to adopt) | now |
| Google Drive | `gdrive` | none | now |
| Gmail | `email` (alias `gmail`) | yes — bot-captured `email` items | now |

All connector scopes are READ-ONLY. Enabling never lets a connector write to
Google. Once the opt-in **connectors scheduler** (`com.secondbrain.connectors`)
is installed, enabled non-Gmail connectors sync on a schedule with no
hand-running — `scripts/service.sh install` installs it only when
`BRAIN_CONNECTORS` names a non-email connector (email keeps its own agent,
`com.secondbrain.email`, which the scheduler never touches); each pass runs
one `brain connectors sync <name>` per enabled connector, sleeps
`BRAIN_CONNECTORS_INTERVAL_S` (default 300) between passes, isolates failures
per source (one failing connector never blocks the others), and maps exits
onto launchd's KeepAlive like the other agents. Until it is installed, sync
is manual (`brain connectors sync <name>`).

## How enabling works (read once)

- The enable switch is `BRAIN_CONNECTORS` in the **live checkout's `.env`**
  (`/Users/yos.i/git/Second-brain/.env`, gitignored). Comma-separated, e.g.
  `BRAIN_CONNECTORS=gcal,gtasks`. Default empty = nothing enabled. Unknown
  names fail fast at startup (a typo never silently disables a source).
- **A restart is required for the running services to see it.** The API and
  the Telegram bot read `.env` ONCE when their process starts (each launcher
  script pins the working directory to the repo root and starts uvicorn/the
  bot with that environment; `scripts/run-server.sh` and `scripts/run-bot.sh`
  both do this, and `load_env_file` only fills variables that are not already
  set in the environment). Editing `.env` does nothing to a running process.
  After editing: `scripts/service.sh restart` (kickstarts every installed
  agent; there is no per-agent restart target) — and if the connectors
  scheduler is not installed yet, `scripts/service.sh install` installs it
  now that `BRAIN_CONNECTORS` names a non-email connector (a later `install`
  also picks up an allowlist change; an uninstall removes the plist when the
  list is empty or email-only).
- Hand-run `brain connectors` commands read `.env` fresh on every invocation
  (a new process each time) — no restart involved for them.
- **Every live command below pins the live store explicitly**: the app
  refuses to touch the live database/Qdrant unless `BRAIN_ALLOW_LIVE=1`, and
  the live collection is `QDRANT_COLLECTION=brain_items`. Run them from the
  live checkout (`cd /Users/yos.i/git/Second-brain`) so `.env` supplies the
  live DATABASE_URL; the two explicit vars make each command self-describing.
- Everything that only READS (list, `--dry-run`, adopt `--dry-run`, the SQL
  checks) is safe to run any time. The first command that WRITES on live is
  `sync` (and `adopt` writing only ledger rows) — both are called out below.

## Command map

| Command | Reads live? | Writes live? | What for |
| --- | --- | --- | --- |
| `.venv/bin/brain connectors list` | yes (DB) | no | per-connector state: enabled, registered, last ok, cursor |
| `... auth google` | no (keychain write) | consent | one-time Google consent (setup doc) |
| `... adopt <name> --dry-run` | yes | **nothing** | the adoption report (gcal/email) |
| `... adopt <name>` | yes | ledger rows ONLY | teach the idempotency ledger about existing items |
| `... sync <name> --dry-run` | yes (DB + Google read) | **nothing** | preview the next sync |
| `... sync <name>` | yes | yes | the first real sync |

All of them need `BRAIN_ALLOW_LIVE=1 QDRANT_COLLECTION=brain_items` when run
by hand (shown in full below).

---

## gcal — Google Calendar

### 1. Prerequisites

- Deploy done (the running code contains the connectors framework and the
  gcal adapter; `scripts/service.sh status` shows the agents running).
- `docs/connectors-setup.md` completed: Google Cloud project, OAuth consent,
  client secret at the configured path, and `brain connectors auth google`
  run once (union scopes — calendar.readonly is included).
- No `gcal` cursor exists yet (`connectors list` shows cursor=no). The adopt
  step below REFUSES to run after a first sync — it is a pre-first-sync step.

### 2. Enable

Edit the live `.env` and add `gcal`:

```bash
BRAIN_CONNECTORS=gcal        # or extend the existing list, e.g. gcal,gtasks
BRAIN_GCAL_CALENDARS=primary # calendar allowlist; 'primary' = the account's main calendar
BRAIN_GCAL_PAST_DAYS=60      # how far back the FIRST sync reaches (frozen at first sync)
```

Then restart so the API and bot see it: `scripts/service.sh restart`.
An empty `BRAIN_GCAL_CALENDARS` value means: sync nothing. Calendar ids
(other than `primary`) come from the calendar's settings page in Google
Calendar.

### 3. Adopt the already-pasted calendar items (IMPORTANT — prevents duplicates)

Calendar events pasted into the bot were captured as items with
`source=calendar`. The connector stores its records under `source=gcal`, and
the capture fingerprint includes the source — so WITHOUT adoption the first
sync would re-capture every already-pasted event as a second item.

Adoption teaches the connector's ledger about the existing items. It writes
ONLY ledger rows pointing at the existing items (no item is created, edited
or re-indexed; the items keep their original source and history).

```bash
cd /Users/yos.i/git/Second-brain
BRAIN_ALLOW_LIVE=1 QDRANT_COLLECTION=brain_items .venv/bin/brain connectors adopt gcal --dry-run
```

Read the report:

- `WOULD ADOPT (n)` — these existing items are matched to Google events;
  after adopting, their first sync is a no-op (same version → skipped).
- `AMBIGUOUS (n)` — a record matched two or more existing items; NOT adopted
  (never guessed, never merged). They will arrive as NEW items on the first
  sync only if the dry-run lists them there — ambiguous ones stay out of the
  ledger and the report is explicit about each.
- `NEW ON FIRST SYNC (n)` — events Google has that were never pasted; the
  first sync captures them as new items (expected).
- `USER-DELETED (n)` — you deleted that item; adopt respects the deletion and
  never re-captures it.

When the report looks right, write the ledger rows (ledger ONLY — no items
are touched):

```bash
BRAIN_ALLOW_LIVE=1 QDRANT_COLLECTION=brain_items .venv/bin/brain connectors adopt gcal
```

If you change your mind before the first sync:
`BRAIN_ALLOW_LIVE=1 .venv/bin/brain connectors adopt gcal --undo` — deletes
exactly the ledger rows the adopt wrote (the DB is byte-identical to
pre-adopt afterwards). Undo refuses per-row if a sync already touched a row.

> `adopt` is available on main. Still: do NOT run the first gcal or email
> sync on live before adopting — without the ledger the first sync
> duplicates the already-captured items (pasted calendar events,
> bot-captured email).

### 4. Preview — sync --dry-run

```bash
BRAIN_ALLOW_LIVE=1 QDRANT_COLLECTION=brain_items .venv/bin/brain connectors sync gcal --dry-run
```

Prints the records the next sync would capture (one line each, same one-liner
renderer the bot uses) and `dry-run: N record(s), next_cursor=…` — and writes
NOTHING. Compare:

- `N` against the adopt report (`NEW ON FIRST SYNC` + any post-adopt changes).
- Titles/times of a few records you know (a pasted event should appear once,
  not twice).

Row counts are unchanged by a dry-run — that is asserted by tests, not
promised here.

### 5. First real sync

```bash
BRAIN_ALLOW_LIVE=1 QDRANT_COLLECTION=brain_items .venv/bin/brain connectors sync gcal
```

Expected output shape: `state=ok created=<new count> updated=<changed> skipped=<adopted count> cursor_saved=True`.

- `created` should equal the adopt report's `NEW ON FIRST SYNC` (+ anything
  that changed since the dry-run), `skipped` the adopted count, and there
  should be NO second copy of any pasted event.
- The cursor is saved only after the whole batch succeeded — a failed sync
  re-fetches the same records next time (idempotent by event id/version).
- Re-running the command is always safe: unchanged versions are skipped.

### 6. Verify

```bash
.venv/bin/brain connectors list        # run with BRAIN_ALLOW_LIVE=1 + the two vars
```

- `gcal` row: `enabled=yes`, `registered=yes`, `last ok` stamped, `cursor=yes`.

Read-only duplicate check (psql against the live DB; SELECT only):

```sql
-- every ledger row points at exactly one item; orphans must be 0
SELECT cr.source, count(*) AS records, count(mi.id) AS linked,
       count(*) - count(mi.id) AS orphans
FROM connector_records cr
LEFT JOIN memory_items mi ON mi.id = cr.item_id
GROUP BY cr.source;

-- no external_id captured twice
SELECT count(*) FROM (
  SELECT external_id FROM connector_records WHERE source = 'gcal'
  GROUP BY external_id HAVING count(*) > 1) d;

-- the pre-existing pasted items are untouched (source 'calendar' count unchanged)
SELECT source, count(*) FROM memory_items GROUP BY source ORDER BY 1;
```

- Cockpit: the health cards show the stack; a dedicated Connectors card is
  not on main yet — `connectors list` is the authoritative view for now.
- Search check: ask the bot (or `GET /search?q=<a known event title>`) for a
  known upcoming event — it should appear ONCE, with the gcal-rendered
  one-liner.

### 7. Switch it off again

Remove `gcal` from `BRAIN_CONNECTORS` in the live `.env`, then
`scripts/service.sh restart`. Effect: `sync gcal` refuses (not enabled), the
cockpit shows it disabled — and **all captured items stay** (nothing is
deleted, nothing is hidden), the ledger and the stored cursor stay. Re-enable
later and the sync resumes from the stored cursor.

---

## gtasks — Google Tasks

### 1. Prerequisites

Same as gcal: deploy done, setup + consent done, no `gtasks` cursor yet.
There are no legacy Tasks items to adopt — the first sync starts from an
empty ledger by design (the adopt step below says so and fetches nothing).

### 2. Enable

```bash
BRAIN_CONNECTORS=gtasks        # or extend: gcal,gtasks
GTASKS_LISTS=                  # empty = sync EVERY task list; or comma-separated
                               # list ids/titles (titles matched case-insensitively)
```

Restart for the services: `scripts/service.sh restart`. (An allowlist change
after syncing invalidates the stored cursor — the next sync re-lists fully so
newly-allowed lists are picked up.)

### 3. Adopt — not applicable

`brain connectors adopt gtasks --dry-run` prints "no legacy gtasks items —
nothing to adopt" and makes no Google fetch. You can run it for completeness
or skip straight to the dry-run sync.

### 4. Preview — sync --dry-run

```bash
BRAIN_ALLOW_LIVE=1 QDRANT_COLLECTION=brain_items .venv/bin/brain connectors sync gtasks --dry-run
```

`N record(s)` should roughly match your open + recently-completed task count
across the allowlisted lists (completed tasks are captured with their status,
not deleted). Titles readable, no HTML escapes in the terminal.

### 5. First real sync

```bash
BRAIN_ALLOW_LIVE=1 QDRANT_COLLECTION=brain_items .venv/bin/brain connectors sync gtasks
```

Expect `created` ≈ the dry-run `N`, `updated=0`, `cursor_saved=True`. The
cursor is the max `updatedMin` across lists; re-running skips unchanged tasks.

### 6. Verify

- `connectors list`: `gtasks` enabled + registered, `last ok` stamped, cursor yes.
- SQL checks as under gcal (same statements, `source = 'gtasks'`).
- Search: a known task by title appears once; subtasks carry their parent's
  title in the text.

### 7. Switch it off again

Remove `gtasks` from `BRAIN_CONNECTORS`, `scripts/service.sh restart`. Items
stay, the ledger and cursor stay, nothing is deleted. Re-enabling resumes
from the cursor (tasks updated while off are picked up by `updatedMin`).

---

## gdrive — Google Drive

### 1. Prerequisites

Deploy done (the running code contains the connectors framework and the
gdrive adapter); setup + consent done (the one-time consent covers all four
sources — the scopes are the union of ENABLED connectors, so Drive scope is
requested only once gdrive is enabled); no `gdrive` cursor yet.

### 2. Enable

Add `gdrive` to `BRAIN_CONNECTORS` in the live `.env` plus the Drive
allowlist settings:

```bash
BRAIN_CONNECTORS=gdrive     # or extend the existing list, e.g. gcal,gdrive
BRAIN_GDRIVE_FOLDERS=       # comma-separated immutable Drive folder ids;
                            # EMPTY (default) = the connector fetches nothing
BRAIN_GDRIVE_DEPTH=1        # folder levels below the allowlisted folders the
                            # first sync walks (hard cap 5)
BRAIN_GDRIVE_TEXT_CAP=10000 # max chars of exported text per file (truncation
                            # is flagged in the record's meta)
```

Restart: `scripts/service.sh restart`.

### 3. Adopt — not applicable

`brain connectors adopt gdrive --dry-run` reports "no legacy gdrive items —
nothing to adopt" (no pre-connector Drive items exist) and makes no fetch.

### 4. Preview — sync --dry-run

```bash
BRAIN_ALLOW_LIVE=1 QDRANT_COLLECTION=brain_items .venv/bin/brain connectors sync gdrive --dry-run
```

`N record(s)` = files under the allowlisted folders (within the depth cap).
Files whose content cannot be read as text still appear as metadata-only
records; binaries become attachment records (fetched lazily).

### 5. First real sync

```bash
BRAIN_ALLOW_LIVE=1 QDRANT_COLLECTION=brain_items .venv/bin/brain connectors sync gdrive
```

The cursor is Drive's `startPageToken` — the next sync fetches only CHANGES.
A moved/deleted/trashed file arrives as a deletion record: the item is marked
source-deleted (hidden from lists/search/ask), never hard-deleted.

### 6. Verify

- `connectors list` (gdrive enabled + registered, cursor yes), the SQL checks
  with `source = 'gdrive'`, and a search for a known doc's title.
- Attachments: PDFs/images become attachment records — check the cockpit
  attachments health card after the sync.

### 7. Switch it off again

Remove `gdrive` from `BRAIN_CONNECTORS`, restart. Items and ledger stay;
nothing is deleted. Re-enabling resumes from the stored page token (changes
made while off are picked up on the next sync).

---

## email — Gmail label

### 1. Prerequisites

Deploy done (the running code contains the connectors framework and the
email adapter); `docs/connectors-setup.md` consent done (Gmail
scope is part of the union consent); `GMAIL_LABEL_ID` set in the live `.env`
(the id, never the display name); the keychain holds the Google refresh
token (`brain gmail-auth` / `brain connectors auth google`).

### 2. Enable

Add `email` (or the `gmail` alias) to `BRAIN_CONNECTORS` in the live `.env`,
then restart the services. The always-on Gmail agent (`com.secondbrain.email`)
is OPT-IN: `scripts/service.sh install` installs it only when
`GMAIL_LABEL_ID` is set — without it the agent is deliberately not installed
(and a stale plist is removed). The agent restarts only on transient
failures; a revoked token PAUSES the poller inside the process instead of
flapping the agent.

### 3. Adopt the bot-captured email items (before the first sync)

The bot captured 23 emails as items (`source=email`); without adoption the
first connector sync risks duplicates. The adoption matcher works on
normalized text + send-minute (bot forwards rarely carry a Gmail message id —
the report says this honestly and lists the low-yield expectation).

```bash
BRAIN_ALLOW_LIVE=1 QDRANT_COLLECTION=brain_items .venv/bin/brain connectors adopt email --dry-run
# read the report: WOULD ADOPT / AMBIGUOUS / NEW ON FIRST SYNC / USER-DELETED
BRAIN_ALLOW_LIVE=1 QDRANT_COLLECTION=brain_items .venv/bin/brain connectors adopt email
```

If you change your mind before the first sync:
`BRAIN_ALLOW_LIVE=1 QDRANT_COLLECTION=brain_items .venv/bin/brain connectors
adopt email --undo` — deletes exactly the ledger rows the adopt wrote (the
DB is byte-identical to pre-adopt afterwards; undo refuses per-row if a sync
already touched a row). Adopt requires no cursor; it refuses after a first
sync — see the gcal section for the full report semantics.

### 4. Preview — sync --dry-run

```bash
BRAIN_ALLOW_LIVE=1 QDRANT_COLLECTION=brain_items .venv/bin/brain connectors sync email --dry-run
```

Expect the unread/new messages under the configured Gmail label. `N` should
be small on a first run right after adoption (recent mail only).

### 5. First real sync

```bash
BRAIN_ALLOW_LIVE=1 QDRANT_COLLECTION=brain_items .venv/bin/brain connectors sync email
```

Gmail pacing note: every Gmail API call is paced `gmail_min_call_interval_s`
apart (default 6 s; the budget is per Google USER, shared with the running
gmail-poll agent) — a first adopt/sync of ~200 messages takes ~20 minutes by
design, and a transient 403 quota error is retried in-run. See
[connectors-setup.md](connectors-setup.md), the Gmail quota section.

From then on the launchd agent syncs automatically (historyId cursor; an
expired history is handled internally with a full re-list + dedupe — never
double capture).

### 6. Verify

- `connectors list`: `email` enabled=yes, registered=yes, `last ok` stamped,
  cursor yes.
- The cockpit email health card turns ok (it exists today for the poller
  heartbeat).
- SQL checks with `source = 'email'`; a search for a known recent email
  subject appears once.
- 24/7 check after a day: `scripts/service.sh status` shows the email agent
  running and `connectors list` shows advancing `last ok`.

### 7. Switch it off again

Remove `email`/`gmail` from `BRAIN_CONNECTORS` **and** unset `GMAIL_LABEL_ID`,
then re-run `scripts/service.sh install` (the email agent is opt-in: with no
label id it is booted out and its plist removed) — or `scripts/service.sh
restart` for the CLI-only effect. Captured items stay; the ledger and
historyId cursor stay; re-enabling later resumes from the cursor.

---

## Quick reference

| Step | gcal | gtasks | gdrive | email |
| --- | --- | --- | --- | --- |
| Enable switch | `BRAIN_CONNECTORS=gcal` + `BRAIN_GCAL_CALENDARS` | `BRAIN_CONNECTORS=gtasks` (+ `GTASKS_LISTS`) | `BRAIN_CONNECTORS=gdrive` + allowlist | `BRAIN_CONNECTORS=email` + `GMAIL_LABEL_ID` |
| Adopt first? | **yes** (pasted calendar items) | no (nothing to adopt) | no | **yes** (bot-captured email) |
| Background agent | `com.secondbrain.connectors` (opt-in via install) | `com.secondbrain.connectors` (opt-in via install) | `com.secondbrain.connectors` (opt-in via install) | `com.secondbrain.email` (opt-in via install) |
| Switch off | unset + restart | unset + restart | unset + restart | unset label + `service.sh install` |
