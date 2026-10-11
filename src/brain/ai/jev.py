"""Jev typed-decision client (Adapter over the TypeSafe Jev API)."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Mapping, Protocol, Sequence

import httpx

from ._http import post_json

DEFAULT_BASE_URL = "https://api.typesafe.ai/v1/systemone"
DEFAULT_MODEL = "jev-latest"
DEFAULT_TIMEOUT_S = 5.0


class JevError(RuntimeError):
    """The Jev API could not be reached or returned an unusable payload.

    Messages never contain the API key or the Authorization header.
    """


@dataclass(frozen=True)
class JevChoice:
    """A Jev "choice" answer: winning option, its probability, and the rest."""

    option: str
    confidence: float
    probabilities: Mapping[str, float] = field(default_factory=dict)


@dataclass(frozen=True)
class JevScore:
    """A Jev "score" answer on a caller-supplied low->high rubric."""

    score: float
    confidence: float


class JevClientProtocol(Protocol):
    """The slice of JevClient that classify() and rerank() depend on."""

    def decide(self, state: Any, questions: Mapping[str, dict[str, Any]]) -> dict[str, Any]:
        """Run one or more questions against one state in a single API call."""
        ...

    def score_batch(self, question: str, levels: Sequence[str], states: Sequence[Any]) -> list[JevScore]:
        """Score many states against one question in a single API call."""
        ...


def _as_criteria(options: Mapping[str, str] | Sequence[str]) -> dict[str, str]:
    if isinstance(options, Mapping):
        return dict(options)
    return {str(option): str(option) for option in options}


def _clamp01(value: float) -> float:
    return min(1.0, max(0.0, value))


def parse_choice(answer: Any) -> JevChoice:
    """Parse one Jev "choice" answer; raises JevError when malformed."""
    if not isinstance(answer, Mapping):
        raise JevError(f"malformed choice answer: expected object, got {type(answer).__name__}")
    probabilities = _parse_probabilities(answer.get("probabilities"))
    option = answer.get("option")
    if option is None and probabilities:
        option = max(probabilities, key=probabilities.__getitem__)
    if option is None:
        raise JevError("malformed choice answer: no 'option' and no 'probabilities'")
    option = str(option)
    confidence_raw = answer.get("confidence")
    if confidence_raw is None:
        confidence_raw = probabilities.get(option)
    try:
        confidence = float(confidence_raw)
    except (TypeError, ValueError) as exc:
        raise JevError("malformed choice answer: no usable 'confidence'") from exc
    return JevChoice(option=option, confidence=_clamp01(confidence), probabilities=probabilities)


def _parse_probabilities(raw: Any) -> dict[str, float]:
    if raw is None:
        return {}
    if not isinstance(raw, Mapping):
        raise JevError("malformed choice answer: 'probabilities' is not an object")
    probabilities = {}
    for key, value in raw.items():
        try:
            probabilities[str(key)] = float(value)
        except (TypeError, ValueError) as exc:
            raise JevError(f"malformed choice answer: probability for {key!r} is not a number") from exc
    return probabilities


def parse_score(answer: Any) -> JevScore:
    """Parse one Jev "score" answer; raises JevError when malformed."""
    if not isinstance(answer, Mapping):
        raise JevError(f"malformed score answer: expected object, got {type(answer).__name__}")
    try:
        score = float(answer["score"])
        confidence = float(answer["confidence"])
    except (KeyError, TypeError, ValueError) as exc:
        raise JevError("malformed score answer: 'score' and 'confidence' must be numbers") from exc
    return JevScore(score=score, confidence=_clamp01(confidence))


class JevClient:
    """Adapter for the Jev typed-decision API.

    One POST carries a single ``state`` plus one or more ``questions`` (the
    API answers them in parallel), matching the request shape used by
    ``~/.claude/mcp/jev/server.js``. Key/endpoint/model/timeout come from
    ``JEV_API_KEY`` / ``JEV_BASE_URL`` / ``JEV_MODEL`` / ``JEV_TIMEOUT_MS``
    unless overridden. Any failure raises :class:`JevError`; callers own the
    fallback policy. The API key lives only in the Authorization header: it
    is never logged and never appears in exception messages.
    """

    def __init__(
        self,
        api_key: str | None = None,
        base_url: str | None = None,
        model: str | None = None,
        timeout_s: float | None = None,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._api_key = api_key
        self._base_url = (base_url or os.getenv("JEV_BASE_URL") or DEFAULT_BASE_URL).rstrip("/")
        self._model = model or os.getenv("JEV_MODEL") or DEFAULT_MODEL
        self._timeout_s = self._resolve_timeout(timeout_s)
        self._http = httpx.Client(timeout=self._timeout_s, transport=transport)

    @staticmethod
    def _resolve_timeout(timeout_s: float | None) -> float:
        if timeout_s is not None:
            return timeout_s
        env_ms = os.getenv("JEV_TIMEOUT_MS")
        return float(env_ms) / 1000.0 if env_ms else DEFAULT_TIMEOUT_S

    def decide(self, state: Any, questions: Mapping[str, dict[str, Any]]) -> dict[str, Any]:
        """POST all ``questions`` against one ``state``; returns raw answers by id."""
        api_key = self._api_key if self._api_key is not None else os.getenv("JEV_API_KEY")
        if not api_key:
            raise JevError("no API key: set JEV_API_KEY (or pass api_key)")
        payload = {"model": self._model, "state": state, "questions": dict(questions)}
        response = self._post(payload, api_key)
        return self._parse_answers(response, requested=list(questions))

    def choice(
        self,
        question: str,
        options: Mapping[str, str] | Sequence[str],
        state: Any,
    ) -> JevChoice:
        """Ask one "choice" question; returns the option, confidence, probabilities."""
        answers = self.decide(
            state,
            {"q": {"type": "choice", "instructions": question, "criteria": _as_criteria(options)}},
        )
        return parse_choice(answers["q"])

    def score(self, question: str, levels: Sequence[str], state: Any) -> JevScore:
        """Ask one "score" question; ``levels`` is ordered low -> high."""
        return self.score_batch(question, levels, [state])[0]

    def score_batch(self, question: str, levels: Sequence[str], states: Sequence[Any]) -> list[JevScore]:
        """Score many candidates against one question in a single API call.

        Candidates travel as one list under ``state``; each question pins its
        candidate by index. Raises JevError if any answer is malformed.
        """
        if not states:
            return []
        questions = {
            f"q{i}": {
                "type": "score",
                "instructions": f"{question} (evaluate only the candidate at index {i})",
                "criteria": list(levels),
            }
            for i in range(len(states))
        }
        answers = self.decide(list(states), questions)
        return [parse_score(answers[f"q{i}"]) for i in range(len(states))]

    def _post(self, payload: dict[str, Any], api_key: str) -> httpx.Response:
        return post_json(
            self._http,
            self._base_url,
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            payload=payload,
            error=JevError,
            context="jev",
            timeout_s=self._timeout_s,
        )

    @staticmethod
    def _parse_answers(response: httpx.Response, requested: Sequence[str]) -> dict[str, Any]:
        try:
            data = response.json()
        except ValueError as exc:
            raise JevError(f"jev returned invalid JSON: {response.text[:200]}") from exc
        answers = data.get("answers") if isinstance(data, Mapping) else None
        if not isinstance(answers, dict):
            raise JevError("jev response is missing an 'answers' object")
        missing = [qid for qid in requested if qid not in answers]
        if missing:
            raise JevError(f"jev response is missing answers for: {', '.join(missing)}")
        return answers
