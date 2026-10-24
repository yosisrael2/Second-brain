"""Application settings and default constants, from environment variables / .env.

TLDR: single source of truth for configuration and AI defaults; no secrets in
code. This module imports nothing from ``brain.ai`` (the ai modules import
their defaults from here), keeping the dependency DAG one-way.
"""

from functools import lru_cache
from pathlib import Path
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import AliasChoices, Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# --- AI defaults (canonical definitions; ai/ modules re-export these) ---------
DEFAULT_ANTHROPIC_URL = "https://api.anthropic.com/v1"
DEFAULT_CLAUDE_MODEL = "claude-sonnet-5-5"
DEFAULT_MAX_TOKENS = 1024
DEFAULT_ANTHROPIC_TIMEOUT_S = 60.0
DEFAULT_SDK_TIMEOUT_S = 120.0
DEFAULT_OPENAI_URL = "https://api.openai.com/v1"
DEFAULT_EMBEDDING_MODEL = "text-embedding-3-small"
DEFAULT_OPENAI_TIMEOUT_S = 30.0
# Multilingual (Hebrew + English) — chosen by the calendar-question eval in
# tests/eval/test_calendar_questions.py: 10/11 retrieval hits vs 6/11 for the
# old English-only BAAI/bge-small-en-v1.5. dim 256.
DEFAULT_FASTEMBED_MODEL = "minishlab/potion-multilingual-128M"
DEFAULT_JEV_BASE_URL = "https://api.typesafe.ai/v1/systemone"
DEFAULT_JEV_MODEL = "jev-latest"
DEFAULT_JEV_TIMEOUT_S = 5.0
DEFAULT_QDRANT_COLLECTION = "brain_items"
DEFAULT_QDRANT_TIMEOUT_S = 10  # seconds; QdrantClient takes int|None


# Connectors (connectors ticket): canonical source names. The Gmail adapter
# registers under "email" (items.source compatibility with existing data);
# "gmail" resolves to it as a registry alias at sync time.
KNOWN_CONNECTOR_NAMES = frozenset({"email", "gcal", "gdrive", "gtasks"})


def unwrap_secret(value: SecretStr | None) -> str | None:
    """Unwrap an optional SecretStr for client constructors (single shared helper)."""
    return value.get_secret_value() if value else None


def local_timezone_name() -> str:
    """Best-effort IANA name of the machine's local zone (fallback: ``UTC``).

    Reads the ``/etc/localtime`` symlink target (POSIX); used as the default
    for the ``TIMEZONE`` setting, which interprets zone-less pasted times.
    """
    try:
        parts = Path("/etc/localtime").resolve().parts
        if "zoneinfo" in parts:
            return "/".join(parts[parts.index("zoneinfo") + 1 :])
    except OSError:  # pragma: no cover - resolve rarely fails; fallback below
        pass
    return "UTC"


