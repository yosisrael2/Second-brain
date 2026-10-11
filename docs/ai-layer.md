# AI layer (`src/brain/ai/`)

TLDR: classify free text into typed memory-item fields, embed it, search it in
Qdrant, rerank hits, and answer questions over them with citations. Every
external service (Jev, OpenAI, Anthropic, Qdrant) sits behind an interface so
tests run fully offline.

## Modules

| Module | What it provides | Pattern |
| ------ | ---------------- | ------- |
| `jev.py` | `JevClient` — typed-decision API adapter (`choice`, `score`, `score_batch`, `decide`) | Adapter |
| `classify.py` | `classify(text)` → `{type, project, importance, actionability, confidence}`; `route(confidence)` → `ItemStatus` | Service |
| `enums.py` | Temporary copies of `ItemType` / `Actionability` / `ItemStatus` (TODO: swap to shared `brain.domain` enums when worker_1 lands them) | — |
| `embedder.py` | `Embedder` protocol; `OpenAIEmbedder` (network) and `FakeEmbedder` (deterministic, offline) | Strategy |
| `vectorstore.py` | `VectorStore` protocol; `QdrantVectorStore` (`ensure_collection`, `upsert`, `search`) | Repository / Adapter |
| `rerank.py` | `rerank(question, candidates, top_n)` — Jev-scored relevance ordering | Strategy |
| `ask.py` | `LLMClient` protocol; `ClaudeLLMClient`, `FakeLLMClient`; `answer(question, context_items)` → `{answer, cited_ids}` | Strategy |

## Behavior details

### classify + route

- One batched Jev call asks type (choice), importance (score 1-5) and
  actionability (score 1-3) against the item text.
- `type` is an `ItemType`, `importance` an int 1-5, `actionability` an
  `Actionability` (`none|later|now`), `confidence` a float 0-1 (the type
  question's confidence).
- `route(confidence)`: `>=0.9` → `filed`, `0.5-0.9` → `needs_review`, below
  `0.5` → `inbox` (as `ItemStatus`).
- Any Jev failure — unreachable, timeout, malformed answer — returns
  `FALLBACK_CLASSIFICATION` (all fields `None`, confidence `0.0`), which
  routes to `inbox`. `classify` never raises.
- Unknown Jev choices (e.g. a type outside the enum) map to `None` for that
  field.

### rerank

- One batched Jev call scores every candidate (mappings with `id` and `text`)
  against the question; results are sorted by score, stable on ties, truncated
  to `top_n`.
- If Jev is down or malformed, candidates are returned in their original
  (vector-search) order, truncated to `top_n`. `rerank` never raises.

### ask

- Builds a prompt listing each context item as `- [id] text` and instructs the
  model to cite ids in square brackets.
- Returns `{"answer": str, "cited_ids": [...]}` where `cited_ids` are the
  context ids actually cited in the answer text.
- LLM failures raise `LLMError`; callers own the fallback policy.

### Jev request shape

Matches `~/.claude/mcp/jev/server.js`: one POST per call with
`{model, state, questions}`, `Authorization: Bearer <key>`; questions run in
parallel and come back in `answers`. `score_batch` bundles candidates as a
list state with per-index questions. Malformed payloads raise `JevError`.
Error messages never include the API key.

## Configuration / env vars

| Variable | Default | Used by |
| -------- | ------- | ------- |
| `JEV_API_KEY` | — (required) | `JevClient` |
| `JEV_BASE_URL` | `https://api.typesafe.ai/v1/systemone` | `JevClient` |
| `JEV_MODEL` | `jev-latest` | `JevClient` |
| `JEV_TIMEOUT_MS` | `5000` | `JevClient` |
| `OPENAI_API_KEY` | — (required for real embeddings) | `OpenAIEmbedder` |
| `OPENAI_BASE_URL` | `https://api.openai.com/v1` | `OpenAIEmbedder` |
| `ANTHROPIC_API_KEY` | — (required for real answers) | `ClaudeLLMClient` |
| `ANTHROPIC_BASE_URL` | `https://api.anthropic.com/v1` | `ClaudeLLMClient` |
| `ANTHROPIC_MODEL` | `claude-sonnet-4-5` | `ClaudeLLMClient` |
| `QDRANT_URL` | — (required) | `QdrantVectorStore` |
| `QDRANT_API_KEY` | — | `QdrantVectorStore` |
| `QDRANT_COLLECTION` | `brain_items` | `QdrantVectorStore` |

Secrets live in `.env` (gitignored) and are only sent as auth headers — never
logged, never committed.

## Wiring into the app (planned, M1)

Routers in `src/brain/api/routes/` will receive these as constructor-injected
collaborators of a service (FastAPI `Depends`); `/search` will combine
`Embedder` + `VectorStore` (+ `rerank`), `/ask` will add `answer`. Not wired
yet.

## Tests

```bash
pytest tests/ai -v
```

All tests are offline: HTTP is stubbed with `httpx.MockTransport`, Qdrant with
an in-memory client, Jev/LLM with stub classes covering the three routing
bands, Jev-failure fallbacks (classify + rerank), and citation presence.
