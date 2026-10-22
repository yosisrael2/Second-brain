"""Application settings and default constants, from environment variables / .env.

TLDR: single source of truth for configuration and AI defaults; no secrets in
code. This module imports nothing from ``brain.ai`` (the ai modules import
their defaults from here), keeping the dependency DAG one-way.
"""

from functools import lru_cache
from pathlib import Path
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import Field, SecretStr, field_validator
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
DEFAULT_FASTEMBED_MODEL = "BAAI/bge-small-en-v1.5"
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
    database_url: str = "postgresql+psycopg://brain:brain@localhost:5432/brain"

    # Vector store
    qdrant_url: str = "http://localhost:6333"
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
