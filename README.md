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

Everything below is merged and running on `main`. One line per capability,
linked to its section or doc.

**Capture**
- Telegram bot (long-polling, allowlist-only) and `POST /capture` API, both feeding the same paste parser — [Telegram bot](#telegram-bot), [Paste-aware capture](#memory-items-api)
- Paste recognition: email, WhatsApp thread, calendar one-liner, or plain note; WhatsApp threads ask "save as one item or separately" first — `src/brain/services/paste.py`
- Jev auto-classification (type / importance / actionability / confidence, routed at >= 0.9) with exact-duplicate guard (content fingerprint) — [Dogfood checklist](#dogfood-checklist-live-run-without-api-keys)
- `.ics` calendar files captured as calendar items — [Telegram bot](#telegram-bot)

**Retrieval**
- Multilingual embeddings (local `fastembed`, `minishlab/potion-multilingual-128M`, 256 dims — Hebrew + English), Postgres FTS over raw text, reciprocal-rank fusion of the two — [AI layer](#ai-layer-and-the-brain-cli)
- Temporal retrieval: English + Hebrew weekday names, `dd.mm` dates, today/tomorrow/yesterday — `src/brain/services/temporal.py`
- Ask recall pool: rerank over 32 candidates, top 8 in the prompt — [Memory items API](#memory-items-api)

**Ask**
- Grounded answers with item-id citations; model visible in the reply (`claude-sonnet-5-5` default) — [Memory items API](#memory-items-api), [docs/ai-layer.md](docs/ai-layer.md)
- Conflict rule: when two sources disagree about the same event, the answer opens with the conflict, quotes both versions with citations, and never claims "no conflict" — `src/brain/ai/ask.py` (PR #27)

**Memory layer**
- Entities (person / group / place / org + aliases) and versioned confirmed facts (`valid_from`/`valid_to`, `proposed -> confirmed -> rejected`, supersedes chain) — [Memory layer API](#memory-layer-api)
- Kid/class/teacher profiles, channel -> child mapping and `applies_to` resolution; memory block injected into ask answers — `src/brain/services/memory.py`
- REST `/memory` API, cockpit Memory tab, bot `/profile` and `/remember`, seed CLI (`brain memory seed-kid`, `seed-channel`) — [Memory layer API](#memory-layer-api)

**Cockpit**
- One local dashboard: flow diagram, health, events, stats, debug traces, memory; KPI row; light + dark; laptop + phone — [Cockpit](#cockpit-ops-dashboard)

**Ops**
- 24/7 launchd services (`scripts/service.sh`), single bot instance lock, live-store startup guard (`BRAIN_ALLOW_LIVE`), per-worker test databases, suite isolated from the live store — [Background services](#background-services-launchd-247), [Live-store startup guard](#live-store-startup-guard)

**Evaluation**
- `brain eval-extraction`: gold-set scorer for extraction candidates (per-type P/R/F1, date accuracy, applies_to, quote grounding, relations, `date_conflict` recall) — [docs/eval-extraction.md](docs/eval-extraction.md)

## Status and progress

| Area | Status | PRs |
| --- | --- | --- |
| Capture pipeline (items, paste parsing, Jev, dedupe) | **Done** | #1, #7, #17, #19 |
| Retrieval (embeddings, FTS fusion, temporal, recall pool) | **Done** | #20, #21, #24 |
| Ask (citations, model config, conflict rule) | **Done** | #6, #27 |
| Memory layer (entities, versioned facts, ask block) | **Done** | #26, #28, #30 |
| Telegram bot + launchd services | **Done** | #9-#12, #16, #18 |
| Cockpit (ops dashboard + restyle) | **Done** | #13, #29 |
| Ops safety (test isolation, live-store guard, dedicated ports) | **Done** | #10, #22, #23, #31 |
| CI + extraction evaluation | **Done** | #4, #5, #25 |

**In progress**

- Meaning extraction PR A1: data model + async enrichment of captured items
- Memory-aware recall for kid questions (PR 0d)
- Attachments (PR F1): images/PDFs read by Claude (a PDF text layer is read locally first); Telegram first, then email and WhatsApp. PR F3 wires email + WhatsApp IN: the Gmail poller fetches stored attachment references (`users.messages.attachments.get`, still read-only) and WhatsApp downloads media for allowlisted channels via the bridge's local `/api/download` — both captured as their own attachment item (20 MB / daily-budget / sandbox respected), replacing the "not read yet" markers
- Gmail source (PR F2): approved (read-only Gmail OAuth, one label); implementation in progress; needs a one-time Google consent by the user (setup: docs/email-setup.md)
- WhatsApp ingestion (PR D1): automatic reading of allowlisted channels, grouped into conversations
- WhatsApp relevance triage (PR D2a, item 36): every conversation is labelled `relevant` | `noise` — Jev's typed choice first, the LLM only below the 0.9 confidence band (one strict, versioned call per conversation). Noise stays stored and searchable but is hidden from the default listings and never enqueued for enrichment; triage never deletes anything, runs on conversation create and update (a conversation can turn relevant), never blocks the poll, and failures are recorded (ops). `brain whatsapp-triage` backfills untriaged conversations (guarded like every live CLI). Health line: `triage: relevant N / noise M today`.
- Bot timezone fix
- Bot proposal cards (PR C): Keep / Fix / Drop on one card per source + 48h pings — built; fires only when the enrichment worker is switched on (worker stays OFF for now)

**Planned**

- PR A2: LLM extraction scored against the frozen gold set + held-out data
- PR B: relations between items + duplicate handling
- PR D: WhatsApp triage, reconciliation, media
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

Diagnostics note: live checks that exercise the ask path with a FAKE LLM must
call `AskService.ask(..., record_ops=False)` — that skips the `ask→search` /
`ask→llm` spans and the kid_recall event, so `/ops/health`'s llm component is
not marked "last call failed" until the next real call. Real requests keep
the default (`record_ops=True`); nothing else changes.

Health components `attachments` (queue depth, read today / daily budget, last
error) and `enrichment` (queue depth, cap used / limit, worker on/off — off is
the designed inert state) mirror `GET /ops/attachments` and `GET
/ops/enrichment`; the cockpit renders their cards automatically. The telegram
heartbeat threshold follows the bot's actual long-poll timeout
(`telegram_poll_timeout_s` + margin), and the poll loop heartbeats on the
conflict/network retry paths too — a quiet chat no longer flaps "degraded".

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

## Background services (launchd, 24/7)

`scripts/service.sh` installs two per-user LaunchAgents that survive logins
and reboots (never run it in CI — it writes to `~/Library/LaunchAgents`):

```bash
scripts/service.sh install     # takeover-first: stops manual instances, then bootstraps
scripts/service.sh status      # per-agent state/pid/last-exit + sleep + docker notes
scripts/service.sh restart     # kickstart -k both agents
scripts/service.sh logs [api|telegram]
scripts/service.sh uninstall
```

- **com.secondbrain.api** runs `scripts/run-server.sh` (foreground uvicorn);
  **com.secondbrain.telegram** runs `scripts/run-bot.sh`, which maps the bot's
  exit codes onto launchd's KeepAlive (`SuccessfulExit=false`): disabled (0)
  and permanent config errors (78, incl. a 409 conflict that outlasts the
  10-minute retry window) exit 0 → no restart; transient failures exit 1 →
  restart with backoff.
- The single-instance lock is machine-wide and per bot id
  (`~/Library/Caches/second-brain/telegram-<bot_id>.lock`, bot id = the
  token's numeric prefix — never the secret): a second bot instance from any
  checkout exits 78 with `another bot instance is running (pid N)`.
- Both agents get `COMPOSE_PROJECT_NAME=second-brain`; secrets stay out of
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