class Settings(BaseSettings):
    """Environment-driven settings with local-dev defaults matching docker-compose.yml."""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_name: str = "second-brain"
    environment: str = "dev"
    database_url: str = "postgresql+psycopg://brain:brain@localhost:27432/brain"

    # Vector store
    qdrant_url: str = "http://localhost:27333"
    qdrant_api_key: SecretStr | None = None
    qdrant_collection: str = DEFAULT_QDRANT_COLLECTION
    qdrant_timeout_s: int = DEFAULT_QDRANT_TIMEOUT_S

    # Jev typed-decision API
    jev_api_key: SecretStr | None = None
    jev_base_url: str = DEFAULT_JEV_BASE_URL
    jev_model: str = DEFAULT_JEV_MODEL
    jev_timeout_ms: int = int(DEFAULT_JEV_TIMEOUT_S * 1000)

    # Provider selection (local dev defaults need no API keys)
    llm_provider: Literal["claude_sdk", "anthropic"] = "claude_sdk"
    embedder_provider: Literal["fastembed", "openai"] = "fastembed"

    # OpenAI embeddings (used when embedder_provider="openai")
    openai_api_key: SecretStr | None = None
    openai_base_url: str = DEFAULT_OPENAI_URL
    openai_embedding_model: str = DEFAULT_EMBEDDING_MODEL
    openai_timeout_s: float = DEFAULT_OPENAI_TIMEOUT_S

    # Local embeddings (used when embedder_provider="fastembed", the default)
    fastembed_model: str = DEFAULT_FASTEMBED_MODEL
    # Ask context size for semantic search. Day/date questions bypass this via
    # temporal retrieval (they fetch every item in the resolved range).
    ask_top_k: int = 8
    # Attachments (design delta items 43-45): original files are stored OUTSIDE
    # the repo and the DB, content-addressed by sha256, exact duplicates stored
    # once. The dir must not live inside the checkout.
    files_dir: Path = Path.home() / ".second-brain-files"
    # Daily reading budget (Claude reads + text-layer extractions), item 44.
    attachment_daily_budget: int = 50
    # Candidates fetched for the reranker before truncating to ask_top_k.
    # Reranking is cheap relative to the LLM call, and the reranker can only
    # surface what reaches it: a Hebrew email corpus scores an English
    # question ~0.2 cosine (TALAN email measured at vector rank 23 of 47 —
    # invisible at pool 8, rank 4 of 32 after rerank). One rerank call,
    # ~4x documents.
    ask_recall_pool: int = 32
    # Kid-recall leg of /ask (PR 0d): when a question names a confirmed kid,
    # items from that kid's channels/class join the recall pool before the
    # rerank. This caps how many extra candidates that leg may fetch —
    # a ceiling, never a floor (fewer matches -> fewer candidates).
    ask_kid_recall_limit: int = 16
    # Meaning extraction (PR A1): daily LLM-extraction cap (D5) and the
    # in-process worker switch. The worker stays OFF in A1 — the queue is
    # durable and drained by `brain enrich` / tests; A2 flips it on with the
    # real extractor (never blocks the capture reply either way).
    enrichment_daily_cap: int = 100
    enrichment_worker_enabled: bool = False
    # A2 activation seam: which extractor the enrichment pipeline builds
    # (services.extraction.service.build_extractor is the ONE chooser).
    # 'stub' (default) keeps live behaviour exactly as A1 shipped it; 'llm'
    # selects the real LLM extractor. Activation = this + the worker switch,
    # never a code change. Literal validation rejects anything else.
    enrichment_extractor: Literal["stub", "llm"] = "stub"

    # Anthropic LLM (used when llm_provider="anthropic")
    anthropic_api_key: SecretStr | None = None
    anthropic_base_url: str = DEFAULT_ANTHROPIC_URL
    anthropic_model: str = DEFAULT_CLAUDE_MODEL
    anthropic_max_tokens: int = DEFAULT_MAX_TOKENS
    anthropic_timeout_s: float = DEFAULT_ANTHROPIC_TIMEOUT_S

    # Claude Code login LLM (used when llm_provider="claude_sdk", the default)
    claude_sdk_model: str | None = None
    claude_sdk_timeout_s: float = DEFAULT_SDK_TIMEOUT_S

    # IANA zone used to interpret zone-less pasted times (Gmail, WhatsApp,
    # copied calendar text) before storing them as aware UTC.
    timezone: str = Field(default_factory=local_timezone_name)

    # P1: a pasted ICS VEVENT with an RRULE fans out into one item per
    # occurrence within [paste time, paste time + paste_rrule_days], capped
    # at paste_rrule_max_occurrences items per series.
    paste_rrule_days: int = Field(
        default=60,
        validation_alias=AliasChoices("paste_rrule_days", "BRAIN_PASTE_RRULE_DAYS"),
    )
    paste_rrule_max_occurrences: int = Field(
        default=60,
        validation_alias=AliasChoices(
            "paste_rrule_max_occurrences", "BRAIN_PASTE_RRULE_MAX_OCCURRENCES"
        ),
    )

    # WhatsApp bridge (PR D1). The poller reads the bridge's messages.db
    # READ-ONLY; an empty path disables the poller. The gap groups messages
    # into conversations (item 35); the overlap window (must stay smaller
    # than the gap) makes restarts idempotent.
    whatsapp_bridge_db: str = ""
    # F3 (item 47): the bridge's local REST endpoint for media downloads
    # (POST {url}/api/download). Read-only use only — never /api/send.
    whatsapp_bridge_url: str = "http://127.0.0.1:8085"
    whatsapp_gap_minutes: int = 60
    whatsapp_overlap_minutes: int = 5
    whatsapp_poll_interval_s: float = 30.0

    # Telegram bot (mobile capture channel). The token is a secret; the
    # allowlist is REQUIRED with a token — the bot never answers strangers.
    telegram_bot_token: SecretStr | None = None
    telegram_allowed_user_ids: str = ""  # comma-separated Telegram user ids
    # The bot's getUpdates long-poll timeout (seconds). ONE source of truth:
    # the poll loop uses it, and /ops/health derives the telegram heartbeat
    # threshold from it (timeout + margin) so a quiet poll never flaps.
    telegram_poll_timeout_s: int = 50

    # Email source (F2): a read-only Gmail label poller. The label ID is an
    # immutable Gmail label id (never its display name); the OAuth refresh
    # token lives in the macOS login keychain (never here, never in .env).
    # credentials_path points at the Desktop-client secret JSON OUTSIDE the
    # repo (the gmail-auth helper needs it; the poller does not).
    gmail_label_id: str = ""
    gmail_query: str = ""
    gmail_poll_interval_s: int = 60
    gmail_credentials_path: str = ""

    # Connectors (connectors ticket): the enabled data sources, comma
    # separated (BRAIN_CONNECTORS="email,gcal,gdrive,gtasks"). Default empty
    # = none enabled; `brain connectors sync` refuses disabled names and
    # `brain connectors auth google` requests the union of their scopes.
    # Field name mirrors the env var (pydantic-settings convention).
    brain_connectors: str = ""

    @field_validator("brain_connectors")
    @classmethod
    def _validate_connectors(cls, value: str) -> str:
        """Fail fast on unknown connector names (a typo must not disable a
        source silently). Canonical names only; 'gmail' is accepted too and
        means 'email'."""
        for part in value.split(","):
            name = part.strip().lower()
            if not name:
                continue
            if name == "gmail":
                name = "email"
            if name not in KNOWN_CONNECTOR_NAMES:
                raise ValueError(
                    f"BRAIN_CONNECTORS: unknown connector {part.strip()!r} "
                    f"(known: {', '.join(sorted(KNOWN_CONNECTOR_NAMES))})"
                )
        return value

    @property
    def connectors_enabled(self) -> tuple[str, ...]:
        """The enabled connector names (canonical, deduped, order kept)."""
        enabled: list[str] = []
        for part in self.brain_connectors.split(","):
            name = part.strip().lower()
            if not name:
                continue
            canonical = "email" if name == "gmail" else name
            if canonical not in enabled:
                enabled.append(canonical)
        return tuple(enabled)

    # Google Calendar connector (connectors ticket): comma-separated calendar
    # ids to sync (allowlist, order preserved). 'primary' is the authenticated
    # user's main calendar; an empty value syncs nothing.
    brain_gcal_calendars: str = "primary"
    # How far back the first gcal full sync reaches (instances that ENDED more
    # than this many days ago are never fetched; the cutoff is frozen at the
    # first sync and persisted in the connector cursor). There is NO future
    # cap: a dropped future instance would never be re-delivered.
    brain_gcal_past_days: int = 60

    # Google Tasks connector (gtasks, scope tasks.readonly). Optional allowlist
    # of task lists to sync, comma-separated list IDs or titles (titles matched
    # case-insensitively); empty = sync every list. IDs are stable while titles
    # can be renamed, so prefer IDs; an allowlist change invalidates the sync
    # cursor (full resync) so old tasks of newly-allowed lists are picked up.
    gtasks_lists: str = Field(
        default="",
        validation_alias=AliasChoices("BRAIN_GTASKS_LISTS", "GTASKS_LISTS"),
    )

    @field_validator("telegram_allowed_user_ids")
    @classmethod
    def _validate_telegram_user_ids(cls, value: str) -> str:
        """Keep the raw string but fail fast when it is not parseable ints."""
        for part in value.split(","):
            part = part.strip()
            if part and not part.lstrip("+").isdigit():
                raise ValueError(
                    f"TELEGRAM_ALLOWED_USER_IDS must be comma-separated integers, got {value!r}"
                )
        return value

    @model_validator(mode="after")
    def _telegram_token_requires_allowlist(self) -> "Settings":
        if self.telegram_bot_token and not self.telegram_allowed_user_ids.strip():
            raise ValueError(
                "TELEGRAM_ALLOWED_USER_IDS is required when TELEGRAM_BOT_TOKEN "
                "is set (run `brain telegram --whoami-hint` to find your id)"
            )
        return self

    @property
    def telegram_allowed_ids(self) -> frozenset[int]:
        """The allowlist as ints (empty when no ids are configured)."""
        return frozenset(
            int(part.strip().lstrip("+"))
            for part in self.telegram_allowed_user_ids.split(",")
            if part.strip()
        )

    @property
    def gcal_calendar_ids(self) -> tuple[str, ...]:
        """The calendar allowlist in order; empty means the gcal connector syncs nothing."""
        return tuple(part.strip() for part in self.brain_gcal_calendars.split(",") if part.strip())

    @field_validator("timezone")
    @classmethod
    def _validate_timezone(cls, value: str) -> str:
        """Refuse to boot with an unresolvable IANA zone name."""
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError, KeyError, OSError) as exc:
            raise ValueError(f"TIMEZONE must be a valid IANA zone name, got {value!r}") from exc
        return value


