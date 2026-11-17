# Stage 3 design: write-back to Google (Calendar + Tasks)

Draft — doc only, no code. Calendar half + assembly: worker_9 (pi /
second-brain-worker_9). Tasks half: worker_11 (pi / second-brain-worker_11).
Scope: when the user taps **Keep** on a proposed child, an event child
(`kind="calendar"`) creates a Google Calendar event and a task child
(`kind="task"`, `due=True`) creates a Google Task. Nothing else writes.

## 1. Overview

```
proposed child --Keep tap--> durable write job --> ledger row (pending)
  --> Google insert (gcal: deterministic event id; gtasks: ledger guard)
  --> ledger bound (external_id, etag/remote_updated)
      + connector_records row (source, external_id -> child, version)
read connectors: our objects are KNOWN records — same version skips,
Google-side edits flow into the child, deletions hide it (existing semantics)
Fix -> patch (bot-owned fields only)   Drop/undo -> delete (idempotent)
```

Principles: never automatic (outward-facing, hard to reverse); never a
duplicate (ledger + deterministic gcal id); never silent (ops + bot surface
every outcome); read/write share one OAuth client; our own objects become KNOWN
records at write time (connector_records), so Google-side edits and deletions
flow into the brain through the normal sync semantics.

## 2. Trigger + explicit confirmation (shared)

A write happens **only** on an explicit user Keep in the bot. Capture,
classification, enrichment and sync never write. The Keep card shows what will
be created and where (target calendar / task list, title, date/time) — the tap
is the confirmation act. Keep enqueues a durable write job (same queue pattern
as enrichment); the job re-reads the child before writing and is safe to run
later. Double taps are safe by construction (§4), not by UI discipline.

## 3. Scopes & consent

### 3.1 Calendar (worker_9)

