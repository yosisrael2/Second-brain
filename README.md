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
- **Strategy (planned)**: `src/brain/ai/` hosts future Embedder/LLMClient
  implementations behind abstractions — owned by the AI/integrations track.

## Folder layout

```
src/brain/
├── config.py            # env-based settings (pydantic-settings)
├── main.py              # FastAPI app factory + ASGI entrypoint (app = create_app())
├── schemas.py           # pydantic request/response models
├── ai/                  # AI integrations (owned by AI/integrations track)
├── api/
│   ├── deps.py          # FastAPI dependency providers (service composition)
│   └── routes/          # thin routers: health.py, items.py
├── services/            # business logic: items.py (+ ItemsRepositoryProto)
├── repositories/        # data access: items.py
└── db/
    ├── enums.py         # shared enums + FTS constant (single definition)
    ├── base.py          # DeclarativeBase + naming convention
    ├── models.py        # ORM models (MemoryItem)
    ├── session.py       # engine, SessionLocal, get_db dependency
    └── migrations/      # Alembic env + versions
tests/
├── conftest.py          # app client, DB migration + cleanup fixtures
├── test_health.py
└── test_items.py
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
| POST   | `/items`           | Create; 201 + item. Validation errors -> 422                     |
| GET    | `/items`           | List newest-first; filters `type`, `project`, `status`; `limit` (1–200, default 50), `offset` |
| GET    | `/items/search?q=` | Keyword search over `raw_text` via Postgres FTS (websearch syntax, e.g. `foo OR bar`); same pagination |
| GET    | `/items/{id}`      | Fetch one; 404 if missing                                        |
| PATCH  | `/items/{id}`      | Partial update (absent fields unchanged); 404 if missing         |
| DELETE | `/items/{id}`      | Delete; 204, or 404 if missing                                   |

Example:

```bash
curl -X POST localhost:8000/items -H 'Content-Type: application/json' \
  -d '{"raw_text": "Renew the ssl cert", "type": "task", "importance": 5}'
curl 'localhost:8000/items?type=task&status=inbox'
curl 'localhost:8000/items/search?q=renew+cert'
curl -X PATCH localhost:8000/items/<id> -H 'Content-Type: application/json' -d '{"status": "filed"}'
curl -X DELETE localhost:8000/items/<id>
```

Tests: `tests/test_items.py` (needs the compose Postgres; skipped with a hint
if it is down).

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

## Tests

```bash
pytest            # unit + API tests
pytest -v         # verbose
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
