"""Answer questions over retrieved context items with a cited LLM response."""

from __future__ import annotations

import json
import logging
from collections.abc import Callable, Mapping, Sequence
from typing import Any, Protocol

import httpx

from brain.config import (
    DEFAULT_ANTHROPIC_TIMEOUT_S,
    DEFAULT_ANTHROPIC_URL,
    DEFAULT_CLAUDE_MODEL,
    DEFAULT_MAX_TOKENS,
)

from ._http import post_json

logger = logging.getLogger(__name__)

ANTHROPIC_VERSION = "2023-06-01"

CITATION_RULES = (
    "Answer using only the source items. Cite the item id in square brackets, "
    "like [item-7], for every claim you take from a source. If the sources do "
    "not contain the answer, say so."
)


class LLMError(RuntimeError):
    """An LLM request failed or returned an unusable payload."""


class LLMClient(Protocol):
    """Strategy interface for text generation backends."""

    def complete(self, prompt: str) -> str:
        """Return the model's completion for ``prompt``."""
        ...

    @property
    def model(self) -> str | None:
        """The model that answered the most recent ``complete`` call.

        ``None`` when the backend does not expose it (e.g. the Claude Code
        CLI default with no ``CLAUDE_SDK_MODEL`` configured).
        """
        ...

    def close(self) -> None:
        """Release underlying resources (may be a no-op)."""
        ...


class ClaudeLLMClient:
    """Strategy producing completions via the Anthropic Messages API.

    Key/endpoint come from ``ANTHROPIC_API_KEY`` / ``ANTHROPIC_BASE_URL``
    unless overridden. Network or payload failures raise :class:`LLMError`.
    """

    def __init__(
        self,
        api_key: str | None = None,
        model: str = DEFAULT_CLAUDE_MODEL,
        base_url: str = DEFAULT_ANTHROPIC_URL,
        max_tokens: int = DEFAULT_MAX_TOKENS,
        timeout_s: float = DEFAULT_ANTHROPIC_TIMEOUT_S,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._model = model
        self._api_key = api_key
        self._max_tokens = max_tokens
        self._url = base_url.rstrip("/") + "/messages"
        self._timeout_s = timeout_s
        self._answered_model: str | None = None
        self._http = httpx.Client(timeout=timeout_s, transport=transport)

    @property
    def model(self) -> str | None:
        """Model that answered the last call (API-reported, else configured)."""
        return self._answered_model

    def close(self) -> None:
        """Release the underlying HTTP connection pool."""
        self._http.close()

    def complete(self, prompt: str) -> str:
        """Return the model's completion for ``prompt``."""
        if not self._api_key:
            raise LLMError("no API key configured for the LLM (ANTHROPIC_API_KEY)")
        logger.info("llm: provider=anthropic model=%s", self._model)
        response = self._post(self._api_key, prompt)
        self._answered_model = self._extract_model(response) or self._model
        return self._extract_text(response)

    @staticmethod
    def _extract_model(response: httpx.Response) -> str | None:
        """Best-effort model id from the API response (None when absent)."""
        try:
            model = response.json().get("model")
        except Exception:  # noqa: BLE001 - text extraction reports malformed payloads
            return None
        return model if isinstance(model, str) and model else None

    def _post(self, api_key: str, prompt: str) -> httpx.Response:
        return post_json(
            self._http,
            self._url,
            headers={
                "x-api-key": api_key,
                "anthropic-version": ANTHROPIC_VERSION,
                "Content-Type": "application/json",
            },
            payload={
                "model": self._model,
                "max_tokens": self._max_tokens,
                "messages": [{"role": "user", "content": prompt}],
            },
            error=LLMError,
            context="llm",
            timeout_s=self._timeout_s,
        )

    @staticmethod
    def _extract_text(response: httpx.Response) -> str:
        try:
            blocks = response.json()["content"]
            if not isinstance(blocks, list):
                raise TypeError("content is not a list")
            text = "".join(
                block.get("text", "")
                for block in blocks
                if isinstance(block, dict) and block.get("type") == "text"
            )
            if not text.strip():
                raise ValueError("no non-empty text blocks")
            return text
        except (TypeError, ValueError, KeyError, AttributeError) as exc:
            raise LLMError(f"malformed llm response: {exc!r}") from exc


class FakeLLMClient:
    """Deterministic LLMClient stub for tests.

    ``reply`` is either a fixed string or a callable receiving the prompt.
    ``prompts`` records every prompt it was given.
    """

    def __init__(
        self,
        reply: str | Callable[[str], str] = "No relevant information.",
        model: str | None = "fake-llm",
    ) -> None:
        self._reply = reply
        self.model = model
        self.prompts: list[str] = []

    def close(self) -> None:
        """No-op: mirrors the real clients' lifecycle method."""

    def complete(self, prompt: str) -> str:
        self.prompts.append(prompt)
        if callable(self._reply):
            return self._reply(prompt)
        return self._reply


def item_text(item: Mapping[str, Any]) -> str:
    """Best-effort text for an item: its ``text`` field, else the serialized item."""
    text = item.get("text")
    if isinstance(text, str) and text.strip():
        return text
    return json.dumps(item, default=str)


def build_prompt(question: str, context_items: Sequence[Mapping[str, Any]]) -> str:
    """Render the answer prompt: question, bracketed sources, citation rules."""
    lines = [f"Question: {question}", "", "Sources:"]
    lines += [f"- [{item['id']}] {item_text(item)}" for item in context_items if item.get("id")]
    lines += ["", CITATION_RULES]
    return "\n".join(lines)


def answer(
    question: str,
    context_items: Sequence[Mapping[str, Any]],
    llm: LLMClient,
) -> dict[str, Any]:
    """Answer ``question`` from ``context_items``, citing item ids.

    Returns ``{answer, cited_ids, model}`` where ``cited_ids`` lists every context
    item id referenced in the answer text and ``model`` is the model that
    answered (None when the backend does not expose one). LLM failures raise
    :class:`LLMError`; callers own the fallback policy.
    """
    answer_text = llm.complete(build_prompt(question, context_items))
    return {
        "answer": answer_text,
        "cited_ids": extract_cited_ids(answer_text, context_items),
        "model": getattr(llm, "model", None),
    }


def extract_cited_ids(answer_text: str, context_items: Sequence[Mapping[str, Any]]) -> list[str]:
    """Return the context item ids cited (as ``[id]``) in ``answer_text``."""
    return [
        item["id"]
        for item in context_items
        if item.get("id") and f"[{item['id']}]" in answer_text
    ]
