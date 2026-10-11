"""LLM completions through the local Claude Code login (claude-agent-sdk).

TLDR: :class:`ClaudeSdkLLMClient` is a :class:`brain.ai.ask.LLMClient`
Strategy that runs a headless, tool-less single-turn Claude Code session —
no ``ANTHROPIC_API_KEY`` needed; the SDK owns the login and we never read,
copy, or log credentials.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import TYPE_CHECKING, Any, Protocol

from .ask import LLMError

if TYPE_CHECKING:
    from claude_agent_sdk import ClaudeAgentOptions

DEFAULT_SDK_TIMEOUT_S = 120.0
# The full ask prompt (question, sources, citation rules) rides in the system
# prompt; the user turn only nudges the model to answer it.
_USER_TURN = "Answer the request in your system prompt."


class SdkQueryFn(Protocol):
    """Shape of ``claude_agent_sdk.query`` we depend on (injectable for tests)."""

    def __call__(
        self, *, prompt: str, options: ClaudeAgentOptions | None
    ) -> AsyncIterator[Any]:
        """Run one headless session and yield its messages."""
        ...


class ClaudeSdkLLMClient:
    """Strategy producing completions via the machine's Claude Code login.

    Each ``complete`` spawns a transient SDK session restricted to plain text:
    ``allowed_tools`` is empty (no Bash, files, or MCP), ``permission_mode`` is
    ``"default"`` and ``max_turns`` is 1. No environment variables are set or
    overridden — auth is whatever the local ``claude`` CLI login provides.
    SDK failures raise :class:`LLMError` so callers keep their 502 mapping.
    """

    def __init__(
        self,
        model: str | None = None,
        timeout_s: float = DEFAULT_SDK_TIMEOUT_S,
        query_fn: SdkQueryFn | None = None,
    ) -> None:
        self._model = model
        self._timeout_s = timeout_s
        self._query_fn = query_fn if query_fn is not None else _sdk_query

    def close(self) -> None:
        """No-op: sessions are transient, nothing lives between calls."""

    def complete(self, prompt: str) -> str:
        """Return the model's completion for ``prompt``."""
        try:
            return asyncio.run(
                asyncio.wait_for(self._collect_text(prompt), timeout=self._timeout_s)
            )
        except TimeoutError as exc:
            raise LLMError(f"claude sdk timed out after {self._timeout_s}s") from exc
        except LLMError:
            raise
        except _sdk_error_types() as exc:
            raise LLMError(f"claude sdk failed: {type(exc).__name__}: {exc}") from exc

    async def _collect_text(self, prompt: str) -> str:
        from claude_agent_sdk import AssistantMessage, ClaudeAgentOptions, TextBlock

        options = ClaudeAgentOptions(
            allowed_tools=[],
            permission_mode="default",
            system_prompt=prompt,
            max_turns=1,
            model=self._model,
        )
        texts: list[str] = []
        async for message in self._query_fn(prompt=_USER_TURN, options=options):
            if isinstance(message, AssistantMessage):
                texts.extend(
                    block.text for block in message.content if isinstance(block, TextBlock)
                )
        text = "".join(texts).strip()
        if not text:
            raise LLMError("claude sdk returned no text")
        return text


def _sdk_query(*, prompt: str, options: ClaudeAgentOptions | None) -> AsyncIterator[Any]:
    """Lazy wrapper around ``claude_agent_sdk.query`` (keeps import optional)."""
    from claude_agent_sdk import query

    return query(prompt=prompt, options=options)


def _sdk_error_types() -> tuple[type[Exception], ...]:
    """Resolve the SDK's exception classes lazily (keeps the import optional)."""
    from claude_agent_sdk import (
        CLIConnectionError,
        CLIJSONDecodeError,
        CLINotFoundError,
        ProcessError,
        ResultError,
    )

    return (CLIConnectionError, CLIJSONDecodeError, CLINotFoundError, ProcessError, ResultError)
