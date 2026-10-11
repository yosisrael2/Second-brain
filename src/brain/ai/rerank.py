"""Re-rank retrieved candidates by relevance to a question, via Jev scoring."""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from typing import Any

from .ask import item_text
from .jev import JevClient, JevClientProtocol, JevError

logger = logging.getLogger(__name__)

RANK_LEVELS: tuple[str, ...] = (
    "Irrelevant: does not relate to the question at all",
    "Tangential: touches the topic but does not answer it",
    "Related: useful background for the question",
    "Directly answers the question",
)
DEFAULT_TOP_N = 5


def rerank(
    question: str,
    candidates: Sequence[Mapping[str, Any]],
    top_n: int = DEFAULT_TOP_N,
    jev: JevClientProtocol | None = None,
) -> list[Mapping[str, Any]]:
    """Return up to ``top_n`` candidates, most relevant first.

    Each candidate is a mapping with ``id`` and ``text`` keys. Scores come
    from one batched Jev call. If Jev is unreachable or returns malformed
    data, the candidates are returned in their original order (trust the
    vector search ranking); this function never raises.
    """
    if not candidates:
        return []
    jev = jev or JevClient()
    try:
        scores = jev.score_batch(
            f"How relevant is this candidate to the question: {question}",
            list(RANK_LEVELS),
            [item_text(candidate) for candidate in candidates],
        )
    except JevError as exc:
        logger.warning("rerank: jev unavailable (%s); keeping original order", exc)
        return list(candidates)[:top_n]
    ranked = sorted(zip(scores, candidates, strict=True), key=lambda pair: -pair[0].score)
    return [candidate for _, candidate in ranked][:top_n]
