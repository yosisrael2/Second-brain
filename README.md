# Second Brain

Second Brain is a local-first personal knowledge base that turns raw captures
(Telegram messages, pasted emails, WhatsApp threads, calendar one-liners, `.ics`
files) into typed, searchable memory items, keeps durable facts about your
world in a versioned memory layer, and answers questions over all of it with
cited, grounded answers — running 24/7 on your own Mac, private by default.

**Features (what works today on `main`):** see
[Features](#features) — **status:** [Status and progress](#status-and-progress).
Roadmap and known limitations: [Next steps](#next-steps).

## Features

Everything below is merged on `main`; items marked *(inactive)* are merged
but switched off until a configuration step. One line per capability, linked
to its section or doc.

**Capture**
- Telegram bot (long-polling, allowlist-only) and `POST /capture` API, both feeding the same paste parser — [Telegram bot](#telegram-bot), [Paste-aware capture](#memory-items-api)
- Paste recognition: email, WhatsApp thread, calendar one-liner, or plain note; WhatsApp threads ask "save as one item or separately" first — `src/brain/services/paste.py`
- Jev auto-classification (type / importance / actionability / confidence, routed at >= 0.9) with exact-duplicate guard (content fingerprint) — [Dogfood checklist](#dogfood-checklist-live-run-without-api-keys)
- `.ics` calendar files captured as calendar items — [Telegram bot](#telegram-bot)
- **WhatsApp ingestion (24/7):** the `com.secondbrain.whatsapp` agent reads the local WhatsApp bridge store read-only, only for channels registered in memory, groups messages into conversations by time gap (60 min) and stores one searchable item per conversation, indexed immediately — `src/brain/services/whatsapp.py`, `scripts/run-whatsapp.sh` (PRs #37, #40, #47, #50)
- **WhatsApp relevance triage:** every conversation is labeled `relevant | noise` — a confirmed kid's name decides deterministically (no model call), otherwise Jev, otherwise one strict LLM call; if both fail the item stays untriaged, never guessed. Noise stays stored and searchable but is hidden from the default listings and never spends the enrichment cap; an update can never downgrade `relevant` to `noise` — `src/brain/services/triage.py` (PR #49)
- **Attachments:** photos and PDFs sent to the bot are stored once (sha256) outside the repo and read in the background — a PDF text layer locally, otherwise Claude in a single-file sandbox; 20 MB / 20 pages / 50 files per day; each attachment becomes its own searchable item. Email and WhatsApp attachment content is wired in the same pipeline — `src/brain/services/attachments.py` (PRs #38, #44)
- **Recurring calendar pastes:** a pasted ICS VEVENT with an RRULE fans out into one item per occurrence inside a bounded window (`BRAIN_PASTE_RRULE_DAYS`, default 60 days, capped at 60 items per series), with a DST-stable wall clock (DTSTART's zone rides every occurrence), EXDATE/COUNT/UNTIL honoured, and a malformed rule falling back to a single item — `src/brain/services/recurrence.py` (PR #65)
- **Gmail source** *(inactive: disabled by default)*: read-only OAuth, one immutable label, Message-ID backstop, attachments via the attachment pipeline — now one live Gmail path: the connector runner owns capture/dedupe/deletes and the `com.secondbrain.email` launchd agent drives the same connector on a schedule (a backoff cycle sleeps like a normal interval — no hammering, and the pause logic is under test); refresh token only in the login keychain, nothing in the plists — [docs/email-setup.md](docs/email-setup.md) (PRs #36, #43, #59, #94, #101)

**Retrieval**
- Multilingual embeddings (local `fastembed`, `minishlab/potion-multilingual-128M`, 256 dims — Hebrew + English), Postgres FTS over raw text, reciprocal-rank fusion of the two — [AI layer](#ai-layer-and-the-brain-cli)
- Temporal retrieval: English + Hebrew weekday names, `dd.mm` dates, today/tomorrow/yesterday — `src/brain/services/temporal.py`
- Ask recall pool: rerank over 32 candidates, top 8 in the prompt — [Memory items API](#memory-items-api)
- Kid-aware recall: a question that names a kid (whole word, Hebrew one-letter prefixes allowed) also recalls items from that kid's channels and class — the channel→kid facts in memory steer recall to the kid's own WhatsApp channels even when the conversations never mention the name; announcements sent up to 3 days before the resolved window are recalled; the window is time-bounded instead of a newest-N cap that pushed mid-week conversations out; when a kid is named the kid's items get reserved slots in the ask context so general hits cannot crowd them out; the kid-channel query uses the same hidden-item rule as every other recall path, so superseded and confirmed-cancelled conversations stay out of recall (a restore brings them back) — `src/brain/services/ask.py` (PRs #32, #42, #89, #95, #98, #100)

**Ask**
- Grounded answers with item-id citations; model visible in the reply (`claude-sonnet-5-5` default) — [Memory items API](#memory-items-api), [docs/ai-layer.md](docs/ai-layer.md)
- Conflict rule: when two sources disagree about the same event, the answer opens with the conflict, quotes both versions with citations, and never claims "no conflict" — `src/brain/ai/ask.py` (PR #27)
- API and bot build the answer service through one builder (same local timezone and retrieval sizes) — `brain.api.deps.build_ask_service` (PR #35)

**Memory layer**
- Entities (person / group / place / org / channel + aliases) and versioned confirmed facts (`valid_from`/`valid_to`, `proposed -> confirmed -> rejected`, supersedes chain) — [Memory layer API](#memory-layer-api)
- Kid/class/teacher profiles, channel -> child mapping and `applies_to` resolution; memory block injected into ask answers — `src/brain/services/memory.py`
- REST `/memory` API, cockpit Memory tab, bot `/profile` and `/remember`, seed CLI (`brain memory seed-kid`, `seed-channel`) — [Memory layer API](#memory-layer-api)

**Meaning extraction and relations**
- Extraction data model + durable enrichment queue (daily cap, backfill budget, grounded quotes, Hebrew-calendar dates, weekday/date conflict flag, re-run diffing) — *(inactive: the enrichment worker is off)* (PR #33)
- LLM meaning extractor (A2): one call per source through the local Claude login client, versioned prompt (v6, recorded in every child's meta); the first pass parses strictly, the one error-fed retry parses leniently — valid candidates are kept, each invalid one is dropped with an `enrichment/candidate_invalid` ops event, and only a non-JSON reply or a missing candidates list fails the job; deterministic date resolution (never the model) with a quote_time backstop, derived tasks, multiple verbatim quote rows per child; scored against the frozen gold set through five offline eval runs — *(inactive: default stub extractor; `ENRICHMENT_EXTRACTOR=llm` + the worker flag activate it, pinned by a test)* — `src/brain/services/extraction/llm_extractor.py` (PRs #39, #68)
- A2 in-bot worker: with `ENRICHMENT_WORKER_ENABLED=true` the Telegram bot process runs the enrichment pass on a schedule (`ENRICHMENT_WORKER_INTERVAL_S`, default 300) — one pass per tick on a fresh session, runs off the polling thread so slow LLM calls never stall the bot, a running pass is never overlapped, a failed pass is logged as an ops event and the bot survives; every pass whose sources created or changed children delivers the proposal card through the existing hook (the bot flushes queued cards right after the pass); default OFF — the switch false (current live state) schedules nothing; `brain enrich` stays the manual path and sends no cards; a pass that outlives `ENRICHMENT_PASS_TIMEOUT_S` (default 900) is declared stalled once (ops event `enrichment/worker_pass_timeout`, health degraded) and the bot stops waiting on it — the stalled thread cannot be killed, so no new pass starts until it finishes — `src/brain/telegram_bot.py` (PRs #84, #93)
- **Reconciliation verdicts before proposals** *(inactive: runs inside the enrichment pass — off while extraction is off)*: every extracted child gets exactly one verdict before it can become a proposal — new / already known / update / more detail / conflict / cancellation; two matching windows (the same local day, plus a 30-day extended window for dateless or moved-date candidates that requires a change or cancel signal); two near-equal matches never silently pick one (they stay new, with an `enrichment/recon_ambiguous` ops event); auto-apply only when the match is in the same thread from the same sender — everything else stays proposed; a cancellation strikes the matched item through and is restorable; more detail covers a fact's effective date and amount, with amounts compared as numbers (PR #103); matching embeds with a local multilingual model loaded on first use (search keeps its own model, no vector-dim change anywhere) — (+ second-stage judge for the 0.60-0.80 band, off by default: `RECON_JUDGE_ENABLED`) — `src/brain/services/extraction/reconciliation.py` (PR #73)
- Bot proposal cards: one card per source with Keep / Fix / Drop and 48 h pings for due tasks and reminders — *(inactive until extraction runs)* (PR #41)
- **Reconciliation verdict cards on the proposal card** *(on main, inactive while extraction is off)*: each verdict child's proposed action rides the source's proposal card as tappable rows — update: ✅ Confirm update / ✖ Keep both; conflict: keep-new / keep-existing side by side; cancellation: confirm strikes the target through, ♻️ Restore revives; more detail renders a 🔔 detail line; every change is one explicit tap (double-tap idempotent, stale buttons answer gracefully) and nothing applies without a tap — `src/brain/telegram_bot.py` (PR #99)
- Relations between items (`supersedes`, `corrects`, `derived_from`, `same_as`): same-thread corrections applied visibly and undoably, cross-thread matches only proposed; superseded items are hidden from lists, search and ask but restorable; duplicate key never merges items for different people — `src/brain/services/relations.py` (PR #48)
- Bot relation cards: superseded items render struck-through with a ♻️ Restore button; applied corrections show a ↩︎ replaced notice with ↩︎ Undo; proposed cross-thread matches show ✅ Confirm replace / ✖ Keep both — `src/brain/services/relations_view.py` (PR #51)

**Connectors**
- Pluggable source framework: adding a source = one subclass + one `@register` line, fed through a shared runner — idempotency ledger (the same external id never re-captures), cursor saved only after a whole batch succeeds, `deleted=True` hides the item (never erased) and a later un-delete restores it, user-deleted items are never re-captured, per-source ops heartbeat; a content-fingerprint dedupe hit counts as `deduped`, not `created`; a failing `--dry-run` exits with the same error family as a real sync; a connector that keeps failing retryable-errors is skipped while its persisted backoff window runs (`state=backoff`, `retry_in_s`) instead of being re-fetched at full rate every pass — `src/brain/connectors/`, setup walkthrough in [docs/connectors-setup.md](docs/connectors-setup.md) (PRs #53, #76, #83, #90)
- **Four Google connectors, all READ-ONLY and all disabled by default** (`BRAIN_CONNECTORS` empty = nothing syncs): **Google Tasks** (`gtasks`, `tasks.readonly` — list allowlist, `updatedMin` watermark, deleted tasks hide their item while completed tasks stay as items, date-only dues anchor at local midnight, subtasks keep their parent), **Google Calendar** (`gcal`, `calendar.readonly` — one item per occurrence via `singleEvents`, per-calendar sync tokens, past cutoff `BRAIN_GCAL_PAST_DAYS` + a rolling future window `BRAIN_GCAL_FUTURE_DAYS` that re-lists once a day so far-future series never flood the first sync), **Google Drive** (`gdrive`, `drive.readonly` — folder allowlist, depth-capped, Docs/Sheets/Slides text with a per-file cap, PDFs/images as attachments, `changes.list` incremental), **Gmail** (`email`, `gmail.readonly` — one label, `historyId` cursor, first sync seeds the ledger from the legacy seen-table) — `src/brain/connectors/google/` (PRs #55, #56, #57, #59)
- **Enabling a connector** *(inactive until you opt in)*: add it to `BRAIN_CONNECTORS`, run the one-time browser consent (`brain connectors auth google`, scopes are the UNION of the enabled connectors), then — for Calendar and Gmail — a one-time `brain connectors adopt <name>` points the idempotency ledger at the items you already captured, so the first sync does not duplicate them (Tasks and Drive need consent only); once the opt-in connectors scheduler is installed (`scripts/service.sh install` — it installs only when `BRAIN_CONNECTORS` names a non-email connector) enabled connectors sync on a schedule with no hand-running; the runbook with the exact order is [docs/connectors-enable.md](docs/connectors-enable.md) (PRs #79, #81, #85, #82)
- **Conformance kit**: every connector registered on `main` must pass the six kit checks against the real runner — termination (small-limit syncs drain a scrambled dataset), key-paged termination with a tie group bigger than a page, idempotency (a re-sync changes nothing), change (one upstream change → exactly one ingested change, the item takes the adapter-rendered text), delete (hides via the PR-B mechanism, never hard-deletes), reset (a resync subset deletes nothing); Gmail declares its messages immutable so its change check runs the domain-true variant (a new record ingests once; the same id re-delivered is skipped, never a second item) — the sweep reads the registry live and fails naming any registered adapter without a donation file — `tests/conformance/`, `src/brain/connectors/conformance.py` (PR #60)

**Cockpit**
- One local dashboard: flow diagram, health, events, stats, debug traces, memory; KPI row; light + dark; laptop + phone; health cards for WhatsApp ("WhatsApp sources": per-channel conversations today/total and media read/not-read), attachments and extraction; a Connectors card (per-source status: enabled, last ok, cursor, `deduped` counter, last error); the item inspector shows item relations (directional, with proposed supersede suffixes) and a superseded banner with struck-through text — [Cockpit](#cockpit-ops-dashboard) (PRs #29, #46, #50, #54, #80)

**Ops**
- 24/7 launchd services (`scripts/service.sh`: API, bot, WhatsApp poller, plus an opt-in `com.secondbrain.email` Gmail agent and an opt-in `com.secondbrain.connectors` scheduler), single bot instance lock, install retries launchd's bootstrap race — [Background services](#background-services-launchd-247) (email agent: PR #43)
- Live-store guard at construction: the database engine and the vector store refuse the live store unless `BRAIN_ALLOW_LIVE=1` (set only by the service scripts); commands that never touch the database need no flag; migrations refuse the live database the same way (alembic guard); each test session fails loudly if the live collection gains test-originated vectors — [Live-store startup guard](#live-store-startup-guard) (PRs #31, #45, #70, #74)
- Tests isolated from live data: per-worker test databases reset (TRUNCATEd at session start) and per-worker vector collections; a per-test timeout and CI job timeouts; no test may create or modify files in the repo root — `dev-start.sh` takes its env-file path from `BRAIN_ENV_FILE` (tests point it at a temp file; a real developer's `dev-start` still creates `.env` when missing, but warns when the created file points at the live database), and a session tripwire fails the test run (exit 101) if `REPO_ROOT/.env` appears or changes — (PRs #22, #23, #38, #42, #62, #92)

**Evaluation**
- `brain eval-extraction`: gold-set scorer for extraction candidates (per-type P/R/F1, date accuracy, applies_to reported twice — raw and excluding gold `{unknown}` rows, denominator printed — quote grounding, relations, `date_conflict` recall) — [docs/eval-extraction.md](docs/eval-extraction.md) (PRs #39, #61)

## Status and progress

| Area | Status | PRs |
| --- | --- | --- |
| Capture pipeline (items, paste parsing, Jev, dedupe, recurring ICS fan-out, loud missing deps) | **Done** | #1, #7, #17, #19, #65, #77 |
| Retrieval (embeddings, FTS fusion, temporal, recall pool, kid-aware recall incl. a kid's own channels, lookback window, kid slots that become date-relevant additions, hidden-predicate channel queries) | **Done** | #20, #21, #24, #32, #42, #89, #95, #98, #100, #104 |
| Ask (citations, model config, conflict rule, one builder; one bad leg never kills an answer, a known contradiction is never hidden, narrow kid-leg rollback) | **Done, live** | #6, #27, #35, #135, #140 |
| Memory layer (entities, versioned facts, channels, ask block, API, UI) + Memory Management screen (part A endpoints: aliases, archive/restore, bridge groups, family summary; part B master-detail UI; redesign: profile, review inbox, family map, group labels, basic info incl. birthday-as-age; photo avatars: upload/change/remove on the profile, local-file-store bytes, initials fallback; WhatsApp groups section on a kid's profile with one-tap keep-as-memory) | **Done, live** (the memory-screen fixes #136 are on main, not yet live) | #26, #28, #30, #106, #118, #127, #130, #134, #136 |
| WhatsApp ingestion (24/7 agent, conversations, indexing) | **Done, live** | #37, #40, #47, #50 |
| Attachments (Telegram, email, WhatsApp media) | **Done** | #38, #44 |
| Gmail connector (`email`) + 24/7 email agent — one live Gmail path through the runner; the agent's loop treats a backoff cycle as a normal interval and a failing cycle sleeps through its stall window; quota hygiene (bounded listing, call pacing, rate-limit retry), vanished-message 404 skips + lazy history pages, reconcile-on-reset (tombstone ledger ids the sweep no longer lists); SB/ labeler v1 (rules over headers only, dry-run default, undo + audit; labeler scope readonly + `gmail.modify`) — the post-merge review majors are fixed (#142, merged and live; the superseded fix PR #143 was closed unmerged) and the runbook matches (#138); the labeler is deployed but NOT activated: before any dry-run it needs `GMAIL_ACCOUNT` (set by the user), the user-run `brain gmail-label --auth` (not done as of writing) and `GMAIL_LABEL_ID` (unset on live); a label guard is in review (PR #144) | **Done, opt-in** (readonly consent done; the labeler's separate `gmail.modify` consent (`brain gmail-label --auth`) is NOT done; school label first-synced by hand) | #36, #43, #59, #83, #94, #101, #109, #125, #129, #137 |
| Extraction data model + queue, LLM extractor (A2), proposal cards, activation seam, in-bot worker (per-pass deadline, week-1 source filter `ENRICHMENT_WORKER_SOURCES`) | **Done, inactive** (default stub; `ENRICHMENT_EXTRACTOR=llm` + `ENRICHMENT_WORKER_ENABLED` + a bot restart activate) | #33, #41, #39, #68, #84, #93, #107 |
| WhatsApp reconciliation verdicts before proposals (new / already known / update / more detail / conflict / cancellation; auto-apply only same thread + same sender; a fact's effective date and amount are more-detail fields, amounts compared as numbers; series scope this time / from now on, cross-kind cancellation, ask conflict leg) | **Done, inactive** (runs inside the enrichment pass) | #73, #103, #102, #121 |
| Reconciliation verdict cards on the proposal card (D-recon-2a: confirm / keep-both / keep-new / keep-existing / cancel / restore taps, one explicit tap per change) | **Done, on main, inactive** (off while extraction is off; not yet live) | #99 |
| WhatsApp relevance triage (noise stays searchable, out of listings + enrichment; kid names re-read once per pass; unread-media-only conversations stay untriaged and re-triage once on the attachment read — never on a filename + id) | **Done** | #49, #111, #113, #117, #124 |
| Connectors framework (runner incl. persisted-backoff enforcement, idempotency ledger, state, Google OAuth union consent, dedupe counter, clean dry-run errors) + setup docs | **Done** (connectors disabled by default) | #53, #58, #76, #83, #90, #108, #110, #115, #120, #123 |
| Enabling connectors: enable runbook, one-time `adopt` ledger pass | **Done** (runs once per connector, before its first sync) | #79, #81, #85 |
| Connectors background agent (opt-in launchd scheduler, `BRAIN_CONNECTORS_INTERVAL_S`, per-source failure isolation, email excluded) | **Done, opt-in** (installs only when a non-email connector is enabled) | #82 |
| Connector conformance kit (six checks against the real runner, registry-driven sweep, Gmail immutable-source change variant) | **Done** | #60 |
| Google Calendar connector (`gcal`, read-only; bounded sync window: past 7d, rolling future 60d; adopt runbook with window envs + dry-run reading) | **Done, on main, inactive** (disabled by default) | #55, #114, #132 |
| Google Tasks connector (`gtasks`, read-only) | **Done, on main, inactive** (disabled by default) | #56 |
| Google Drive connector (`gdrive`, read-only) | **Done, on main, inactive** (disabled by default) | #57 |
| Relations + dedupe + bot cards (Undo / Confirm / Keep-both / Restore) | **Done** | #48, #51 |
| Telegram bot + launchd services (incl. the `network_recovered` event after a failure streak) | **Done** | #9-#12, #16, #18, #50, #67, #96 |
| Cockpit (ops dashboard, restyle, health cards, WhatsApp sources, Connectors card, relations inspector) | **Done** | #13, #29, #46, #54, #80 |
| Ops safety (test isolation + session-start reset, live-store guards incl. the alembic live guard, ports, timeouts, tests never write the repo env file, fail-fast on unknown test-DB revision with `BRAIN_TEST_DB_RESET` opt-in) | **Done** | #10, #22, #23, #31, #38, #42, #45, #62, #70, #74, #75, #92, #122, #128 |
| CI + extraction evaluation (incl. applies_to excl-gold-unknown; fastembed model cached between runs, 600s timeout on the eval module only) | **Done** | #4, #5, #25, #39, #61, #112 |

**In progress** (26 Oct 2026)

- Gmail label guard (PR #144, open): `gmail-label` refuses when `GMAIL_LABEL_ID` is unset (live has it unset), instead of an obscure API error or a whole-mailbox scan
- WhatsApp acceptance re-run 5 (unblocked: #102 and #104 merged)
- Reconciliation judge (branch local, not yet a PR)
- Stage-3 Google write-back design (PR #63, draft — needs an explicit user GO before any write-back work); the design doc: [docs/design/write-back.md](docs/design/write-back.md) (proposal → human confirm → write; Calendar + Tasks; kill switch default OFF)

Merged on `main`, **not yet live**: #136 (memory-screen fixes — Move-to-basic-info really moves and is undoable; a superseded value stays history and is undone by a new `undo-supersede` op instead of looking user-rejected; a partial move rolls back per subject; the jsdom screen tests fail rather than skip in CI; the add-memory Title field, stored in provenance with no migration) and #138 (labeler runbook, docs only). The live instance runs `fde5365` (deployed 26 Oct ~10:57Z — *reported*; no migration, so the live alembic head stays `7d90f6ea8407`; pre-deploy backup `brain-pre-fde5365-20261026-1256.sql` — *reported*), so live carries the ask fixes #135/#140, the Gmail labeler #137 and its majors fix #142. Rollback is `git checkout --detach` of the previous sha + reinstall + restart, never `alembic downgrade`; DB backups live in `~/second-brain-backups/`. The enrichment worker is OFF (the default stub extractor), every connector is registered but disabled, and the email + connectors launchd agents are not installed. Gmail by hand: adopt linked 18 pasted emails and the school label sync was later completed with its cursor saved (*reported*; live holds 153 items in total — *reported*). The reconciliation verdict cards (PR #99), the Memory Management screen and its redesign (#130, #134) are live.

**Planned**

- Pending user decisions: one-time Google consent (activate Gmail) and switching extraction on after the held-out score
- Then: actions — approved items become Google Calendar events / tasks

**Quality check** (22-23 Oct 2026, real questions): calendar questions **10/10**,
school-email questions **11/12**, WhatsApp questions **6.5/11** (up from a
**5.5** baseline before the memory layer). The WhatsApp gold set is held
privately and is not part of the repo.

## Stack

- Python 3.11, FastAPI, Pydantic v2
- SQLAlchemy 2 + Alembic, Postgres 16
- Qdrant (vector search)
- pytest, docker compose

## Architecture

A visual, code-referenced tour lives in
[docs/how-it-works.html](docs/how-it-works.html) (self-contained, open in any
browser). The short version:

```
client ──> api/routes      (thin routers: HTTP in/out, no business logic)
              │ Depends
              ▼
          services         (business logic, framework-free)
              │ constructor-injected
              ▼
          repositories     (Repository pattern: all data access)
              │
              ▼
          db/              (SQLAlchemy models, session, Alembic migrations)
```

- **Dependency Injection**: routers receive services via `fastapi.Depends`
  (`src/brain/api/deps.py`); services receive repositories via constructor,
  typed against a `Protocol` (Dependency Inversion).
- **Repository pattern**: all DB access lives in `src/brain/repositories/`.
- **Service layer**: all business logic lives in `src/brain/services/`.
- **Factory**: `create_app()` in `src/brain/main.py` assembles the app;
  `create_engine_from_settings()` builds the DB engine from config.
- `src/brain/ai/` hosts the Strategy implementations (Embedder/LLMClient/Jev
  classification, vector search, rerank, grounded Q&A) behind abstractions —
  owned by the AI/integrations track; see [docs/ai-layer.md](docs/ai-layer.md).

## Folder layout

```
scripts/
├── dev-start.sh         # preflight + compose + alembic + uvicorn (local dev)
└── dev-stop.sh          # stop server / containers / (optionally) purge volumes

src/brain/
├── config.py            # env-based settings (pydantic-settings)
├── factories.py         # provider factories: LLM/embedder by LLM_PROVIDER/EMBEDDER_PROVIDER
├── main.py              # FastAPI app factory + ASGI entrypoint (app = create_app())
├── cli.py               # `brain` CLI (reindex: rebuild Qdrant from Postgres)
├── schemas.py           # pydantic request/response models
├── ops/                 # cockpit ops layer: events/heartbeats/spans, health,
│                        #   flow graph, traces, stats (used by API + bot shim)
├── cockpit/             # dashboard static assets (vanilla JS/CSS, no build)
├── ai/                  # AI integrations (owned by AI/integrations track)
├── api/
│   ├── deps.py          # FastAPI dependency providers (service composition)
│   └── routes/          # thin routers: health.py, items.py, search.py, ask.py, ops.py, memory.py
├── services/            # business logic: items, ingest, search, ask, memory
├── repositories/        # data access: items.py, memory.py
└── db/
    ├── enums.py         # shared enums + FTS constant (single definition)
    ├── base.py          # DeclarativeBase + naming convention
    ├── models.py        # ORM models (MemoryItem)
    ├── session.py       # engine, SessionLocal, get_db dependency
    └── migrations/      # Alembic env + versions
tests/
├── conftest.py          # app client, DB migration + cleanup fixtures
├── fakes.py             # offline fakes shared by wiring tests
├── test_health.py
├── test_items.py
├── test_wiring.py       # /items /search /ask with fake-backed infra
├── test_ingest_service.py / test_search_service.py / test_ask_service.py
├── test_cli_and_lifespan.py
├── test_ops_core.py / test_ops_health.py / test_ops_flow.py
├── test_ops_traces.py / test_ops_routes.py   # cockpit + ops layer
├── test_dev_scripts.py  # dev-start.sh/dev-stop.sh syntax + --help contracts
└── ai/                  # AI-layer unit tests (offline)
```

## Memory items API

A memory item is a captured piece of knowledge (a note, task, decision, ...)
that starts in `status=inbox` and gets classified and filed later.

**Data model** (`memory_items` table, `src/brain/db/models.py`):

| Field          | Type / constraint                                          |
| -------------- | ---------------------------------------------------------- |
| `id`           | UUID, primary key, auto-generated                          |
| `raw_text`     | text, required                                             |
| `source`       | string ≤64, required, default `manual`                     |
| `type`         | enum `ItemType`: `task\|decision\|note\|event\|reference\|idea`, nullable |
| `project`      | string ≤128, nullable (free-form; no projects table in MVP) |
| `importance`   | int 1–5, nullable                                          |
| `actionability`| enum `Actionability`: `none\|later\|now`, nullable          |
| `confidence`   | float 0.0–1.0, nullable (set by classifier)                |
| `status`       | enum `ItemStatus`: `inbox\|filed\|needs_review`, default `inbox` |
| `search_vector`| generated `tsvector` over `raw_text` (English FTS), GIN-indexed |
| `created_at` / `updated_at` | timestamptz, server-generated                 |

Validation is enforced at both the API layer (pydantic, `src/brain/schemas.py`)
and the database (CHECK constraints). The enums are defined once in
`src/brain/db/enums.py` — import them, never duplicate (shared contract with
the AI/integrations track).

**Endpoints** (router: `src/brain/api/routes/items.py`, service:
`src/brain/services/items.py`, repository: `src/brain/repositories/items.py`):

| Method | Path               | Behavior                                                        |
| ------ | ------------------ | --------------------------------------------------------------- |
| POST   | `/items`           | Create; 201 + item. Validation errors -> 422. `?classify=false` disables auto-classification (default: on) |
| GET    | `/items`           | List newest-first; filters `type`, `project`, `status`; `limit` (1–200, default 50), `offset` |
| GET    | `/items/search?q=` | Keyword search over `raw_text` via Postgres FTS (websearch syntax, e.g. `foo OR bar`); same pagination |
| GET    | `/items/{id}`      | Fetch one; 404 if missing                                        |
| PATCH  | `/items/{id}`      | Partial update (absent fields unchanged); 404 if missing         |
| DELETE | `/items/{id}`      | Delete; 204, or 404 if missing                                   |
| POST   | `/items/{id}/relations` | Link to another item (`to_id`, `kind` supersedes\|corrects\|derived_from\|same_as, `provenance`). kind=supersedes auto-APPLIES (item 20a: old item struck-through, undoable); `propose: true` only PROPOSES (item 20b, cross-thread LLM match — old item stays visible until confirm) |
| GET    | `/items/{id}/relations` | Every relation touching the item                                   |
| GET    | `/items/{id}/current`   | Resolve the supersedes chain to the CURRENT version                |
| POST   | `/items/relations/{rid}/confirm` | Confirm a proposed supersede (old item struck-through)    |
| POST   | `/items/relations/{rid}/undo`    | Undo/restore (item 20a/D4: old item current again)        |
| GET    | `/search?q=`       | Semantic search (embed -> Qdrant -> Jev rerank); falls back to keyword search (`score: null`) when embeddings/Qdrant are unavailable |
| POST   | `/ask`             | Answer a question from top-k items with citations; 502 when the LLM is unconfigured/down |

Example:

```bash
curl -X POST localhost:27800/items -H 'Content-Type: application/json' \
  -d '{"raw_text": "Renew the ssl cert", "type": "task", "importance": 5}'
curl 'localhost:27800/items?type=task&status=inbox'
curl 'localhost:27800/items/search?q=renew+cert'
curl -X PATCH localhost:27800/items/<id> -H 'Content-Type: application/json' -d '{"status": "filed"}'
curl -X DELETE localhost:27800/items/<id>
# AI endpoints (see docs/ai-layer.md for behavior + env vars)
curl -X POST 'localhost:27800/items?classify=false' -H 'Content-Type: application/json' \
  -d '{"raw_text": "raw capture, no classification"}'
curl 'localhost:27800/search?q=how+do+I+roll+out+kubernetes'
curl -X POST localhost:27800/ask -H 'Content-Type: application/json' \
  -d '{"question": "what port does the server run on?"}'
```

Tests: `tests/test_items.py` (needs the compose Postgres; skipped with a hint
if it is down). Wiring tests run fully offline (`tests/test_wiring.py`).

## Memory layer API

Durable, confirmed knowledge about the user's world, separate from the
capture stream: **entities** (person / group / place / org, with aliases)
and **versioned facts** (subject → predicate → object/value, with
`valid_from`/`valid_to` and `proposed → confirmed → rejected` status).
Notes are WHAT HAPPENED; memory is WHAT IS TRUE. Storage lives in
`entities` / `entity_aliases` / `memory_facts` (migration
`b7c2d5e8f1a4`); the business rules live in `brain/services/memory.py`
(`kids()`, `set_kid()`, `remember()`, `compact_block()` for the ask prompt,
`resolve_applies_to()`), the HTTP surface in `brain/api/routes/memory.py`.

**Facts are never edited in place**: PATCH closes the old version
(`valid_to=now`) and opens a new one chained via `supersedes_id` — history
is kept. DELETE is soft (`status=rejected`, `valid_to=now`); the row stays
queryable via the history endpoint and disappears from the overview.
Confirm only moves a fact from `proposed` to `confirmed` (409 otherwise).
Every fact carries `provenance` (where it came from: `user` or a source
item + quote) — extraction (PR A) will insert proposals through the same
schema.

**Endpoints** (router: `src/brain/api/routes/memory.py`, service:
`src/brain/services/memory.py`):

| Method | Path                        | Behavior                                                          |
| ------ | --------------------------- | ----------------------------------------------------------------- |
| GET    | `/memory`                   | Entities with their current facts, grouped; `?status=proposed` for the inbox (empty groups dropped), `?q=` filters by name/alias/fact value |
| POST   | `/memory/entities`          | Create an entity (`kind`, `name`, optional `aliases`); 201, or the existing identical entity |
| PATCH  | `/memory/entities/{id}`     | Rename; 404 if missing                                            |
| DELETE | `/memory/entities/{id}`     | Hard delete (cascades aliases + its facts); 404 if missing        |
| POST   | `/memory/facts`             | Add a fact; `status=confirmed` (default) or `proposed`; 404 unknown subject, 422 without object/value |
| PATCH  | `/memory/facts/{id}`        | Edit = close old version + open new (returns the new version); 404 if missing |
| DELETE | `/memory/facts/{id}`        | Soft delete (rejected, `valid_to=now`, history kept); 409 if already closed |
| POST   | `/memory/facts/{id}/confirm`| `proposed → confirmed` in place; 409 when not proposed            |
| GET    | `/memory/facts/{id}/history`| The version chain, oldest first                                   |

Example:

```bash
curl -X POST localhost:27800/memory/entities -H 'Content-Type: application/json' \
  -d '{"kind": "person", "name": "Dana", "aliases": ["דנה"]}'
curl -X POST localhost:27800/memory/facts -H 'Content-Type: application/json' \
  -d '{"subject_id": "<entity-id>", "predicate": "grade", "value": "5"}'
curl 'localhost:27800/memory?status=confirmed'          # grouped overview
curl 'localhost:27800/memory?status=proposed'           # the Proposed inbox
curl -X PATCH localhost:27800/memory/facts/<fact-id> -H 'Content-Type: application/json' \
  -d '{"value": "6"}'                                   # new version, old one closed
curl -X POST localhost:27800/memory/facts/<fact-id>/confirm
curl 'localhost:27800/memory/facts/<fact-id>/history'   # the version chain
```

Tests: `tests/test_memory_api.py` (every route + the versioning semantics),
`tests/services/test_memory.py` (the core service), and the bot memory
commands in `tests/test_telegram_memory.py`.

In the **Telegram bot**: `/profile` shows the kids (gender/class/grade/
teacher) and remembered facts with one ✖ button per fact (soft delete,
history kept); `/remember <text>` stores a confirmed free-text fact on the
`me` entity.

## Cockpit (ops dashboard)

`http://127.0.0.1:27800/cockpit/` — one local dashboard showing whether every
part of Second Brain is connected and healthy. Served by the API itself
(localhost-only binding), vanilla JS/CSS under `src/brain/cockpit/` — no CDN,
no build step. Auto-refreshes every 5s (pausable), light + dark, laptop +
phone widths.

Tabs:

- **Flow** — the live data-flow graph (Telegram/API -> parser -> ingest ->
  Jev/Postgres/embedder -> Qdrant/rerank/LLM, plus Docker/background/disk).
  Edges are green (healthy), amber (some errors), red (failing or target
  down) or grey (no traffic in 15 min); animated dashes = traffic in the
  last 60s. Click a node to jump to its health card.
- **Health** — per-component status (`ok` / `degraded` / `down` / `disabled`)
  with details; expensive probes (embedder/jev/llm cost tokens) run only via
  the "Run probe" button, never on auto-refresh.
- **Events** — the `ops_events` log (component, kind, ok, latency, edge,
  bounded JSON detail), filterable + paginated, errors highlighted.
- **Stats** — per-component call counts/error rates/p50/p95 (24h + 7d),
  classification-confidence histogram, item status shares.
- **Debug** — dry-run traces (capture: parse -> classify -> embed ->
  neighbours, saves nothing; search: embed -> candidates -> rerank order ->
  fallback decision) and the item inspector (DB row + vector payload + item
  events). The ask trace runs the real LLM (labeled, user-initiated).
- **Memory** — the memory layer (entities + versioned facts): kids first,
  search, add/rename/delete entities, add/inline-edit/soft-delete facts,
  a "Proposed" inbox (✅ confirm / ❌ reject), and a per-fact history drawer
  showing every version with its provenance (source item + quote; item ids
  open in the Debug inspector).

Underlying ops endpoints (router: `src/brain/api/routes/ops.py`):

| Method | Path                  | Behavior                                                        |
| ------ | --------------------- | --------------------------------------------------------------- |
| GET    | `/ops/health`         | Cheap per-component checks (cached 5s; probes never run here)   |
| POST   | `/ops/probe/{c}`      | On-demand probe; 404 for unknown components                     |
| GET    | `/ops/flow`           | Flow-graph nodes/edges with live status + per-hop stats         |
| GET    | `/ops/events`         | Event log; filters `component`, `ok`, `since`; `limit`/`offset` |
| GET    | `/ops/item/{id}`      | Item inspector (row + vector payload + item events)             |
| GET    | `/ops/stats`          | 24h/7d stats + confidence histogram + status shares             |
| POST   | `/ops/trace/capture`  | Capture dry-run — never writes                                   |
| GET    | `/ops/trace/search?q=`| Search pipeline trace (before/after rerank, fallback decision)  |
| POST   | `/ops/trace/ask`      | Ask trace — runs the real LLM (user-initiated)                  |

Diagnostics note: live checks that exercise the ask path with a FAKE LLM are
safe by default — `FakeLLMClient` is marked synthetic and NEVER records ops
spans/events, so a verification run cannot flip `/ops/health`'s llm component
('last call failed' / spurious 'ok'). Passing `record_ops=False` still skips
the `ask→search` / `ask→llm` spans for any client (diagnostics runs); real
requests keep the default (`record_ops=True`) and remain the only source of
llm health evidence.

Health components `attachments` (queue depth, read today / daily budget, last
error) and `enrichment` (queue depth, cap used / limit, worker on/off — off is
the designed inert state) mirror `GET /ops/attachments` and `GET
/ops/enrichment`; the cockpit renders their cards automatically. The telegram
heartbeat threshold follows the bot's actual long-poll timeout
(`telegram_poll_timeout_s` + margin), and the poll loop heartbeats on the
conflict/network retry paths too — a quiet chat no longer flaps "degraded";
a streak of network failures also records a `network_recovered` event when
the connection comes back (PR #96).

Privacy contract: the ops layer never records secrets (tokens appear only as
`set`/`unset` + last 4 chars) and never note bodies — events/traces carry
item ids, lengths and error types; item text appears only in the inspector
you explicitly open for one item. Events are pruned after 7 days.

Instrumentation lives in the services (not the routes): ingest spans
`ingest->jev`, `ingest->postgres`, `ingest->embedder`, `embedder->qdrant`;
search spans `search->embedder/qdrant/rerank`; ask spans `ask->search/llm`;
the Telegram bot writes its own events + heartbeat through the
`brain.ops` shim it already had. `GET /health`-class api requests are
recorded by a middleware that skips `/ops` and `/cockpit` paths (so the
dashboard's own polling does not flood the log).

## AI layer and the `brain` CLI

Auto-classification on `POST /items` (Jev), semantic `/search` (embeddings +
Qdrant + rerank), and grounded `/ask` are optional at runtime: with their API
keys unset the app still captures and keyword-searches — degradations and
failure modes are documented in [docs/ai-layer.md](docs/ai-layer.md).

**Extraction evaluation**: `brain eval-extraction --gold <jsonl> --pred
<jsonl> [--aggregate-only]` scores predicted child items against the frozen
gold labels (per-type P/R/F1, date accuracy, applies_to accuracy, quote
grounding, relation reproduction, date_conflict recall). `--aggregate-only`
prints only the numbers (for the extraction developer); full mode writes a
per-child report for the manager. Schema, matching and metrics:
[docs/eval-extraction.md](docs/eval-extraction.md).

**Default providers need no API keys**: the LLM runs on your local Claude Code
login (`claude_agent_sdk`, check with `claude -p ok` — run `claude` and
`/login` if needed) and embeddings run locally via `fastembed`
(`minishlab/potion-multilingual-128M`, 256 dims, multilingual — Hebrew + English;
the model downloads once on first use).

> **Migrating from the old English-only embedder (bge-small-en-v1.5)?** The
> vector dimension changed (384 -> 256): the first write fails with the
> "collection built with a different embedder" guard. Run `brain reindex`
> once — it drops and rebuilds the collection from Postgres (the source of
> truth).
Switch back to keyed providers with `LLM_PROVIDER=anthropic` /
`EMBEDDER_PROVIDER=openai` (see `.env.example`).

Rebuild the vector index from Postgres (covers items that missed indexing
while Jev/Qdrant were down, and purges ghost vectors for items no longer in
Postgres — it drops and rebuilds the collection):

```bash
brain reindex [--batch-size 500]
# exit 0 + "indexed N item(s)"; exit 1 with a clear error when embeddings/Qdrant are unavailable
```

## Connectors (pluggable data sources)

`src/brain/connectors/` is the pluggable source framework: adding a source =
one subclass + one `@register` line. All sources are READ-ONLY this ticket
(gmail.readonly / calendar.readonly / drive.readonly / tasks.readonly);
write-back is a later stage.

**Every connector ships DISABLED**: `BRAIN_CONNECTORS` is empty by default,
so nothing syncs until you explicitly enable sources (comma list, e.g.
`BRAIN_CONNECTORS="email,gcal,gdrive,gtasks"`), and each Google source needs
a **one-time browser consent** (`brain connectors auth google`) before it can
read anything. Two walkthroughs: [docs/connectors-setup.md](docs/connectors-setup.md)
(Google Cloud project, consent screen, the 7-day-Testing gotcha, per-connector
allowlists) and [docs/connectors-enable.md](docs/connectors-enable.md) — the
runbook for turning a connector on for real: consent, then (Calendar and
Gmail only) a one-time `brain connectors adopt <name>` that points the
idempotency ledger at the items you already captured so the first sync does
not duplicate them, then the first sync — by hand or by the opt-in
`com.secondbrain.connectors` launchd scheduler (on `main`; `scripts/service.sh
install` installs it only when `BRAIN_CONNECTORS` names a non-email connector,
and it syncs each enabled connector every `BRAIN_CONNECTORS_INTERVAL_S`,
default 300, with per-source failure isolation).

One OAuth client + one refresh token (keychain item `second-brain-gmail`, the
same one `brain gmail-auth` creates) serves every Google source: consent
requests the UNION of enabled connectors' scopes, so re-running
`brain connectors auth google` after enabling a source grants the extra
scopes (never run consent from tests — the user does it once). All four
connectors are registered on `main` (PRs #55, #56, #57, #59); a source that
is enabled but not available reports "not available" instead of syncing.
On the live instance all four are registered but disabled, and the email +
connectors launchd agents are not installed.

```bash
brain connectors list                    # name, enabled, last ok, cursor, last error
brain connectors sync gtasks --dry-run   # print the records; write NOTHING
brain connectors sync gtasks --limit 100 # one sync cycle through the runner
brain connectors adopt gcal --dry-run    # what adopt would point at whom; writes NOTHING
brain connectors auth google             # consent, union of enabled scopes
```

Runner semantics (per connector, idempotent — safe to re-run): new external
id -> capture via the standard ingest path (`source` = connector name, meta
carries connector/external_id/version/url); same version -> skipped; new
version -> updated in place (text/occurred_at/meta, re-indexed, enrichment
re-enqueued; your own fields are never touched); `deleted=True` -> never
hard-deleted: the item gets `meta.source_deleted=true` and is hidden from
list/search/ask (the superseded-item mechanism). The cursor moves ONLY after
a whole batch succeeds; an auth error stops the connector (`state.last_error`
says re-consent is needed), transient errors back off. `reset=True` batches
are upserted as-is — deletions are never inferred from absent records.

Shipped sources (all READ-ONLY; all disabled until enabled):

| name (items.source) | module | scope | cursor | notes |
| --- | --- | --- | --- | --- |
| `email` (alias `gmail`) | `connectors/google/gmail.py` | gmail.readonly | Gmail `historyId` (history.list, label-filtered); 404 on an expired id -> query fallback + `reset=True` | one immutable Gmail label (`GMAIL_LABEL_ID`, optional `GMAIL_QUERY`); the first sync SEEDS the ledger from the legacy `email_messages` seen-table so old captures never re-ingest; label removed -> deleted; attachments via `fetch_attachment`; the launchd agent (`brain gmail-poll`) drives the same connector through the runner; a cycle that keeps failing sleeps through its persisted stall window via the same seam as a normal interval (PR #101) |
| `gcal` | `connectors/google/calendar.py` | calendar.readonly | per-calendar `nextSyncToken` (JSON cursor with the frozen past cutoff + the rolling future edge) | `singleEvents`+`showDeleted` = one item per occurrence; `BRAIN_GCAL_CALENDARS` allowlist (default `primary`); past reach bounded by `BRAIN_GCAL_PAST_DAYS` (default 7, frozen in the cursor); future window `BRAIN_GCAL_FUTURE_DAYS` (default 60): full walks send `timeMax` at the date-quantized edge stored in the cursor, and when the edge advances the next sync re-lists the whole window (at most once a day) so nothing entering the window is lost; incremental deliveries beyond the edge are dropped and counted (`beyond_window`), never deletions; a moved occurrence arrives as cancelled-old + new id |
| `gtasks` | `connectors/google/tasks.py` | tasks.readonly | `updatedMin` watermark across the allowlisted lists (`BRAIN_GTASKS_LISTS`) | deleted tasks hide their item, completed tasks stay as items; date-only dues anchor at local midnight; subtasks carry their parent |
| `gdrive` | `connectors/google/drive.py` | drive.readonly | `changes.list` `startPageToken` -> `newStartPageToken`; unknown token (404) or truncated window (410) -> `reset=True` | folder allowlist `BRAIN_GDRIVE_FOLDERS` (empty = zero API calls), depth-capped BFS (`BRAIN_GDRIVE_DEPTH`, default 1); Docs/Sheets/Slides -> text with a per-file cap (`BRAIN_GDRIVE_TEXT_CAP`), PDFs/images -> attachments, other binaries -> metadata-only; trashed/moved-out -> `deleted=True` |

### How to add a new source (the recipe, ≤15 lines)

### How to add a new source (the recipe, ≤15 lines)

1. `src/brain/connectors/google/<name>.py`: `class MyConnector(GoogleConnector):`
   with `name = "<source>"` (== the items.source value) and `SCOPES = (...,)`;
   decorate with `@register`.
2. Implement `fetch(cursor, *, limit) -> FetchResult`: list via the injected
   service, map each payload to `SourceRecord` in a pure
   `record_from_<thing>(payload, tz)` function (tz-aware times; Asia/Jerusalem
   in `text`), return `next_cursor` (the source's native incremental token).
3. Cursor expired (410)? Catch `ConnectorRetryableError` with `status == 410`
   and return `reset=True` from scratch — never infer deletions.
4. Tests: unit tests with a fake service (fixtures under
   `tests/fixtures/connectors/<name>/`) + one runner-level test using
   `brain.connectors.testing`. No network in tests, ever.
5. One README row + one settings knob (allowlists) if needed; NO migrations
   (the framework owns them).

## Background services (launchd, 24/7)

`scripts/service.sh` installs per-user LaunchAgents that survive logins and
reboots — API, Telegram bot, WhatsApp poller, an opt-in Gmail agent
(installed only when `GMAIL_LABEL_ID` is set;
[docs/email-setup.md](docs/email-setup.md); never run it in CI — it writes to
`~/Library/LaunchAgents`), and an opt-in connectors scheduler (installed only
when `BRAIN_CONNECTORS` contains a non-email connector):

```bash
scripts/service.sh install     # takeover-first: stops manual instances, then bootstraps
scripts/service.sh status      # per-agent state/pid/last-exit + sleep + docker notes
scripts/service.sh restart     # kickstart -k installed agents (skips the rest)
scripts/service.sh logs [api|telegram|email|connectors|whatsapp]
scripts/service.sh uninstall
```

- **com.secondbrain.api** runs `scripts/run-server.sh` (foreground uvicorn);
  **com.secondbrain.telegram** runs `scripts/run-bot.sh`, **com.secondbrain.email**
  runs `scripts/run-email.sh` (the Gmail connector loop — OPT-IN: installed only
  when `GMAIL_LABEL_ID` is set, else install prints a skipped line and removes any
  stale plist), and **com.secondbrain.whatsapp** runs `scripts/run-whatsapp.sh`.
  All map their exit codes onto launchd's KeepAlive (`SuccessfulExit=false`):
  disabled (0) and permanent config errors (78, incl. the bot's 409 conflict that
  outlasts the 10-minute retry window) exit 0 → no restart; transient failures
  exit 1 → restart with backoff. A needed re-auth pauses inside the email loop
  instead of exiting. The opt-in **com.secondbrain.connectors** runs
  `scripts/run-connectors.sh` (installed only when `BRAIN_CONNECTORS` names a
  non-email connector; email never synced here — its own agent owns it): one
  `brain connectors sync <name>` cycle per connector, a
  `BRAIN_CONNECTORS_INTERVAL_S` (default 300) sleep between passes, the same
  exit mapping at the pass level, and per-connector failure isolation — a
  failing source never blocks the others.
- The single-instance lock is machine-wide and per bot id
  (`~/Library/Caches/second-brain/telegram-<bot_id>.lock`, bot id = the
  token's numeric prefix — never the secret): a second bot instance from any
  checkout exits 78 with `another bot instance is running (pid N)`.
- All agents get `COMPOSE_PROJECT_NAME=second-brain`; secrets stay out of
  the plists (the app reads `.env` itself via its working directory). Logs:
  `~/Library/Logs/second-brain/`.
- Accepted deviation: the API launcher does NOT wait for docker and does NOT
  run alembic at boot. At login uvicorn may start before the docker daemon —
  postgres/qdrant report "down" in the cockpit until docker catches up, and
  restart-on-failure self-heals. Migrations run on deploy (`alembic upgrade
  head`), not on boot.

Note: semantic `/search` ranks candidates per page (`limit + offset` fetched
before ranking), so deep offsets approximate rather than globally rank — fine
for MVP top-k use.

### Telegram bot

`brain telegram` runs a long-polling Telegram bot (no webhook; optional extra:
`pip install -e '.[telegram]'`). Plain messages are captured through the same
paste parser as `POST /capture`; `/search`, `/ask`, `/review`, `/recent`,
`/profile` and `/remember` use the same services as the API in-process; `/status`
shows live per-component health via the `brain.ops` shim — zero bot changes. Access is
allowlist-only: with `TELEGRAM_BOT_TOKEN` set, `TELEGRAM_ALLOWED_USER_IDS`
(comma-separated ids) is required — anyone else gets no reply (their id is
logged once at INFO). `brain telegram --whoami-hint` explains how to find
your id.

Interaction details:

- every capture answers with a card (kind emoji, extracted fields such as
  from/subject/when/location, type · importance · actionability · status;
  HTML-escaped) carrying inline buttons **File / Type / Delete / Undo** —
  valid for 10 minutes, callback data stays under 64 bytes, stale or unknown
  buttons are answered gracefully (no crash, no state change).
- a multi-message WhatsApp thread is **not saved immediately**: the bot asks
  "Save as 1 item / Save separately" first (the paste is held in memory,
  expiring after 10 minutes).
- forwarded messages record `meta.sender` (an `@username`, "First Last",
  or chat title — resolved locally from the forward origin, no extra API
  call) + `meta.forwarded = true` + `meta.sender_id`, and use the forward
  date as `occurred_at` (source stays the parsed kind).
- any text ending in `?` (without a slash) is an ask; the bot shows the
  TYPING indicator while waiting and answers with the model name plus one
  "Open" button per cited item.
- `/review` walks the inbox one card at a time (File / Type / Delete / Next);
  `/recent` lists the last 10 items; `/profile` shows the kids + remembered
  facts with a ✖ remove button per fact; `/remember <text>` stores a
  confirmed free-text fact; bot commands are registered via
  `set_my_commands` at startup.
- documents: `.ics` files are captured as calendar items; other files,
  photos and voice get a "not supported yet" reply.

Exit codes (launchd service contract):

| Code | Meaning |
| ---- | ------- |
| `0`  | `TELEGRAM_BOT_TOKEN` unset — logs "telegram bot disabled (no token)"; the service stays idle, no restart loop |
| `1`  | transient failure (network, etc.) — the service restarts |
| `78` | `EX_CONFIG`: getUpdates 409 conflict (a second poller on the same token) or an invalid/unauthorized token — fix the configuration, do not restart-loop |

The shared AI clients are app-lifetime singletons built in
`src/brain/api/deps.py` (implementations picked by `LLM_PROVIDER` /
`EMBEDDER_PROVIDER` in `src/brain/factories.py`) and closed on shutdown via
the app lifespan. If the Qdrant collection was built with a different
embedder than the active one, indexing fails with a clear message — run
`brain reindex` to drop and rebuild it.

## Dogfood checklist (live run without API keys)

Exact steps to exercise the AI features with ONLY a Jev key and your Claude
Code login. `scripts/dev-start.sh` collapses steps 1-3 (see
"Local dev start-up" above):

```bash
# 1. Infrastructure + schema + server (also creates .venv/.env if missing,
#    checks your Claude login, reuses or starts healthy containers)
scripts/dev-start.sh

#    ...or by hand:
# docker compose up -d && curl -s localhost:27333/healthz && alembic upgrade head
# uvicorn brain.main:app --port 27800

# 2. Keys: JEV_API_KEY in .env (gitignored) — optional but needed for
#    classification/rerank. LLM + embeddings come from your Claude Code
#    login and local fastembed; no OPENAI_API_KEY / ANTHROPIC_API_KEY needed.
#    The script reports both checks; to fix login manually:
claude -p ok            # verifies the Claude Code login; run `claude` + /login if it fails

# 3. Run the API (first /search or /items call downloads the fastembed model,
#    ~130MB, cached in ~/.cache/fastembed — afterwards fully offline)
uvicorn brain.main:app --port 27800

# 4. Capture with auto-classification (Jev fills type/importance/actionability,
#    confidence, and routes by confidence; fields you sent explicitly in the
#    payload are never overwritten — classification only fills what's missing)
curl -s -X POST localhost:27800/items -H 'Content-Type: application/json' \
  -d '{"raw_text": "Renew the TLS certificate before Friday"}'
# expect 201: "type": "task", "importance": 1-5, "actionability": "none|later|now",
#             "confidence": 0.0-1.0, "status": "filed" (>=0.9) or "needs_review" (0.5-0.9)

# 5. Semantic search (fastembed -> Qdrant -> Jev rerank)
curl -s 'localhost:27800/search?q=renew+certificate&limit=5'
# expect 200: array of items with float "score" (null only in keyword-fallback mode)

# 6. Grounded answer with citations (via your Claude Code login)
curl -s -X POST localhost:27800/ask -H 'Content-Type: application/json' \
  -d '{"question": "what do I need to renew?"}'
# expect 200: {"answer": "...[<item-id>]...", "cited_ids": ["<item-id>"], "sources": [...]}

# 7. Rebuild the vector index from Postgres
brain reindex
# expect: "indexed N item(s) into collection 'brain_items"' and exit 0
```

Degradation reference (all verified by tests): Jev unset -> `/items` still
returns 201 with `type: null, status: inbox`; Jev rerank down -> original
order; `claude` CLI not logged in -> `/ask` returns 502; `brain reindex`
exits 1 with a clear message. Fully keyless run (no JEV_API_KEY either):
capture still works, `/search` scores real fastembed vectors without rerank
reordering, `/ask` still answers from your login.

## Local dev start-up

`scripts/dev-start.sh` does the whole dance with preflight checks; `scripts/dev-stop.sh` stops it:

```bash
scripts/dev-start.sh             # preflight (docker, venv, .env, jev/claude status),
                                 # compose up + health wait, alembic, uvicorn on :27800
scripts/dev-start.sh --reindex   # also rebuild the vector collection first
scripts/dev-start.sh --no-reload # production-ish uvicorn (no file watching)
scripts/dev-start.sh --force-port# kill a FOREIGN port holder without asking
scripts/dev-stop.sh              # stop uvicorn (pid file)
scripts/dev-stop.sh --down       # also stop postgres + qdrant containers
scripts/dev-stop.sh --purge-data # ALSO delete volumes — asks for confirmation
```

Behavior worth knowing:

- **Self-healing restart** — running `dev-start.sh` when our uvicorn is
  already running (pid file or orphan on the port) stops it first (TERM,
  wait, KILL escalation — the same shared logic `dev-stop.sh` uses) and
  starts fresh. Running it twice in a row is safe and just restarts.
- **Port holders** — if the app port is held by a FOREIGN process, the
  script shows pid + command and asks `kill it? [y/N]` (refusing by default
  when non-interactive); `--force-port` kills without asking. Protected
  processes are NEVER killed: docker/rancher/colima/vpnkit proxies,
  anything on the infra ports (those are the reuse path below), pid 1, or the
  script's own shell.
- **Stable compose project** — both scripts resolve `COMPOSE_PROJECT_NAME`
  identically (`scripts/lib.sh`: real environment > `.env` > default
  `second-brain-dev`) and print it before any docker call, so a stop can never
  target another project's volumes. Container names never drift.
- **Port collisions with other compose projects** — if the dedicated ports (27432/27333) are already
  published by a foreign compose project, the script says who holds it and
  REUSES the healthy service (verified against `DATABASE_URL` / `QDRANT_URL`)
  instead of touching another project's containers; if what's on the port is
  not healthy/compatible it fails with exact instructions. It never removes
  other projects' containers.
- **Preflight** — creates `.venv` + `pip install -e '.[dev]'` if missing;
  creates `.env` from `.env.example` if absent; reports JEV key as set/unset
  and checks the `claude` CLI is installed (no LLM round-trip — if `/ask`
  returns 502, run `claude` once and `/login`). `JEV_BASE_URL` is optional —
  the default works.
- **State** — pid + log live under `.dev/` (gitignored). A busy port is
  never silently taken over (see self-healing above). `PORT` overrides the
  default 27800. Stopping kills the whole uvicorn tree (no orphans under
  `--reload`).
- **Data purge** — `scripts/dev-stop.sh --purge-data` deletes the resolved
  project's docker volumes (all items + vectors), naming the project and each
  volume in the confirmation prompt (`PURGE` to proceed).
- **`.env` loading in the scripts** — plain `KEY=VALUE` lines; surrounding
  quotes stripped; inline `#` comments and trailing whitespace are NOT
  stripped (keep values comment-free). The real environment always wins over
  `.env`, matching pydantic-settings. Secrets are never printed.
- **Live-store startup guard** — Settings' defaults ARE the live service
  (database `brain` on localhost:27432, collection `brain_items`), so a bare
  `uvicorn brain.main:app` from a worktree without `.env` would silently
  write to the live DB. The app therefore refuses to start against the live
  store (`brain.config.assert_not_live_store`, checked in the app lifespan):
  it raises `refusing to start against the LIVE store; set
  DATABASE_URL/QDRANT_COLLECTION for a test instance, or BRAIN_ALLOW_LIVE=1
  for the real service`. The launchd path (`scripts/run-server.sh`) exports
  `BRAIN_ALLOW_LIVE=1`; a local instance must instead set an explicit
  `DATABASE_URL` (a `*_test` database) and `QDRANT_COLLECTION`, e.g.
  `DATABASE_URL=postgresql+psycopg://brain:brain@localhost:27432/brain_test
  QDRANT_COLLECTION=brain_items_test uvicorn brain.main:app` — and stop it
  by its own PID or port (`kill $(lsof -ti tcp:<port>)`), never by pattern.
  Only the `BRAIN_ALLOW_LIVE=1` opt-in runs against the live store. To run
  `dev-start.sh` against the real store you opt in explicitly with
  `BRAIN_ALLOW_LIVE=1 scripts/dev-start.sh`.
- **Live-store construction guard** — the lifespan guard cannot catch code
  that never goes through an app start (a `TestClient(create_app())` without
  a `with` block, an ad-hoc script, a lru_cache'd factory that outlived an
  env change). The SAME refusal therefore also runs at CONSTRUCTION: the
  SQLAlchemy engine factory (`brain.db.session.create_engine_from_settings`)
  refuses the live database URL, and the Qdrant vector store's `__init__`
  refuses the live collection name when it builds a real client (stores with
  an injected client — the tests' in-memory qdrant — are never the live
  server and are not gated). Consequence for the CLI: every `brain`
  subcommand that touches the live store (`reindex`, `enrich`, `whatsapp`,
  `gmail-poll`, `memory seed-*`, `telegram`) now refuses unless the operator
  sets `BRAIN_ALLOW_LIVE=1` for that one command — e.g.
  `BRAIN_ALLOW_LIVE=1 brain reindex`. `scripts/run-bot.sh` exports the flag
  itself (the bot is the real service), as does `scripts/run-server.sh`.
  The test suite additionally runs a session-level tripwire
  (`tests/live_guard.py`): it snapshots the LIVE collection's point count
  before and after the run (one read-only REST GET each) and fails loudly if
  it changed — a future leak turns red instead of surfacing days later.

## Setup

Prerequisites: Python 3.11, Docker (compose v2). The scripts above do all of
this manually:

```bash
# 1. Infrastructure: Postgres + Qdrant
docker compose up -d

# 2. Python environment
python3.11 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"

# 3. Configuration
cp .env.example .env    # adjust if needed; never commit .env
#   WORKTREES (not your main checkout): point DATABASE_URL at a `*_test`
#   dbname (e.g. .../brain_test) — the default in .env.example is the live
#   service's database, and a bare `alembic` run from this directory uses it.

# 4. Schema
alembic upgrade head

# 5. Run
uvicorn brain.main:app --reload
# Health check: http://127.0.0.1:27800/health
```

## Config / env vars

Copy `.env.example` to `.env`. Never commit `.env`.

| Variable       | Default                                                | Purpose            |
| -------------- | ------------------------------------------------------ | ------------------ |
| `APP_ENV`      | `dev`                                                  | runtime environment |
| `DATABASE_URL` | `postgresql+psycopg://brain:brain@localhost:27432/brain` | Postgres (SQLAlchemy URL) |
| `QDRANT_URL`   | `http://localhost:27333`                                | Qdrant API endpoint |
| `LLM_PROVIDER` | `claude_sdk`                                           | `claude_sdk` (local login) or `anthropic` |
| `CLAUDE_SDK_MODEL` | unset (login default)                              | model for `/ask` on `claude_sdk`, e.g. `claude-haiku-5-5` (faster) / `claude-sonnet-5-5` (quality) |
| `ANTHROPIC_MODEL` | `claude-sonnet-5-5`                                 | model for `/ask` on `anthropic` |
| `EMBEDDER_PROVIDER` | `fastembed`                                       | `fastembed` (local) or `openai` |
| `TIMEZONE` | machine's local IANA zone (e.g. `Asia/Jerusalem`)   | interprets zone-less pasted times (Gmail/WhatsApp/calendar) before storing them as aware UTC |
| `TELEGRAM_BOT_TOKEN` | — (SecretStr; optional) | enables `brain telegram` (long-poll Telegram bot; needs `pip install -e '.[telegram]'`) |
| `TELEGRAM_ALLOWED_USER_IDS` | — (REQUIRED when the token is set) | comma-separated Telegram user ids; the bot answers only these users (others get no reply) and refuses to start without this list |

All settings live in `Settings` (`src/brain/config.py`); AI-layer variables
(keys as `SecretStr`, base URLs, models, timeouts) are listed in
[docs/ai-layer.md](docs/ai-layer.md) and `.env.example`. None are required to
boot the app; `classify`/`rerank` fall back gracefully when Jev is
unconfigured.

## Tests

```bash
pytest            # unit + API tests
pytest -v         # verbose
ruff check src tests   # lint (E, F, I, B, UP; config in pyproject [tool.ruff])
mypy src/brain/ai      # type check (strict-ish; config in pyproject [tool.mypy])
```

`tests/test_health.py` needs no infrastructure. DB-backed tests
(`tests/test_items.py`) need the docker-compose Postgres running (they apply
Alembic migrations and clean the table after each test; they skip with a hint
if Postgres is unreachable).

The suite NEVER touches a developer's database: `tests/conftest.py` pins
`DATABASE_URL` to a separate `brain_test` database (created on demand),
refuses to run at all against a non-`*_test` URL, and the destructive
`clean_items` teardown double-checks the engine URL in use before deleting.
`tests/test_db_isolation.py` proves the point: it seeds a marker row in the
dev database, runs the DB-backed suite, and asserts the marker survives.

**Per-worker Qdrant collections.** The suite also refuses non-`*_test`
Qdrant collections — and when you leave `QDRANT_COLLECTION` unset, it DERIVES
the collection from the test database name (`brain_w5_test` ->
`brain_items_w5_test`, `brain_test` -> `brain_items_test`), so two workers
testing against different test databases never share one vector collection.
Previously every suite silently pinned the same `brain_items_test`, and
concurrent runs polluted each other's vectors (a real incident). Run two
suites side by side by pointing each at its own `DATABASE_URL` and letting
the collection follow, e.g. `DATABASE_URL=.../brain_w5_test pytest`;
CI's `brain_test` keeps the classic `brain_items_test`. If your shell
exports `QDRANT_COLLECTION` explicitly, unset it — an explicit value
bypasses the derivation.

CI runs the same commands on every PR and push to main (GitHub Actions,
`.github/workflows/ci.yml`): ruff, mypy, shellcheck, and pytest against
Postgres 16 + Qdrant service containers — no API keys or Claude login
needed (the suite uses fakes and mock transports, never live providers).

## Adding a feature

1. Model changes: edit `src/brain/db/models.py`, then
   `alembic revision --autogenerate -m "..."` and review the generated script;
   `alembic upgrade head` to apply.
2. Data access: add/extend a repository in `src/brain/repositories/`.
3. Business logic: add/extend a service in `src/brain/services/`.
4. HTTP surface: add a thin router in `src/brain/api/routes/` and register it
   in `create_app()` (`src/brain/main.py`).
5. Tests next to the behavior in `tests/`.
6. Document the feature in this README (or `docs/<feature>.md` linked from
   here) in the same change. If the change affects anything described in
   [docs/how-it-works.html](docs/how-it-works.html) (flows, components, data
   model, degradation behavior, security model), update that page in the same
   change so it stays an accurate picture of the code.

## Git rules

- Work on feature branches (e.g. `feature/<topic>`); never push directly to
  `main`; never force-push or rewrite history.
- Keep secrets out of the repo: local config lives in `.env` (gitignored);
  `.env.example` documents the shape.

## Next steps

For the next worker. Anything marked **PROPOSAL** is an idea, not a decided
behavior.

### 1. Landed — hardening (PR #3 + follow-ups)

All of the hardening items below are merged into the hardening branch and
submitted to main in one PR:

- Landed in PR #3: search N+1 → `get_many`; dead `ItemsService.create` deleted;
  UUID guard in `_load_items`; deterministic list ordering; Qdrant healthcheck
  in compose; dev-start self-healing restart; test DB isolation; seed-demo
  script; `/ask` source truncation.
- Landed on top of PR #3: **reindex batching** (one `embed_batch` + one
  `upsert_many` per DB batch, identical final Qdrant state), **embedder probe
  before purge** (an EmbedderError in `reindex` now hits before the collection
  is dropped), removal of the config-default re-export shims in `ai/`
  (`brain.config` is the single source), and `rerank(jev=...)` made a required
  dependency (the ValueError guard is gone).


### 2. Known limitations / tech debt (on main)

| Limitation | Detail | Where |
| --- | --- | --- |
| Jev never returns `project` | the classifier question needs rewording, or a dedicated choice over known projects | `src/brain/ai/classify.py` |
| Rerank reorders, score stays cosine | expose the rerank score alongside the vector score | `src/brain/ai/rerank.py`, `src/brain/services/search.py` |
| Offset pagination is approximate | `/search` and reindex assume a quiet window; concurrent writes shift pages | `src/brain/services/search.py`, `src/brain/services/ingest.py` |
| No auth | single user, localhost only — never expose publicly | `src/brain/main.py` |
| Status transitions not enforced | any PATCH can set any status; no inbox→filed workflow rules | `src/brain/db/models.py`, `src/brain/services/items.py` |
| No retries/backoff | external calls fail fast by design; revisit when an adapter needs resilience | `src/brain/ai/_http.py` |
| fastembed first-run download | ~130MB model download on first capture/search, then offline | `src/brain/ai/embedder.py` |
| Qdrant image pinned to client | image tag and qdrant-client version move together | `docker-compose.yml` |
| Compose port collisions | two projects on the dedicated ports (27432/27333): dev-start reuses healthy services or fails loudly | `scripts/dev-start.sh` |

### 3. Roadmap — phases 4-6 (from the project plan)

**Phase 4 — Knowledge Intelligence.** Detect relationships between items (duplicate / related /
supports / contradicts / supersedes) with Jev over top-k neighbours at ingest time; store in a new
relations table.
1. Relations table + migration: `src/brain/db/models.py` + `alembic revision` (FK to `memory_items`,
   relation-type CHECK). *Accept:* migration up/down round-trips.
2. Relation classifier: new module in `src/brain/ai/` over `JevClientProtocol`, called from the
   existing ingest seam (`src/brain/services/ingest.py` `_apply_classification`). *Accept:* capturing
   a near-duplicate of an existing item records a `duplicate` relation.
3. Read API: `GET /items/{id}/relations` in `src/brain/api/routes/items.py`. *Accept:* typed links
   returned for a seeded pair.

**Phase 5 — External Integrations.** Adapters per source (Gmail, Drive, Calendar, Slack, notes,
tasks) feeding the existing `IngestService` with `source=<adapter>`; dedupe via Phase-4 relations.
1. Adapter Protocol + registry: new `src/brain/adapters/`, reusing the Protocol/Factory seams
   (`ClassifierProto`, `ItemIndexerProto` show the pattern; `factories.py` for wiring). *Accept:* an
   adapter can be selected by config without touching services.
2. First adapter (notes/files): import plain files via `IngestService.create` with
   `source="notes"` (the free-form `source` column already supports this). *Accept:* imported note
   is searchable and shows `source=notes`.
3. Dedupe at ingest: check Phase-4 relations before insert; skip or link duplicates. *Accept:*
   re-importing the same file creates no second item.

**Phase 6 — Actions & Agents.** From passive store to active assistant: task creation, calendar
actions, email drafting with an approval flow, morning summaries / weekly reviews; LangGraph only
where a multi-step agent earns its place.
1. Task queue: surface `type=task` items as actionable rows (new table or view over `memory_items`;
   `src/brain/db/models.py`). *Accept:* open tasks listed newest-first with status.
2. Daily summary: CLI/endpoint that asks `AskService` over today's items (seam:
   `src/brain/services/ask.py`). *Accept:* `brain summary` returns a cited digest.
3. Email drafting behind an approval gate: drafts stored as items, never auto-sent. *Accept:* a
   draft requires explicit confirmation before any send action exists.

### 4. Dogfood & feedback (PROPOSAL)

Use Second Brain for real notes for a week, then decide the routing thresholds from data instead of
the current guesses (0.9/0.5 in `src/brain/ai/classify.py`). **PROPOSAL** for what to log/count:
confidence histogram of `confidence` (Postgres column), share of items landing `needs_review` vs
`filed` vs `inbox`, and manual mis-route tallies; re-pick `ROUTE_FILE_THRESHOLD`/`ROUTE_REVIEW_THRESHOLD`
from that distribution.

### 5. Where to start tomorrow

1. Review + merge the open hardening PR (section 1 — PR #3's work plus
   reindex batching, the embedder probe, and the ai-layer cleanups).
2. Add the CI workflow (section 2) so everything above gets enforced automatically.
3. Start the dogfood week (section 4) with real notes.
4. Slice the first Phase-4 ticket (section 3) once hardening has landed.
