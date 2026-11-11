"""Re-rank retrieved candidates by relevance to a question, via Jev scoring."""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from .ask import item_text
from .jev import JevClientProtocol, JevError

logger = logging.getLogger(__name__)

RANK_LEVELS: tuple[str, ...] = (
    "Irrelevant: does not relate to the question at all",
    "Tangential: touches the topic but does not answer it",
    "Related: useful background for the question",
    "Directly answers the question",
)
DEFAULT_TOP_N = 5
# Jev rejects a request whose input is too large (HTTP 400
# max_tokens_exceeded; measured 11 Nov 2026: 48 Hebrew candidates of 1,000
# chars = ~32k input tokens pass, 1,500 chars fail). /ask sends up to
# ask_recall_pool + ask_kid_recall_limit (48) full item texts, and one long
# email was enough to fail every rerank, silently falling back to vector
# order. Each candidate is cut to its share of this budget (relevance is
# judged on the head of the text), never below the floor.
STATE_CHAR_BUDGET = 24_000
MIN_CANDIDATE_CHARS = 300


def rerank(
    question: str,
    candidates: Sequence[Mapping[str, Any]],
    jev: JevClientProtocol,
    top_n: int = DEFAULT_TOP_N,
) -> list[Mapping[str, Any]]:
    """Return up to ``top_n`` candidates, most relevant first.

    Each candidate is a mapping with ``id`` and ``text`` keys; ``jev`` is the
    required scoring dependency (build one in the DI factory). Scores come
    from one batched Jev call. If Jev is unreachable or returns malformed
    data, the candidates are returned in their original order (trust the
    vector search ranking); this function never raises.
    """
    if not candidates:
        return []
    try:
        scores = jev.score_batch(
            f"How relevant is this candidate to the question: {question}",
            list(RANK_LEVELS),
            _bounded_texts(candidates),
        )
    except JevError as exc:
        logger.warning("rerank: jev unavailable (%s); keeping original order", exc)
        return list(candidates)[:top_n]
    ranked = sorted(zip(scores, candidates, strict=True), key=lambda pair: -pair[0].score)
    return [candidate for _, candidate in ranked][:top_n]


def _bounded_texts(candidates: Sequence[Mapping[str, Any]]) -> list[str]:
    """Candidate texts, each cut to its share of :data:`STATE_CHAR_BUDGET`."""
    cap = max(MIN_CANDIDATE_CHARS, STATE_CHAR_BUDGET // len(candidates))
    return [item_text(candidate)[:cap] for candidate in candidates]


@dataclass(frozen=True)
class JevReranker:
    """Adapter exposing :func:`rerank` as an injectable Strategy object."""

    jev: JevClientProtocol

    def rerank(
        self,
        question: str,
        candidates: Sequence[Mapping[str, Any]],
        top_n: int = DEFAULT_TOP_N,
    ) -> list[Mapping[str, Any]]:
        """Rerank ``candidates`` by Jev-scored relevance to ``question``."""
        return rerank(question, candidates, self.jev, top_n)
