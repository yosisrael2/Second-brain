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

- **Dependency Injection**: routers receive services via `fastapi.Depends`;
  services receive repositories via constructor injection.
- **Repository pattern**: all DB access lives in `src/brain/repositories/`.
- **Service layer**: all business logic lives in `src/brain/services/`.
- **Factory**: `create_app()` in `src/brain/main.py` assembles the app;
  `create_engine_from_settings()` builds the DB engine from config.
- `src/brain/ai/` hosts future Strategy implementations (Embedder/LLMClient)
  behind abstractions — owned by the AI/integrations track. Already live:
  classification (Jev), embeddings, vector search, rerank, and grounded Q&A —
  see [docs/ai-layer.md](docs/ai-layer.md).

## Folder layout

```
src/brain/
├── config.py            # env-based settings (pydantic-settings)
├── main.py              # FastAPI app factory, GET /health
├── ai/                  # AI integrations (owned by AI/integrations track)
├── api/routes/          # thin FastAPI routers
├── services/            # business logic (Service layer)
├── repositories/        # data access (Repository pattern)
└── db/
    ├── base.py          # DeclarativeBase
    ├── models.py        # ORM models (registers on Base.metadata)
    ├── session.py       # engine, SessionLocal, get_db dependency
    └── migrations/      # Alembic env + version scripts
tests/                   # pytest suite (fastapi TestClient)
```

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

AI-layer variables (`JEV_API_KEY`, `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`,
`QDRANT_COLLECTION`, timeouts, base URLs) are documented in
[docs/ai-layer.md](docs/ai-layer.md). None are required to boot the app;
`classify`/`rerank` fall back gracefully when Jev is unconfigured.

## Tests

```bash
pytest            # unit + API tests
pytest -v         # verbose
ruff check src tests   # lint (E, F, I, B, UP; config in pyproject [tool.ruff])
mypy src/brain/ai      # type check (strict-ish; config in pyproject [tool.mypy])
```

The `/health` test needs no infrastructure. DB-backed tests require the
docker-compose Postgres to be running.

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