@lru_cache
def get_settings() -> Settings:
    """Return the cached Settings instance."""
    return Settings()


# --- live-store startup guard -------------------------------------------------
# The Settings defaults ARE the live service (they match docker-compose.yml and
# the launchd path via .env). A bare `uvicorn brain.main:app` from a worktree
# without .env therefore points at production — that incident is why the app
# refuses to start against the live store unless explicitly allowed.
LIVE_DATABASE_URL = "postgresql+psycopg://brain:brain@localhost:27432/brain"
LIVE_QDRANT_COLLECTION = DEFAULT_QDRANT_COLLECTION  # 'brain_items'


def test_collection_for(database_name: str) -> str:
    """The Qdrant collection name for a test database (per-worker isolation).

    Derives the test collection from the test database name so two workers
    running suites against different test databases (brain_w5_test,
    brain_test, ...) never share one Qdrant collection — concurrent suites
    polluting each other's vectors was a real incident. 'brain_w5_test' ->
    'brain_items_w5_test', 'brain_test' -> 'brain_items_test'. Callers keep
    the ``*_test``-suffix guard: this function is only ever fed names that
    already end in ``_test`` (conftest enforces that before calling).
    """
    if database_name.startswith("brain_"):
        database_name = database_name[len("brain_") :]
    return f"brain_items_{database_name}"


