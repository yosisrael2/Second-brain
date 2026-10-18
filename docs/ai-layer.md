# AI layer (`src/brain/ai/`)

TLDR: classify free text into typed memory-item fields, embed it, search it in
Qdrant, rerank hits, and answer questions over them with citations. Every
external service (Jev, OpenAI, Anthropic, Qdrant, the local Claude Code
login) sits behind an interface so tests run fully offline.

## Providers (Factory)

Implementations are selected by two `Settings` fields and built in
`src/brain/factories.py` (shared by the API composition root and the CLI):

| Concern | `Settings` field | Default | Alternatives |
| ------- | ---------------- | ------- | ------------ |
| LLM | `llm_provider` | `claude_sdk` — `ClaudeSdkLLMClient` via your local Claude Code login, no API key | `anthropic` — `ClaudeLLMClient` (needs `ANTHROPIC_API_KEY`) |
| Embeddings | `embedder_provider` | `fastembed` — `FastEmbedEmbedder`, local, 384 dims | `openai` — `OpenAIEmbedder` (needs `OPENAI_API_KEY`) |

Both `ClaudeSdkLLMClient` and `FastEmbedEmbedder` run fully offline (after
fastembed's one-time model download) and neither reads credentials — the SDK
uses whatever login the `claude` CLI has; we never touch credential files or
tokens.

## Modules

| Module | What it provides | Pattern |
| ------ | ---------------- | ------- |
| `jev.py` | `JevClient` — typed-decision API adapter (`choice`, `score`, `score_batch`, `decide`) | Adapter |
| `classify.py` | `classify(text)` → `{type, project, importance, actionability, confidence}`; `route(confidence)` → `ItemStatus`; `JevClassifier` adapter. Enums come from `brain.db.enums` (single shared definition) | Service / Adapter |
| `embedder.py` | `Embedder` protocol; `OpenAIEmbedder` (network), `FastEmbedEmbedder` (local fastembed), `FakeEmbedder` (deterministic, offline) | Strategy |
| `vectorstore.py` | `VectorStore` protocol; `QdrantVectorStore` (`ensure_collection`, `upsert`, `delete`, `delete_collection`, `search`); qdrant failures surface as `VectorStoreError`; `ensure_collection` rejects a dim mismatch (remedy: `brain reindex`) | Repository / Adapter |
| `rerank.py` | `rerank(question, candidates, top_n)` — Jev-scored relevance ordering; `JevReranker` adapter | Strategy |
| `ask.py` | `LLMClient` protocol; `ClaudeLLMClient`, `FakeLLMClient`; `answer(question, context_items)` → `{answer, cited_ids}` | Strategy |
| `claude_sdk.py` | `ClaudeSdkLLMClient` — single-turn, tool-less Claude Code session (`tools=[]` → `--tools ''`, `setting_sources=[]`, `strict_mcp_config=True`, scratch `cwd`); auth = local login, no env overrides; SDK errors → `LLMError` | Adapter / Strategy |

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
- Returns `{"answer": str, "cited_ids": [...], "model": str | None}` where
  `cited_ids` are the context ids actually cited in the answer text and
  `model` is the model that actually answered — read from the backend's
  response (`AssistantMessage.model` for the claude_sdk provider, the
  Messages API response `model` field for anthropic), falling back to the
  configured value, else `None` (CLI default, unconfigured). Each LLM call
  also logs `provider` + requested model (or `cli-default`) at INFO.
- LLM failures raise `LLMError`; callers own the fallback policy.

### Security model of /ask (claude_sdk provider)

Stored memory text is untrusted input: it is concatenated into the system
prompt, so a malicious or poisoned item can carry prompt-injection
instructions. The Claude Code session is therefore locked down so injection
cannot escalate beyond producing text: `tools=[]` sends `--tools ''` — the
session has NO tools at all (note `allowed_tools=[]` would be a no-op: it can
only auto-approve, never remove tools); `setting_sources=[]` loads no
user/project settings, so user permission allowlists (e.g. `Bash(curl *)`),
hooks and skills are inert; `strict_mcp_config=True` blocks project
`.mcp.json` server discovery; and `cwd` is a fresh empty scratch dir (removed
on close), so there is no workspace content to read or write. Credentials are
never handled by our code — the SDK authenticates with the existing local
`claude` CLI login, and we pass no environment overrides. Worst case is a
confused answer, not an executed command.

### Jev request shape

Matches `~/.claude/mcp/jev/server.js`: one POST per call with
`{model, state, questions}`, `Authorization: Bearer <key>`; questions run in
parallel and come back in `answers`. `score_batch` bundles candidates as a
list state with per-index questions. Malformed payloads raise `JevError`.
Error messages never include the API key.

### Retry policy

Deliberate: no automatic retries on Jev/OpenAI/Anthropic/Qdrant calls. Every
client fails fast (bounded timeouts) and surfaces a typed error; callers own
recovery. `classify` and `rerank` degrade gracefully (inbox fallback /
original order), so a transient Jev blip never blocks capture or search.
Revisit only if real production traffic shows a need.

## Configuration / env vars

| Variable | Default | Used by |
| -------- | ------- | ------- |
| `LLM_PROVIDER` | `claude_sdk` | provider Factory (`factories.py`) |
| `EMBEDDER_PROVIDER` | `fastembed` | provider Factory (`factories.py`) |
| `CLAUDE_SDK_MODEL` | — (unset = login default; e.g. `claude-haiku-5-5` for faster /ask, `claude-sonnet-5-5` for quality) | `ClaudeSdkLLMClient` |
| `CLAUDE_SDK_TIMEOUT_S` | `120` | `ClaudeSdkLLMClient` |
| `FASTEMBED_MODEL` | `BAAI/bge-small-en-v1.5` (dim 384) | `FastEmbedEmbedder` |
| `JEV_API_KEY` | — (optional; unset = fail-closed fallback) | `JevClient` |
| `JEV_BASE_URL` | `https://api.typesafe.ai/v1/systemone` | `JevClient` |
| `JEV_MODEL` | `jev-latest` | `JevClient` |
| `JEV_TIMEOUT_MS` | `5000` (int; pydantic-validated) | `JevClient` |
| `OPENAI_API_KEY` | — (optional; only when `EMBEDDER_PROVIDER=openai`) | `OpenAIEmbedder` |
| `OPENAI_BASE_URL` | `https://api.openai.com/v1` | `OpenAIEmbedder` |
| `OPENAI_EMBEDDING_MODEL` | `text-embedding-3-small` (dim 1536) | `OpenAIEmbedder` |
| `OPENAI_TIMEOUT_S` | `30` | `OpenAIEmbedder` |
| `ANTHROPIC_API_KEY` | — (optional; only when `LLM_PROVIDER=anthropic`; otherwise `/ask` uses the local login) | `ClaudeLLMClient` |
| `ANTHROPIC_BASE_URL` | `https://api.anthropic.com/v1` | `ClaudeLLMClient` |
| `ANTHROPIC_MODEL` | `claude-sonnet-5-5` | `ClaudeLLMClient` |
| `ANTHROPIC_MAX_TOKENS` | `1024` | `ClaudeLLMClient` |
| `ANTHROPIC_TIMEOUT_S` | `60` | `ClaudeLLMClient` |
| `QDRANT_URL` | `http://localhost:6333` | `QdrantVectorStore` |
| `QDRANT_API_KEY` | — | `QdrantVectorStore` |
| `QDRANT_COLLECTION` | `brain_items` | `QdrantVectorStore` |
| `QDRANT_TIMEOUT_S` | `10` | `QdrantVectorStore` |

All of them are `Settings` fields (`src/brain/config.py`, pydantic-settings;
keys are `SecretStr` so they never leak into logs or reprs) — the AI modules
themselves read no environment variables. Secrets live in `.env` (gitignored)
and are only sent as auth headers.

## App wiring (live)

The layer is wired into the app on this branch (see also README):

- **Composition root**: `src/brain/api/deps.py` builds app-lifetime clients
  (`JevClient`, embedder, LLM client, `QdrantVectorStore`) from `Settings` as
  cached factories (implementations picked by the provider Factory in
  `src/brain/factories.py`), assembles the services per request via
  `Depends`, and exposes `close_shared_clients()` — called by the app lifespan
  on shutdown (only factories already used are closed; shutdown never builds
  new clients).
- **IngestService** (`services/ingest.py`): `POST /items` persists first, then
  classifies (fills only unset fields, routes by confidence), then indexes the
  text via `ItemIndexer` (`services/indexing.py`, ensures the collection at
  most once per instance). Classifier/indexing failures (`JevError`,
  `EmbedderError`, `VectorStoreError`) are logged and skipped: the item is
  captured, stays in the inbox, and is picked up later by `brain reindex`.
  `?classify=false` disables classification; explicit payload fields are never
  overwritten.
- **ItemsService** (`services/items.py`): keeps vectors fresh — `DELETE
  /items/{id}` removes the item's vector, and PATCHes touching
  `raw_text`/`source`/`project` re-index it; index failures never fail the
  request and are recovered by the next `brain reindex`.
- **SearchService** (`services/search.py`): `GET /search?q=` embeds the query,
  searches Qdrant, reranks via Jev, and resolves items from Postgres. Falls
  back to Postgres keyword search (hits with `score: null`) when embeddings or
  Qdrant are unavailable. Caveat: candidates are fetched and ranked per page
  (`limit + offset`), so deep offsets approximate rather than globally rank.
- **AskService** (`services/ask.py`): `POST /ask` searches top-k and answers
  via `LLMClient` with `[id]` citations. LLM failures raise `LLMError`, mapped
  to HTTP 502 by the router layer.
- **CLI**: `brain reindex` (`src/brain/cli.py`) does a full rebuild — it purges
  the Qdrant collection first, which also removes ghost vectors for items no
  longer in Postgres — then re-embeds every item page by page; exits 1 with a
  clear message when embeddings or Qdrant are unavailable.
- **Collections**: `QdrantVectorStore.ensure_collection` is called with the
  active embedder's `dim` (384 for fastembed's `BAAI/bge-small-en-v1.5`, 1536
  for `text-embedding-3-small`); an existing collection with a different dim
  raises a clear `VectorStoreError` — run `brain reindex` to drop and rebuild.

## Tests

```bash
pytest tests/ai -v
```

All tests are offline: HTTP is stubbed with `httpx.MockTransport`, Qdrant with
an in-memory client, the SDK with a fake `query_fn` (real SDK message DTOs),
fastembed with a fake `TextEmbedding` module, Jev/LLM with stub classes
covering the three routing bands, Jev-failure fallbacks (classify + rerank),
and citation presence. Endpoint wiring is covered by `tests/test_wiring.py`
with fake-backed infrastructure; `tests/test_cli_and_lifespan.py` covers
`brain reindex` and client shutdown.
