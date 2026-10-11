"""Application settings, loaded from environment variables / .env.

TLDR: single source of truth for configuration; no secrets in code.
"""

from functools import lru_cache
from typing import Literal

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

from brain.ai.ask import (
    DEFAULT_ANTHROPIC_TIMEOUT_S,
    DEFAULT_ANTHROPIC_URL,
    DEFAULT_CLAUDE_MODEL,
    DEFAULT_MAX_TOKENS,
)
from brain.ai.claude_sdk import DEFAULT_SDK_TIMEOUT_S
from brain.ai.embedder import (
    DEFAULT_EMBEDDING_MODEL,
    DEFAULT_FASTEMBED_MODEL,
    DEFAULT_OPENAI_TIMEOUT_S,
    DEFAULT_OPENAI_URL,
)
from brain.ai.jev import DEFAULT_BASE_URL as DEFAULT_JEV_BASE_URL
from brain.ai.jev import DEFAULT_MODEL as DEFAULT_JEV_MODEL
from brain.ai.jev import DEFAULT_TIMEOUT_S as DEFAULT_JEV_TIMEOUT_S
from brain.ai.vectorstore import DEFAULT_COLLECTION as DEFAULT_QDRANT_COLLECTION
from brain.ai.vectorstore import DEFAULT_QDRANT_TIMEOUT_S


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


@lru_cache
def get_settings() -> Settings:
    """Return the cached Settings instance."""
    return Settings()
