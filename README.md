# Second Brain

An AI-powered personal knowledge base: capture raw thoughts, organize them into
typed, searchable memory items, and retrieve them with keyword and semantic
search.

## Stack

- Python 3.11, FastAPI, Pydantic v2
- SQLAlchemy 2 + Alembic, Postgres 16
- Qdrant (vector search)
- pytest, docker compose

## Architecture

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
Code login:

```bash
# 1. Infrastructure + schema (Postgres + Qdrant 1.19.1, matching qdrant-client)
docker compose up -d
curl -s localhost:6333/healthz          # -> "healthz check passed"
alembic upgrade head

# 2. Keys: JEV_API_KEY in .env (gitignored) — optional but needed for
#    classification/rerank. LLM + embeddings come from your Claude Code
#    login and local fastembed; no OPENAI_API_KEY / ANTHROPIC_API_KEY needed.
cp .env.example .env    # then fill JEV_API_KEY
claude -p ok            # verifies the Claude Code login; run `claude` + /login if it fails

# 3. Run the API (first /search or /items call downloads the fastembed model,
#    ~130MB, cached in ~/.cache/fastembed — afterwards fully offline)
uvicorn brain.main:app --port 8000

# 4. Capture with auto-classification (Jev fills type/importance/actionability,
#    confidence, and routes by confidence)
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

## Setup

Prerequisites: Python 3.11, Docker (compose v2).

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
| `EMBEDDER_PROVIDER` | `fastembed`                                       | `fastembed` (local) or `openai` |

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
Alembic migrations to the configured `DATABASE_URL` and clean the table after
each test; they skip with a hint if Postgres is unreachable).

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
   here) in the same change.

## Git rules

- Work on feature branches (e.g. `feature/<topic>`); never push directly to
  `main`; never force-push or rewrite history.
- Keep secrets out of the repo: local config lives in `.env` (gitignored);
  `.env.example` documents the shape.