def _is_live_database_url(url: str) -> bool:
    """True when `url` points at the live database.

    Either the exact default URL, or any driver scheme pointing at database
    name 'brain' on localhost:27432 (the live service's port), or a
    unix-socket connection (no netloc host/port) to the live dbname — this
    host's default-cluster 'brain' database can only be the live one, and
    the compose stack publishes no host unix socket, so such a URL is both
    live-classified and structurally unreachable (guard tests rely on that).
    """
    if url == LIVE_DATABASE_URL:
        return True
    try:
        from sqlalchemy.engine import make_url  # noqa: PLC0415 — models already ship it

        parsed = make_url(url)
    except Exception:  # noqa: BLE001 — a non-URL string is simply not the live URL
        return False
    host = (parsed.host or "").lower()
    name = (parsed.database or "").lstrip("/")
    if name == "brain" and parsed.host is None and parsed.port is None:
        return True  # unix-socket connection to the live-named database
    return name == "brain" and parsed.port == 27432 and host in {"localhost", "127.0.0.1", "::1"}


def _allow_live(allow_live: str | None) -> bool:
    """BRAIN_ALLOW_LIVE=1 opts in; an explicit argument wins over the env."""
    if allow_live is not None:
        return allow_live == "1"
    import os  # noqa: PLC0415 — keep the module import surface unchanged

    return os.environ.get("BRAIN_ALLOW_LIVE") == "1"


