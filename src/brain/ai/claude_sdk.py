"""LLM completions through the local Claude Code login (claude-agent-sdk).

TLDR: :class:`ClaudeSdkLLMClient` is a :class:`brain.ai.ask.LLMClient`
Strategy that runs a headless, tool-less single-turn Claude Code session in a
locked-down sandbox — no ``ANTHROPIC_API_KEY`` needed; the SDK owns the login
and we never read, copy, or log credentials.
"""

from __future__ import annotations

import asyncio
import logging
import shutil
import tempfile
from collections.abc import AsyncIterator
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

from brain.config import DEFAULT_SDK_TIMEOUT_S

from .ask import LLMError

if TYPE_CHECKING:
    from claude_agent_sdk import ClaudeAgentOptions

logger = logging.getLogger(__name__)

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

    Each ``complete`` spawns a transient SDK session hardened against prompt
    injection from stored item text (the full security rationale lives in
    ``docs/ai-layer.md``):

    - ``tools=[]`` — the CLI gets ``--tools ''``, i.e. NO tools exist at all
      (note ``allowed_tools=[]`` would be a no-op: it only ever auto-approves).
    - ``setting_sources=[]`` — no user/project settings are loaded, so user
      permission allowlists (e.g. ``Bash(curl *)``), hooks and skills are out.
    - ``strict_mcp_config=True`` — no project ``.mcp.json`` discovery.
    - ``cwd`` is a fresh empty scratch dir — no workspace files to read.
    - No environment variables are set or overridden; auth is whatever the
      local ``claude`` CLI login provides.

    SDK failures raise :class:`LLMError` so callers keep their 502 mapping.
    ``close()`` removes the scratch dir (idempotent).
    """

    def __init__(
        self,
        model: str | None = None,
        timeout_s: float = DEFAULT_SDK_TIMEOUT_S,
        query_fn: SdkQueryFn | None = None,
        scratch_dir: str | Path | None = None,
    ) -> None:
        self._model = model
        self._timeout_s = timeout_s
        self._query_fn = query_fn if query_fn is not None else _sdk_query
        self._answered_model: str | None = None
        self._scratch_dir = Path(scratch_dir) if scratch_dir else self._make_scratch_dir()

    @property
    def model(self) -> str | None:
        """Model that answered the last call (SDK-reported, else configured).

        Falls back to the configured value when the SDK messages do not carry
        one, and to ``None`` when nothing is configured (CLI default).
        """
        return self._answered_model or self._model

    @staticmethod
    def _make_scratch_dir() -> Path:
        return Path(tempfile.mkdtemp(prefix="brain-ask-"))

    def close(self) -> None:
        """Remove the scratch dir (idempotent; missing dirs are ignored)."""
        shutil.rmtree(self._scratch_dir, ignore_errors=True)

    def complete(self, prompt: str) -> str:
        """Return the model's completion for ``prompt``.

        Uses ``asyncio.run`` internally: do NOT call from inside a running
        event loop (call from a worker thread instead — FastAPI sync routes
        already run in one).
        """
        logger.info("llm: provider=claude_sdk model=%s", self._model or "cli-default")
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
            tools=[],
            setting_sources=[],
            strict_mcp_config=True,
            permission_mode="default",
            system_prompt=prompt,
            max_turns=1,
            model=self._model,
            cwd=str(self._scratch_dir),
        )
        texts: list[str] = []
        answered_model: str | None = None
        async for message in self._query_fn(prompt=_USER_TURN, options=options):
            if isinstance(message, AssistantMessage):
                texts.extend(
                    block.text for block in message.content if isinstance(block, TextBlock)
                )
                if message.model:
                    answered_model = message.model
        self._answered_model = answered_model
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