The write path requests `https://www.googleapis.com/auth/calendar.events` —
read+write of events only; there is no narrower event-write scope (scope list:
https://developers.google.com/workspace/calendar/api/auth). The read connector
keeps `calendar.readonly`. Adding the write scope is a **separate consent**: the
consent flow re-runs with the enlarged scope set; Google incremental
authorization keeps the existing refresh token and adds scopes
(https://developers.google.com/identity/protocols/oauth2/web-server#incrementalAuth).
Same client secret file, same keychain token — the token's scope set grows.

`scopes_for()` change (google/base.py): add
`WRITE_SCOPE_BY_CONNECTOR = {"gcal": ("https://www.googleapis.com/auth/calendar.events",), "gtasks": ("https://www.googleapis.com/auth/tasks",)}`
next to `SCOPE_BY_CONNECTOR`, and
`scopes_for(enabled, write_enabled=())` returns read scopes of `enabled` plus
write scopes of `write_enabled` (stable order, deduped, same ValueError on
unknown names). Write enablement is the settings list `BRAIN_CONNECTORS_WRITE`
(default empty) — read-only installs keep requesting exactly today's set, so
their consent screen does not change.

### 3.2 Tasks (worker_11)

Tasks has exactly two scopes: `https://www.googleapis.com/auth/tasks`
(read+write) and `.../tasks.readonly` — there is **no** narrow write scope, so
the minimal possible grant for the Tasks half is `auth/tasks`
(https://developers.google.com/tasks/reference/rest/v1/tasks/insert#authorization-scopes).
It is a separate consent from the read-only one, via the same incremental
re-consent (URL above) and the same `scopes_for(enabled, write_enabled=())`
change — see 3.1 for the shared mechanism.

## 4. Idempotency — the write-back ledger

One shared table, one row per child (new migration):
`google_writeback(child_id PK/FK memory_items, kind 'gcal'|'gtasks', external_id,
etag, remote_updated, last_error, created_at, updated_at)`.
Create flow: INSERT row (pending) → Google insert → UPDATE row with
`external_id`. Retries/double taps check the ledger first: a row **with**
`external_id` short-circuits (return the existing Google id, no second insert).
A pending row (create may or may not have landed) is reconciled before any new
insert — see each half below. Ledger hit → no duplicate, ever. The ledger is
also the read-side self-recognition source (§6): change detection stores the
gcal `etag` and the gtasks `updated` string (`remote_updated` fits both).

### 4.1 Calendar (worker_9)

`events.insert` accepts a caller-supplied `id` (base32hex alphabet a–v + 0–9,
5–1024 chars: https://developers.google.com/workspace/calendar/api/v3/reference/events).
We send a deterministic id: `"sb0" + sha256("gcal:" + child_uuid)[:28]` (hex ⊂
base32hex, 31 chars, starts with a letter). A retry or double tap re-inserts
the **same** id → Google answers 409 "already exists" → treated as success:
`events.get(id)`, adopt it (the id is ours by construction) and bind the ledger;
the private marker is checked as belt-and-braces. Crash between insert and
ledger UPDATE self-heals the same way — the id is derived from the child, so no
state is needed to avoid duplicates. On success (fresh or reconciled) a
connector_records row is written with the returned etag (§6), so sync treats
the object as known from its first page. (Tasks has no client-set id: worker_11
reconciles via a bounded `updatedMin` list — §4.2/§8.)

### 4.2 Tasks (worker_11)

A pending gtasks row is reconciled before any new insert: with no client-set id
and no extension fields on the Task resource, the retry lists the write list
with `updatedMin = ledger.created_at` (a fresh task's `updated` == creation
time) and adopts an exact (title, due) match within the pending row's age — a
heuristic, documented as such (kept per design review). A success (fresh or
reconciled) also writes the connector_records row (§6). Ledger hit → no
duplicate. (tasks.insert generates the id server-side; no client-set id exists
— https://developers.google.com/tasks/reference/rest/v1/tasks/insert.)

## 5. Edit / undo

### 5.1 Calendar (worker_9)

Fix after create → `events.patch` (partial update) with ONLY the bot-owned
fields (summary, start, end, location, description) — fields the user edited in
Google are preserved unless the user explicitly fixes that same field (then the
user wins: the bot action IS the instruction)
(https://developers.google.com/workspace/calendar/api/v3/reference/events/patch).
Concurrent Google-side edit: we store the event's `etag` at write time and send
`If-Match` with it on patch; a 412 means it changed in Google since — the bot
fetches fresh and requires a second explicit confirm before overwriting
(conditional modification with ETags:
https://developers.google.com/workspace/calendar/api/v3/reference/events/patch).
Drop/undo → `events.delete`; a 404/410 (already gone) is success — idempotent
undo (https://developers.google.com/workspace/calendar/api/v3/reference/events/delete).
The app item is NOT deleted; `source_deleted` semantics apply only to read-sync.

### 5.2 Tasks (worker_11)

Fix → `tasks.patch` with only the fixed fields (title/notes/due); PATCH is
partial, so user edits in Google are preserved unless explicitly fixed
(https://developers.google.com/tasks/reference/rest/v1/tasks/patch). Drop →
`tasks.delete`; 404 is success
(https://developers.google.com/tasks/reference/rest/v1/tasks/delete). Tasks has
no trash/cancel — delete is permanent. Concurrent-edit detection: store
`remote_updated` (the task's `updated`) at write time; if it differs from a
fresh `tasks.get` before the patch, surface "changed in Google since" and
require a second explicit confirm (compare, never silently clobber).

## 6. Recognising our own objects (read side) — they are KNOWN records

No drop-filter, no marker for recognition (manager design review): at write
time, after a successful create, we ALSO insert a normal `connector_records`
row — `(source='gcal'|'gtasks', external_id=<google id>, item_id=<the CHILD
item>, version=<the etag / updated string we got back>)`. The read connectors
then see our own objects as KNOWN records and the existing runner semantics do
all the work:

- same version → unchanged, skipped (the common case — no duplicate item);
- the user edits the event/task in Google → version changes → the change flows
  into the child item through the NORMAL update path (Google-side edits are no
  longer silently ignored — a pure drop-filter would have let a time change
  made in Google never reach the brain; user-edit protection included);
- the user deletes it in Google → deleted record → the child is hidden via the
  EXISTING delete semantics (nothing new to build).

The write ledger keeps its two jobs: idempotency (child_id → external_id, §4)
and crash reconciliation (§4.1/§4.2). Calendar may additionally keep the
private marker as a belt for crash reconciliation only (NOT for recognition):
`extendedProperties.private = {"second-brain": {"child_id": "<uuid>", "v": 1}}`
— Calendar Event resources DO have extendedProperties (verified live from the
events resource JSON: `extendedProperties: { private: { (key): string },
shared: { (key): string } }`);
private properties are visible only to the creating OAuth client, and read and
write share one client.

### 6.1 Calendar (worker_9)

`record_from_event` maps marked events normally — they are known records via
connector_records. The marker is written on create and read only by the crash
reconciliation path (§4.1: 409 → `events.get(id)` → marker present → adopt +
bind). Documented nuance: deleting our own event in Google now HIDES the child
via existing delete semantics — same behaviour as any synced event; the undo of
the original write is separate from that sync signal. Event resource + private
properties:
https://developers.google.com/workspace/calendar/api/v3/reference/events and
https://developers.google.com/workspace/calendar/api/guides/extended-properties.

### 6.2 Tasks (worker_11)

The Task resource has NO extension fields — verified against the resource page
(https://developers.google.com/tasks/reference/rest/v1/tasks), whose complete
JSON field set is {kind, id, etag, title, updated, selfLink, parent, position,
notes, status, due, completed, deleted, hidden, links, webViewLink,
assignmentInfo}. gtasks therefore relies purely on the connector_records row
written at create time: same version → skip; edit in Google → flows into the
child; delete in Google → hidden via existing delete semantics. (Earlier
marker-based wording — `#Task.FIELDS.extended_properties` — retracted: a 200
on a URL does not verify an #anchor.)

## 7. Target calendar/list + field mapping from child fields

### 7.1 Calendar (worker_9)

Target: setting `BRAIN_GCAL_WRITE_CALENDAR`, default `"primary"`; if
`BRAIN_GCAL_CALENDARS` is set and excludes it → refuse at write time (visible
error, no cross-calendar write). Mapping (child fields per
`services/extraction/candidates.py` + resolved item columns):

| child | event field |
|---|---|
| `title` | `summary` |
| all-day (`all_day=True` or empty `time_raw`) | `start.date`/`end.date` = occurred_at's date in the TIMEZONE setting (end = next day); no `timeZone` |
| timed | `start.dateTime` = occurred_at (aware ISO) + `timeZone` = TIMEZONE name; `end` ← paste-parsed end (calendar paste carries start–end) else start + `BRAIN_GCAL_WRITE_DEFAULT_MINUTES` (default 30) |
| `quote` (+ source link) | `description` |
| paste `meta.location` | `location` |

Stage 3 creates single events only (children are one-offs; recurring/RRULE
children out of scope).

### 7.2 Tasks (worker_11)

Target list: setting `BRAIN_GTASKS_WRITE_LIST`; default `'@default'` (the API's
special default-list id:
https://developers.google.com/tasks/reference/rest/v1/tasks/insert#path-parameters).
If `BRAIN_GTASKS_LISTS` is set and excludes the write list → refuse. Mapping:
`title`→`title`; body→`notes`; due (date-only by our convention) →
`"YYYY-MM-DDT00:00:00.000Z"` — Tasks has no all-day flag and no timezone field;
the API treats `due` as effectively date-only and our read side already maps
midnight stamps back to the picked date, so the picked date survives
round-trip. Child times are dropped for tasks — a task is a date. Parent/
subtask: none in stage 3 (flat task).

## 8. Failure modes + ops visibility (shared)

- 401/403 + `invalid_grant` → `ConnectorAuthError` path: run stops, ops shows
  "needs re-consent" (same taxonomy as read).
- 429/5xx/network → retryable, runner-style backoff (job stays queued).
- 400 (bad list/calendar id, malformed due) → terminal: `ledger.last_error`
  set, `ops.record_event`, bot message states the write failed.
- 409 on insert → success (idempotency, §4.1); 412 on patch → conflict confirm
  (§5.1); create-succeeded-but-ledger-update-failed → next attempt reconciles
  via deterministic id (gcal) / bounded updatedMin match (gtasks) — never
  double-inserts; the connector_records row is then written so sync sees the
  object as known (§6).
- Every job outcome → ops heartbeat component `writeback:gcal` / `writeback:gtasks`
  + `ops.record_event` (create/patch/delete, ok/error, detail) — visible in
  /ops/health like `connector:<name>`. Ledger rows are inspectable.
- The write path follows the live-store rules: never in tests, keychain token
  required, disabled unless `BRAIN_CONNECTORS_WRITE` includes the connector.

## 9. Test plan (fakes, no network)

- Fake write services (duck-typed like the read-side `_FakeCalendarService`)
  recording insert/patch/delete payloads + injected errors
  (409/412/403/400/5xx/404/410).
- gcal: insert payload (deterministic id, summary, date vs dateTime+timeZone,
  marker, target calendar id); double Keep → ONE insert (409 reconcile path);
  crash-before-ledger → reconcile via deterministic id; connector_records row
  written at create with the returned etag; read-side: same-version object →
  skipped as known; Google-side edit (new version) → update flows into the
  child; Google-side delete → hidden via existing delete semantics; patch sends
  only changed fields; If-Match 412 → second-confirm flow; delete 404/410 →
  success; scopes_for union + empty `BRAIN_CONNECTORS_WRITE` default.
- gtasks (worker_11): insert payload; double Keep → one insert; pending-row
  reconciliation via the bounded updatedMin (title, due) match (no marker — the
  Task resource has no extension fields); read-side: same version → skip,
  edit → flows into child, delete → hidden; patch-only-changed; delete
  404 → success; auth error → re-consent surfaced; ledger repo tests.
- Ledger repo tests per worker on the worker's own test DB; bot flow tests with
  the write job stubbed assert enqueue-only until Keep.

## Sources

Calendar: events insert/patch/delete + resource (id rules, etag,
extendedProperties) https://developers.google.com/workspace/calendar/api/v3/reference/events
(https://developers.google.com/workspace/calendar/api/v3/reference/events/insert),
extended properties guide
https://developers.google.com/workspace/calendar/api/guides/extended-properties,
scopes https://developers.google.com/workspace/calendar/api/auth.
Tasks: REST reference https://developers.google.com/tasks/reference/rest/v1/tasks.
OAuth incremental consent
https://developers.google.com/identity/protocols/oauth2/web-server#incrementalAuth.
