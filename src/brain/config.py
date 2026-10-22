"""Application settings and default constants, from environment variables / .env.

TLDR: single source of truth for configuration and AI defaults; no secrets in
code. This module imports nothing from ``brain.ai`` (the ai modules import
their defaults from here), keeping the dependency DAG one-way.
"""

from functools import lru_cache
from pathlib import Path
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import Field, SecretStr, field_validator, model_validator
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
    # Candidates fetched for the reranker before truncating to ask_top_k.
    # Reranking is cheap relative to the LLM call, and the reranker can only
    # surface what reaches it: a Hebrew email corpus scores an English
    # question ~0.2 cosine (TALAN email measured at vector rank 23 of 47 —
    # invisible at pool 8, rank 4 of 32 after rerank). One rerank call,
    # ~4x documents.
    ask_recall_pool: int = 32

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

    # Telegram bot (mobile capture channel). The token is a secret; the
    # allowlist is REQUIRED with a token — the bot never answers strangers.
    telegram_bot_token: SecretStr | None = None
    telegram_allowed_user_ids: str = ""  # comma-separated Telegram user ids

    @field_validator("telegram_allowed_user_ids")
    @classmethod
    def _validate_telegram_user_ids(cls, value: str) -> str:
        """Keep the raw string but fail fast when it is not parseable ints."""
        for part in value.split(","):
            part = part.strip()
            if part and not part.lstrip("+").isdigit():
                raise ValueError(
                    "TELEGRAM_ALLOWED_USER_IDS must be comma-separated integers, "
                    f"got {value!r}"
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


def _is_live_database_url(url: str) -> bool:
    """True when `url` points at the live database.

    Either the exact default URL, or any driver scheme pointing at database
    name 'brain' on localhost:27432 (the live service's port).
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
    return name == "brain" and parsed.port == 27432 and host in {"localhost", "127.0.0.1", "::1"}


def assert_not_live_store(
    database_url: str, qdrant_collection: str, allow_live: str | None = None
) -> None:
    """Refuse to start the app against the LIVE store unless explicitly allowed.

    Wired into the app lifespan (src/brain/main.py). ``scripts/run-server.sh``
    — the launchd path — exports ``BRAIN_ALLOW_LIVE=1`` to opt in; anything
    else (a worktree without .env, a stray local instance) must point at an
    explicit test DATABASE_URL / QDRANT_COLLECTION or fail fast here.
    """
    if allow_live == "1":
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
