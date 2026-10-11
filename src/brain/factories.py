"""Provider factories: build AI clients from ``Settings`` (Factory pattern).

Shared by the FastAPI composition root (:mod:`brain.api.deps`) and the CLI so
both honor ``LLM_PROVIDER`` / ``EMBEDDER_PROVIDER``. The keyless defaults are
``claude_sdk`` (local Claude Code login) and ``fastembed`` (local embeddings).
"""

from __future__ import annotations

from pydantic import SecretStr

from brain.ai.ask import ClaudeLLMClient, LLMClient
from brain.ai.claude_sdk import ClaudeSdkLLMClient
from brain.ai.embedder import Embedder, FastEmbedEmbedder, OpenAIEmbedder
from brain.config import Settings


def build_embedder(settings: Settings) -> Embedder:
    """Return the embedder selected by ``settings.embedder_provider``."""
    if settings.embedder_provider == "openai":
        return OpenAIEmbedder(
            api_key=_secret(settings.openai_api_key),
            model=settings.openai_embedding_model,
            base_url=settings.openai_base_url,
            timeout_s=settings.openai_timeout_s,
        )
    return FastEmbedEmbedder(model=settings.fastembed_model)


def build_llm_client(settings: Settings) -> LLMClient:
    """Return the LLM client selected by ``settings.llm_provider``."""
    if settings.llm_provider == "anthropic":
        return ClaudeLLMClient(
            api_key=_secret(settings.anthropic_api_key),
            model=settings.anthropic_model,
            base_url=settings.anthropic_base_url,
            max_tokens=settings.anthropic_max_tokens,
            timeout_s=settings.anthropic_timeout_s,
        )
    return ClaudeSdkLLMClient(
        model=settings.claude_sdk_model,
        timeout_s=settings.claude_sdk_timeout_s,
    )


def _secret(value: SecretStr | None) -> str | None:
    """Unwrap an optional pydantic SecretStr for client constructors."""
    return value.get_secret_value() if value else None
