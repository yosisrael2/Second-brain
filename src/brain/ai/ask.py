"""Answer questions over retrieved context items with a cited LLM response."""

from __future__ import annotations

import json
import os
from typing import Any, Callable, Mapping, Protocol, Sequence

import httpx

from ._http import post_json

DEFAULT_ANTHROPIC_URL = "https://api.anthropic.com/v1"
ANTHROPIC_VERSION = "2023-06-01"
DEFAULT_CLAUDE_MODEL = "claude-sonnet-4-5"
DEFAULT_MAX_TOKENS = 1024
DEFAULT_ANTHROPIC_TIMEOUT_S = 60.0

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


class ClaudeLLMClient:
    """Strategy producing completions via the Anthropic Messages API.

    Key/endpoint come from ``ANTHROPIC_API_KEY`` / ``ANTHROPIC_BASE_URL``
    unless overridden. Network or payload failures raise :class:`LLMError`.
    """

    def __init__(
        self,
        model: str | None = None,
        api_key: str | None = None,
        base_url: str | None = None,
        max_tokens: int = DEFAULT_MAX_TOKENS,
        timeout_s: float = DEFAULT_ANTHROPIC_TIMEOUT_S,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._model = model or os.getenv("ANTHROPIC_MODEL") or DEFAULT_CLAUDE_MODEL
        self._api_key = api_key
        self._max_tokens = max_tokens
        self._url = (base_url or os.getenv("ANTHROPIC_BASE_URL") or DEFAULT_ANTHROPIC_URL).rstrip("/") + "/messages"
        self._timeout_s = timeout_s
        self._http = httpx.Client(timeout=timeout_s, transport=transport)

    def complete(self, prompt: str) -> str:
        """Return the model's completion for ``prompt``."""
        api_key = self._api_key if self._api_key is not None else os.getenv("ANTHROPIC_API_KEY")
        if not api_key:
            raise LLMError("no API key: set ANTHROPIC_API_KEY (or pass api_key)")
        response = self._post(api_key, prompt)
        return self._extract_text(response)

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
                block.get("text", "") for block in blocks if isinstance(block, dict) and block.get("type") == "text"
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

    def __init__(self, reply: str | Callable[[str], str] = "No relevant information.") -> None:
        self._reply = reply
        self.prompts: list[str] = []

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
    lines += [f"- [{item['id']}] {item_text(item)}" for item in context_items]
    lines += ["", CITATION_RULES]
    return "\n".join(lines)


def answer(
    question: str,
    context_items: Sequence[Mapping[str, Any]],
    llm: LLMClient | None = None,
) -> dict[str, Any]:
    """Answer ``question`` from ``context_items``, citing item ids.

    Returns ``{answer, cited_ids}`` where ``cited_ids`` lists every context
    item id referenced in the answer text. LLM failures raise
    :class:`LLMError`; callers own the fallback policy.
    """
    llm = llm or ClaudeLLMClient()
    answer_text = llm.complete(build_prompt(question, context_items))
    return {"answer": answer_text, "cited_ids": extract_cited_ids(answer_text, context_items)}


def extract_cited_ids(answer_text: str, context_items: Sequence[Mapping[str, Any]]) -> list[str]:
    """Return the context item ids cited (as ``[id]``) in ``answer_text``."""
    return [item["id"] for item in context_items if f"[{item['id']}]" in answer_text]
