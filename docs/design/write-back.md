# Stage 3 — Google write-back (Calendar + Tasks): design doc

Status: **draft for user GO** (issue [#63](https://github.com/yosisrael2/Second-brain/issues/63)).
Design only — no production code in this PR. Everything below is grounded in
the current tree (origin/main 22a3304); every code claim cites `file:line`.

## 0. The one rule

**A Google write happens only after an explicit human confirm — never
silently.** Meaning extraction is probabilistic; write-back is not. The
pipeline is `proposal → human confirm → write → undo-able record`, and every
stage is observable (ops events) and reversible (undo) or explicitly
permanent-with-warning (Tasks delete).

## 1. Proposal model

A new table `action_proposals` (one alembic migration — the only schema
change in the whole stage). Write-back has a different lifecycle than memory
items: the extracted child stays a memory proposal (`child_state='proposed'`,
`src/brain/db/models.py:134-138`) until *its own* Keep, independently of
whether an action was written for it.

A row holds:

| column | why |
|---|---|
| `id` (uuid) | the confirm/undo handle |
| `child_id` → `memory_items.id` | the extracted child the action came from (nullable ON DELETE SET NULL, like `connector_records.item_id`, `src/brain/db/models.py:591`) |
| `target` | `gcal_event` \| `gtask` |
| `payload` (JSONB) | the exact write body: `title` (Hebrew as extracted), `start`/`end` or `due`, `all_day`, `location`, `description`, `calendar_id`/`list_id` — a frozen snapshot of what WILL be sent |
| `status` | `proposed` → `written` → (`undone`) \| `failed` \| `dismissed`; CHECK constraint |
| `provenance` (JSONB) | `source_item_id`, the grounded quote, extractor version, match/recon verdict — the same audit discipline as fact provenance (`src/brain/db/models.py:349`) |
| `external_id`, `external_version` | filled at write time; `external_version` = etag/`updated` |
| `dedupe_key` (unique) | the idempotency key (§3) |
| `error` (JSONB) | the last failure (kind, HTTP status, message) for the failure card |

Proposals are CREATED by the enrichment pass alongside the child (kind-gated,
§14) and by nothing else. No code path other than the confirm endpoint ever
talks to Google.

## 2. Confirm UX (cockpit + Telegram one-tap)

**Cockpit**: a "Write-back" strip on the proposal card / a dedicated section:
one line per proposal — target icon, the Hebrew title, the resolved date in
`Asia/Jerusalem`, and two buttons: **צור ב-Google** (confirm) and **בטל**
(dismiss). Confirm = `POST /actions/{id}/confirm`, dismiss =
`POST /actions/{id}/dismiss` — the same shape as the memory screen's fact
confirm (`POST /memory/facts/{id}/confirm`, `src/brain/api/routes/memory.py:181`).
The card renders the payload BEFORE writing: the user confirms an exact
event/task, not an intention.

**Telegram**: one tap on the existing proposal card. The card keyboard
already carries typed callbacks (`CB_PROPOSED_KEEP = "pk:<token>:<index>"`,
`src/brain/services/proposals.py:63`); write-back adds `wb:<token>:<id>`
(confirm) and `wd:<token>:<id>` (dismiss) rendered the same way. Double-tap
is idempotent (the row's status is the guard — a second confirm on a
`written` row answers "already created" with the link, like the stale-button
handling the cards already have). After a write the card row flips to
✅ נוצר (with the event link); after undo it flips back to proposed.

## 3. Idempotency / dedupe (never double-create)

Two mechanisms, one per target:

- **Calendar — deterministic client-supplied event id** (issue #63's
  worker_9 note): the event id is `sb0-<sha256(child_id)>` sent by US
  (`events.insert` with an id). A retry after a crash re-sends the same id;
  Google answers **409 already-exists → treated as SUCCESS** and we fetch
  that event to fill the ledger (self-healing reconcile, no user action).
  This makes write-then-ledger ordering safe.
- **Tasks — no client-supplied ids exist**: the ledger is checked FIRST
  (`dedupe_key` unique + a `connector_records` lookup); a bounded
  `updatedMin` reconciliation re-lists recent tasks to heal a crash between
  write and ledger insert (documented heuristic from the worker_11 note).

At write time we ALSO insert a normal `connector_records` row
(`src/brain/db/models.py:572`: `source`, `external_id`, `version` = etag or
`updated`, `item_id` = the child) with source `google_writeback` — so our own
created objects are KNOWN records to the sync runners: same version skips,
Google-side edits flow into the child, Google-side deletions hide it via the
existing sync semantics. The ledger consult (`source='google_writeback'`,
`dedupe_key`) happens before every insert; a `written` proposal is never
re-sent.

## 4. Undo

- **Calendar**: `events.delete` on the created id; the proposal flips to
  `undone` and its ledger + `connector_records` rows are deleted — so a
  later re-confirm can genuinely recreate (the deterministic id is free
  again after Google's ~short reuse window; documented, with the retry
  policy).
- **Tasks**: delete is **PERMANENT** (no trash). The confirm card for a
  Tasks target says so in the undo hint (מחיקה סופית), and the undo button
  is still offered — permanent is not "impossible", it is "irreversible".
- Undo is also explicit-confirm (a second tap), never a side effect.

## 5. OAuth scopes per target (least privilege, separate tokens)

Today every Google connector is read-only
(`SCOPE_BY_CONNECTOR`, `src/brain/connectors/google/base.py:32`) and one
consent covers the union. Write-back must NOT widen that token:

- **gcal write**: a SEPARATE consent requesting only
  `https://www.googleapis.com/auth/calendar.events` (issue #63: "minimal
  write scope + separate incremental consent"). The sync token stays
  read-only forever.
- **gtasks**: no narrower write scope exists — `auth/tasks` covers read and
  write. Flagged as an open question (§15) because it is broader than
  ideal.
- **Separate keychain items**, one per target, exactly like the labeler's
  own item (`LABELER_KEYCHAIN_SERVICE = "second-brain-gmail-labeler"`,
  `src/brain/services/gmail_labeler.py:65-70`):
  `second-brain-gcal-write` and `second-brain-gtasks-write`. A missing item
  is a clean refusal (exit 78 pattern), never a fallback to the sync token.
  `brain connectors auth gcal-write` / `gtasks-write` run the incremental
  consent (the `--client-secret` flow the runbook documents,
  `docs/gcal-adopt-runbook.md`).

## 6. Audit trail (ops_events)

Every state change writes an ops event (`ops_events`,
`src/brain/db/models.py:190`): `writeback/proposed`, `writeback/confirmed`,
`writeback/ok` (latency + external id), `writeback/failed` (kind + HTTP
status), `writeback/undone`, `writeback/dismissed`, `writeback/cap_hit`,
`writeback/kill_switch` (a confirm attempted while disabled). Detail carries
ids and counts only — never tokens, never full payloads (the same rule the
labeler's audit follows). The ops heartbeat for the reconcile pass (§3)
shows last-run + healed-count.

## 7. Caps per day

`WRITEBACK_MAX_PER_DAY` per target (default **50** — user decision 4, 28 Oct): a counter derived from
today's `writeback/ok` events. At the cap, confirms answer with a clear
refusal (the proposal stays proposed) + `writeback/cap_hit` — a runaway
extractor can never spam the family calendar. The cap is checked before the
API call.

## 8. Kill switch (default OFF)

- `WRITEBACK_ENABLED: bool = False` (settings) — the master switch, the same
  pattern as `recon_judge_enabled` (`src/brain/config.py:99`).
- Per-target: `WRITEBACK_GCAL_ENABLED`, `WRITEBACK_GTASKS_ENABLED` (both
  default False) — enabling Tasks does not enable Calendar.
- When OFF: the confirm endpoints return 409 with the flag named, the
  cockpit/Telegram buttons are not rendered, and NOTHING constructs a client
  (the flag-first rule the judge review enforced). Merging ≠ enabling; the
  first enable is a user GO plus the separate consents.
- The live-store guard applies: writes target Google, but every CLI touch of
  the live DB still goes through `assert_not_live_store`
  (`src/brain/config.py`).

## 9. Failure modes

| failure | behavior |
|---|---|
| token expired/revoked (401) | proposal STAYS proposed; a `writeback/failed` event; the UI offers the re-consent command (exit-78 style message) |
| 404 on write/undo (target calendar/list gone) | proposal → `failed` with the error; the settings pointer is an open question (§15) |
| 409 (gcal id exists) | SUCCESS path: fetch + fill the ledger, flip to `written` (§3) |
| 412 If-Match (patch race — the user edited in Google while we wrote) | re-fetch, re-diff, ask for a SECOND confirm (never overwrite silently) |
| crash between write and ledger insert | gcal: deterministic id → the retry self-heals (409 path); gtasks: the reconcile pass repairs within the `updatedMin` window |
| Google 5xx / network | proposal stays `proposed`; retry is a fresh confirm (idempotent) |
| partial writes (event created, series scope rows failed) | the event stands (it is real in Google); the failure is recorded; the reconcile pass reconciles the ledger |

## 10. Hebrew content and Asia/Jerusalem timezones

Hebrew text is written AS EXTRACTED — no transliteration, no translation;
`summary`/`notes`/`title` keep the child's Hebrew verbatim (the extraction
prompt already normalizes). All-day events map `start.date` (no time);
timed events map `start.dateTime` + `timeZone: "Asia/Jerusalem"` — the same
mapping the read side parses (`record_from_event` /
`_event_instant`, `src/brain/connectors/google/calendar.py:112`). Due dates
for Tasks follow the Tasks quirk: `due` is an RFC3339 instant; a Hebrew
"ב-10.11" without a time becomes 09:00 Asia/Jerusalem (the local-midnight
backstop mirrors `_due_at_local_midnight`, `src/brain/connectors/google/tasks.py:146`).
Every rendered date in the confirm cards is `Asia/Jerusalem` (the cockpit's
existing convention).

## 11. Test plan

- **pytest (fakes only — no live calls ever)**: proposal creation from the
  enrichment pass (kind-gated); confirm happy path (fake service asserts the
  exact body: Hebrew title, dateTime+timeZone); 409 self-heal; ledger-then-
  skip (double confirm never double-creates); undo deletes + clears the
  ledger; cap hit; kill switch OFF = zero client constructions (the
  constructor-raises pattern from the judge review); failure mapping
  (401/404/412/5xx); the connector_records row written at write time.
- **jsdom**: the write-back strip renders proposed rows, flips on
  confirm/dismiss, hides with the kill switch OFF.
- **Playwright**: cockpit confirm → written state (fake service); Telegram
  callback handlers with fakes (double-tap idempotent).
- The fake Google services live in `tests/fakes/` next to the existing
  connector fakes; CI runs everything green with the flag OFF.

## 12. Rollout phases (each needs its own user GO)

Per the user's decisions (§15): **Tasks first** (decision 5 — tasks + reminders only at Phase 2, no calendar events yet); the Calendar target and its consent are deferred to Phase 3.

1. **Phase 0 (this PR)**: the design; nothing else.
2. **Phase 1 — plumbing**: the `action_proposals` migration, settings + kill
   switch, proposal creation in the enrichment pass (kept children; kinds
   `task`/`reminder` only), cockpit + Telegram rendering with confirm
   DISABLED. No Google call exists yet; no Google scope is requested.
3. **Phase 2 — Tasks writes**: the `second-brain-gtasks-write` consent +
   keychain item, confirm/undo (delete flagged PERMANENT), ledger-first
   idempotency + the `updatedMin` reconcile heal, the 50/day cap, audit.
4. **Phase 3 — Calendar events (deferred until the user GOes Phase 3)**:
   the **separate `calendar.events` consent** — requested only now
   (least privilege: no calendar write scope exists before this phase);
   the dedicated family calendar (created by the user at GO time, its id
   as a setting); the deterministic `sb0-<sha256(child_id)>` event id with
   the 409 self-heal; If-Match patch discipline.

   ### Per-kid event colors (calendar phase, user addendum 28 Oct)

   - Events created **for a named kid** get that kid's Google Calendar
     `colorId` (`events.colorId`, from the 11 colors in `colors.get`). The
     mapping is **per-kid config on the kid entity** (a `calendar_color`
     attribute on the person entity, editable on the Memory screen's basic
     info) rather than a global settings dict — kid attributes already
     travel with the entity (`kids()` exposes class/grade/teacher), a new
     kid needs no code change, and the value survives renames. Default
     mapping: **Rona → colorId "4" (flamingo)**, **Itamar → colorId "9"
     (blueberry)** — distinct, user-changeable in the UI.
   - Events about **several kids or none** get the **calendar default**
     (omit `colorId` entirely — no explicit default color write).
   - The color is set **at create time only**. The after-edit sync
     (decision 7) must **never write colorId back**: a user recolor in
     Google flows INTO the child as an observed edit, never out again. The
     reconcile pass patches no color fields (enforced by test).
   - **Google Tasks has no color** — the per-kid cue there is a **title
     prefix** ("[Rona] …"), recommended (visible in every list view,
     survives sorting, user-editable). Applied at create time only; never
     re-prefixed by a later sync. If the user edits the title and removes
     the prefix, it stays removed.

5. **Phase 4 — reconciliation + digest**: the reconcile passes' ops
   heartbeat, Google-side-edit flow-through polish (including the color
   never-overwrite guard), and the **weekly Telegram digest** of
   writes/undos (decision 9 — sent even in a week with zero writes, so
   silence is informative).

## 13. Explicit non-goals

No silent writes; no writes without the per-target consent; no writes to
calendars/lists other than the configured target; no batch "confirm all"
(extraction quality is the risk — every write is one deliberate tap); no
editing of events the sync captured (write-back only ever creates its own).

## 14. What becomes a proposal (initial policy)

Only children the user has ALREADY kept as memory (`child_state='kept'` —
decision 6) get write-back proposals, kinds `task` and `reminder` only at
first (decision 5; calendar events join at Phase 3). A proposal is
(re)generated once per child (dedupe_key = child id + target); editing the
child makes a NEW proposal only via the confirm card's "edit before write"
later phase — out of scope here.

## 15. Decisions (user answers, 28 Oct — recorded by the manager)

1. **Target calendar**: a **dedicated family calendar** ("start with it and
   see if it works for us"). Created BY THE USER at Phase-3 GO time; its
   id becomes a setting (`WRITEBACK_GCAL_CALENDAR_ID`), never hardcoded.
2. **Tasks list**: the **default first Tasks list**.
3. **Tasks scope breadth**: `auth/tasks` (read+write on all lists) is
   **accepted** — Google offers no narrower write scope.
4. **Daily cap**: **50 per target** (not 20). §7 updated.
5. **First kinds**: **tasks + reminders only** — NO calendar events at
   Phase 2; the calendar target is Phase 3. §12/§14 updated.
6. **Kept-only children**: confirmed (the recommended option). §14 updated.
7. **After-edit sync**: **wanted** — a user edit in Google flows into the
   child (the connector_records version flow), including color (see §12:
   color is never written back).
8. **Undo window**: default (**none** — undo stays offered indefinitely,
   with the Tasks permanence warning).
9. **Weekly Telegram digest**: **wanted** — Phase 4 (§12).
10. **Consent UX**: terminal `connectors auth gtasks-write` / `gcal-write`
    at GO time — OK.

## 16. Implementation breakdown (the ordered PRs)

Every PR carries the flag discipline: write-back code exists from Phase 1
but **nothing renders, requests consent, or calls Google while the flags
are OFF** (all defaults below are OFF; merging ≠ enabling). Each PR is
individually revertable.

| # | Phase | Scope | Files | Tests | Migration | Flags (default) |
|---|---|---|---|---|---|---|
| 1 | 1 | `action_proposals` table + the settings/kill-switch block (`WRITEBACK_ENABLED`, `WRITEBACK_GCAL_ENABLED`, `WRITEBACK_GTASKS_ENABLED`, `WRITEBACK_MAX_PER_DAY=50`) + the status machine | alembic revision, `src/brain/db/models.py`, `src/brain/config.py`, `src/brain/services/writeback.py` (new, model + state transitions only) | migration up/down; CHECK constraints; flag defaults; status-machine transitions; kill-switch constructor-raises pattern | **YES** (the stage's only migration) | master flags exist, all False |
| 2 | 1 | Proposal generation in the enrichment pass (kept children; kinds task/reminder; dedupe_key; provenance to the source item) | `src/brain/services/extraction/service.py` (kept path), `src/brain/services/writeback.py` | proposal created exactly once per child+target; kinds filter; kept-only filter; provenance contents | no | gated on `WRITEBACK_ENABLED` (False) |
| 3 | 1 | Confirm UX: cockpit write-back strip + `POST /actions/{id}/confirm\|dismiss` (409 while the flags are OFF) + Telegram `wb:`/`wd:` callbacks + ops_events audit | `src/brain/api/routes/memory.py` (or new `routes/actions.py`), `src/brain/cockpit/memory.js`/`memory.css`, `src/brain/telegram_bot.py` card keyboard, `src/brain/services/proposals.py` | pytest: 409-when-off; double-confirm idempotent; ops events written; jsdom: strip renders proposed rows, flips on confirm, hides when OFF; Telegram stale-button graceful | no | endpoints refuse while OFF |
| 4 | 2 | Tasks write path: `connectors auth gtasks-write` consent + `second-brain-gtasks-write` keychain item + confirm → `tasks.insert` + the `google_writeback` ledger (`connector_records` row at write time, version=etag/updated) + 50/day cap + failure mapping | `src/brain/cli.py` (auth subcommand), `src/brain/connectors/google/tasks.py`, `src/brain/services/writeback.py` | fakes: exact request body (Hebrew verbatim, due local-midnight backstop, "[Kid] " title prefix); ledger-then-skip (double confirm never double-creates); cap refusal; 401/404/5xx mapping; kill-switch OFF = zero client constructions | no | `WRITEBACK_GTASKS_ENABLED` (False) |
| 5 | 2 | Undo: Tasks delete (permanent — warned in the UX) + ledger cleanup + re-creatable after undo; ops audit completion | `src/brain/services/writeback.py`, cockpit/Telegram undo affordances | undo deletes + clears ledger + flips status; re-confirm recreates; audit rows for every transition | no | unchanged |
| 6 | 3 | Calendar write path (DEFERRED): the separate `calendar.events` consent + `second-brain-gcal-write` item + the dedicated calendar id setting + deterministic `sb0-<sha256(child_id)>` id + 409 self-heal + per-kid colorId at create (defaults Rona=4/Itamar=9; multi/none = omit) + Tasks prefix guard for the same rule | `src/brain/cli.py`, `src/brain/connectors/google/calendar.py`, `src/brain/services/writeback.py` | fakes: 409 self-heal; exact body (date vs dateTime+timeZone); color mapping per kid / omitted for multi+none; consent requested ONLY at this phase (assert no calendar scope before) | no | `WRITEBACK_GCAL_ENABLED` (False) |
| 7 | 4 | Reconciliation + after-edit sync guards: the `updatedMin`/list heals, connector_records version flow-through, the NEVER-write-back color guard, ops heartbeat | `src/brain/connectors/google/tasks.py`, `src/brain/connectors/google/calendar.py`, `src/brain/services/writeback.py` | reconcile repairs a write-without-ledger; version flow updates the child; color is never patched (the test from §12) | no | unchanged |
| 8 | 4 | Weekly Telegram digest of writes/undos (sent even at zero) | `src/brain/telegram_bot.py` (or a small scheduled job), `src/brain/services/writeback.py` | digest rendering with fakes; zero-write week still sends | no | follows `WRITEBACK_ENABLED` |

Sequencing notes: PR 1 blocks everything (schema); PR 2+3 complete Phase 1
(proposals visible, confirm still refuses). PR 4+5 = Phase 2 (Tasks live
behind its flag). PR 6 = Phase 3 (the first calendar scope request — needs
the user-created calendar id). PR 7+8 = Phase 4. No PR beyond #1 carries a
migration.
