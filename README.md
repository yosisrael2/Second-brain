# Second Brain

An AI-powered personal knowledge base: capture raw thoughts, organize them into
typed, searchable memory items, and retrieve them with keyword and semantic
search.

Roadmap, known limitations, and where to pick up work: see [Next steps](#next-steps).

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
├── ai/                  # AI integrations (owned by AI/integrations track)
├── api/
│   ├── deps.py          # FastAPI dependency providers (service composition)
│   └── routes/          # thin routers: health.py, items.py, search.py, ask.py
├── services/            # business logic: items, ingest, search, ask
├── repositories/        # data access: items.py
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
| GET    | `/search?q=`       | Semantic search (embed -> Qdrant -> Jev rerank); falls back to keyword search (`score: null`) when embeddings/Qdrant are unavailable |
| POST   | `/ask`             | Answer a question from top-k items with citations; 502 when the LLM is unconfigured/down |

Example:

```bash
curl -X POST localhost:8000/items -H 'Content-Type: application/json' \
  -d '{"raw_text": "Renew the ssl cert", "type": "task", "importance": 5}'
curl 'localhost:8000/items?type=task&status=inbox'
curl 'localhost:8000/items/search?q=renew+cert'
curl -X PATCH localhost:8000/items/<id> -H 'Content-Type: application/json' -d '{"status": "filed"}'
curl -X DELETE localhost:8000/items/<id>
# AI endpoints (see docs/ai-layer.md for behavior + env vars)
curl -X POST 'localhost:8000/items?classify=false' -H 'Content-Type: application/json' \
  -d '{"raw_text": "raw capture, no classification"}'
curl 'localhost:8000/search?q=how+do+I+roll+out+kubernetes'
curl -X POST localhost:8000/ask -H 'Content-Type: application/json' \
  -d '{"question": "what port does the server run on?"}'
```

Tests: `tests/test_items.py` (needs the compose Postgres; skipped with a hint
if it is down). Wiring tests run fully offline (`tests/test_wiring.py`).

## AI layer and the `brain` CLI

Auto-classification on `POST /items` (Jev), semantic `/search` (embeddings +
Qdrant + rerank), and grounded `/ask` are optional at runtime: with their API
keys unset the app still captures and keyword-searches — degradations and
failure modes are documented in [docs/ai-layer.md](docs/ai-layer.md).

**Default providers need no API keys**: the LLM runs on your local Claude Code
login (`claude_agent_sdk`, check with `claude -p ok` — run `claude` and
`/login` if needed) and embeddings run locally via `fastembed`
(`BAAI/bge-small-en-v1.5`, 384 dims; the model downloads once on first use).
Switch back to keyed providers with `LLM_PROVIDER=anthropic` /
`EMBEDDER_PROVIDER=openai` (see `.env.example`).

Rebuild the vector index from Postgres (covers items that missed indexing
while Jev/Qdrant were down, and purges ghost vectors for items no longer in
Postgres — it drops and rebuilds the collection):

```bash
brain reindex [--batch-size 500]
# exit 0 + "indexed N item(s)"; exit 1 with a clear error when embeddings/Qdrant are unavailable
```

Note: semantic `/search` ranks candidates per page (`limit + offset` fetched
before ranking), so deep offsets approximate rather than globally rank — fine
for MVP top-k use.

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
# docker compose up -d && curl -s localhost:6333/healthz && alembic upgrade head
# uvicorn brain.main:app --port 8000

# 2. Keys: JEV_API_KEY in .env (gitignored) — optional but needed for
#    classification/rerank. LLM + embeddings come from your Claude Code
#    login and local fastembed; no OPENAI_API_KEY / ANTHROPIC_API_KEY needed.
#    The script reports both checks; to fix login manually:
claude -p ok            # verifies the Claude Code login; run `claude` + /login if it fails

# 3. Run the API (first /search or /items call downloads the fastembed model,
#    ~130MB, cached in ~/.cache/fastembed — afterwards fully offline)
uvicorn brain.main:app --port 8000

# 4. Capture with auto-classification (Jev fills type/importance/actionability,
#    confidence, and routes by confidence; fields you sent explicitly in the
#    payload are never overwritten — classification only fills what's missing)
curl -s -X POST localhost:8000/items -H 'Content-Type: application/json' \
  -d '{"raw_text": "Renew the TLS certificate before Friday"}'
# expect 201: "type": "task", "importance": 1-5, "actionability": "none|later|now",
#             "confidence": 0.0-1.0, "status": "filed" (>=0.9) or "needs_review" (0.5-0.9)

# 5. Semantic search (fastembed -> Qdrant -> Jev rerank)
curl -s 'localhost:8000/search?q=renew+certificate&limit=5'
# expect 200: array of items with float "score" (null only in keyword-fallback mode)

# 6. Grounded answer with citations (via your Claude Code login)
curl -s -X POST localhost:8000/ask -H 'Content-Type: application/json' \
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
                                 # compose up + health wait, alembic, uvicorn on :8000
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
  anything on 5432/6333 (those are the reuse path below), pid 1, or the
  script's own shell.
- **Stable compose project** — both scripts resolve `COMPOSE_PROJECT_NAME`
  identically (`scripts/lib.sh`: real environment > `.env` > default
  `second-brain-dev`) and print it before any docker call, so a stop can never
  target another project's volumes. Container names never drift.
- **Port collisions with other compose projects** — if 5432/6333 are already
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
  default 8000. Stopping kills the whole uvicorn tree (no orphans under
  `--reload`).
- **Data purge** — `scripts/dev-stop.sh --purge-data` deletes the resolved
  project's docker volumes (all items + vectors), naming the project and each
  volume in the confirmation prompt (`PURGE` to proceed).
- **`.env` loading in the scripts** — plain `KEY=VALUE` lines; surrounding
  quotes stripped; inline `#` comments and trailing whitespace are NOT
  stripped (keep values comment-free). The real environment always wins over
  `.env`, matching pydantic-settings. Secrets are never printed.

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
# Health check: http://127.0.0.1:8000/health
```

## Config / env vars

Copy `.env.example` to `.env`. Never commit `.env`.

| Variable       | Default                                                | Purpose            |
| -------------- | ------------------------------------------------------ | ------------------ |
| `APP_ENV`      | `dev`                                                  | runtime environment |
| `DATABASE_URL` | `postgresql+psycopg://brain:brain@localhost:5432/brain` | Postgres (SQLAlchemy URL) |
| `QDRANT_URL`   | `http://localhost:6333`                                | Qdrant API endpoint |
| `LLM_PROVIDER` | `claude_sdk`                                           | `claude_sdk` (local login) or `anthropic` |
| `CLAUDE_SDK_MODEL` | unset (login default)                              | model for `/ask` on `claude_sdk`, e.g. `claude-haiku-5-5` (faster) / `claude-sonnet-5-5` (quality) |
| `ANTHROPIC_MODEL` | `claude-sonnet-5-5`                                 | model for `/ask` on `anthropic` |
| `EMBEDDER_PROVIDER` | `fastembed`                                       | `fastembed` (local) or `openai` |
| `TIMEZONE` | machine's local IANA zone (e.g. `Asia/Jerusalem`)   | interprets zone-less pasted times (Gmail/WhatsApp/calendar) before storing them as aware UTC |

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
| Compose port collisions | two projects on 5432/6333: dev-start reuses healthy services or fails loudly | `scripts/dev-start.sh` |

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