def assert_not_live_database(database_url: str, allow_live: str | None = None) -> None:
    """Refuse the LIVE database URL unless BRAIN_ALLOW_LIVE=1 (construction guard).

    The narrow half of :func:`assert_not_live_store`: called from the
    SQLAlchemy engine factory (brain.db.session) so every engine built from
    settings is checked where it is created, not only when an app starts.
    """
    if _allow_live(allow_live):
        return
    if _is_live_database_url(url=database_url):
        raise RuntimeError(
            "refusing to start against the LIVE store; set DATABASE_URL/QDRANT_COLLECTION "
            "for a test instance, or BRAIN_ALLOW_LIVE=1 for the real service ("
            f"DATABASE_URL points at the live database ({database_url!r})"
            ")"
        )


def assert_not_live_collection(
    qdrant_collection: str, allow_live: str | None = None
) -> None:
    """Refuse the LIVE collection name unless BRAIN_ALLOW_LIVE=1 (construction guard).

    The other narrow half: called from the Qdrant vector store's __init__
    with the collection the store will ACTUALLY use (injected value, not
    just the env), so a cached factory or a stray script cannot build a
    live-pointing store unnoticed.
    """
    if _allow_live(allow_live):
        return
    if qdrant_collection == LIVE_QDRANT_COLLECTION:
        raise RuntimeError(
            "refusing to start against the LIVE store; set DATABASE_URL/QDRANT_COLLECTION "
            "for a test instance, or BRAIN_ALLOW_LIVE=1 for the real service ("
            f"QDRANT_COLLECTION is the live collection ({qdrant_collection!r})"
            ")"
        )


def assert_not_live_store(
    database_url: str, qdrant_collection: str, allow_live: str | None = None
) -> None:
    """Refuse to start the app against the LIVE store unless explicitly allowed.

    Wired into the app lifespan (src/brain/main.py) and composed from the
    two narrow construction guards (engine factory, vector store). ``scripts/run-server.sh``
    — the launchd path — exports ``BRAIN_ALLOW_LIVE=1`` to opt in; anything
    else (a worktree without .env, a stray local instance) must point at an
    explicit test DATABASE_URL / QDRANT_COLLECTION or fail fast here.
    """
    if _allow_live(allow_live):
        return
    reasons: list[str] = []
    if _is_live_database_url(url=database_url):
        reasons.append(f"DATABASE_URL points at the live database ({database_url!r})")
    if qdrant_collection == LIVE_QDRANT_COLLECTION:
        reasons.append(f"QDRANT_COLLECTION is the live collection ({qdrant_collection!r})")
    if reasons:
        raise RuntimeError(
            "refusing to start against the LIVE store; set DATABASE_URL/QDRANT_COLLECTION "
            "for a test instance, or BRAIN_ALLOW_LIVE=1 for the real service ("
            + "; ".join(reasons)
            + ")"
        )
