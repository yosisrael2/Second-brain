"""Application settings, loaded from environment variables / .env.

TLDR: single source of truth for configuration; no secrets in code.
"""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Environment-driven settings with local-dev defaults matching docker-compose.yml."""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_name: str = "second-brain"
    environment: str = "dev"
    database_url: str = "postgresql+psycopg://brain:brain@localhost:5432/brain"
    qdrant_url: str = "http://localhost:6333"


@lru_cache
def get_settings() -> Settings:
    """Return the cached Settings instance."""
    return Settings()
