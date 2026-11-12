"""rerank() behavior: ordering, truncation, and Jev-failure fallback."""

import pytest

from brain.ai.jev import JevError, JevScore
from brain.ai.rerank import rerank


class StubJev:
    def __init__(
        self, scores: list[JevScore] | None = None, error: Exception | None = None
    ) -> None:
        self._scores = scores or []
        self._error = error
        self.calls: list[tuple] = []

    def score_batch(self, question, levels, states):
        self.calls.append((question, list(levels), list(states)))
        if self._error is not None:
            raise self._error
        assert len(states) == len(self._scores), "stub scores must match candidates"
        return self._scores


CANDIDATES = [
    {"id": "a", "text": "quarterly revenue report"},
    {"id": "b", "text": "team meeting notes"},
    {"id": "c", "text": "renew TLS certificate"},
]


def test_rerank_orders_by_score_descending():
    jev = StubJev([JevScore(1.0, 0.9), JevScore(3.0, 0.9), JevScore(2.0, 0.9)])

    ranked = rerank("certificate expiry", CANDIDATES, top_n=3, jev=jev)

    assert [item["id"] for item in ranked] == ["b", "c", "a"]


def test_rerank_truncates_to_top_n():
    jev = StubJev([JevScore(1, 0.9), JevScore(3, 0.9), JevScore(2, 0.9)])

    ranked = rerank("q", CANDIDATES, top_n=2, jev=jev)

    assert [item["id"] for item in ranked] == ["b", "c"]


def test_rerank_ties_keep_original_order():
    tie = JevScore(2.0, 0.9)
    jev = StubJev([tie, tie, tie])

    ranked = rerank("q", CANDIDATES, top_n=3, jev=jev)

    assert [item["id"] for item in ranked] == ["a", "b", "c"]


def test_rerank_jev_parameter_is_required():
    """Omitting jev is a signature error (required param), not a guarded fallback."""
    with pytest.raises(TypeError):
        rerank("q", CANDIDATES)  # type: ignore[call-arg]


def test_rerank_jev_failure_returns_original_order():
    jev = StubJev(error=JevError("jev down"))

    ranked = rerank("q", CANDIDATES, top_n=2, jev=jev)

    assert [item["id"] for item in ranked] == ["a", "b"]


def test_rerank_truncates_fallback_to_top_n():
    jev = StubJev(error=JevError("jev down"))

    ranked = rerank("q", CANDIDATES, top_n=1, jev=jev)

    assert [item["id"] for item in ranked] == ["a"]


def test_rerank_empty_candidates_returns_empty_without_calling_jev():
    jev = StubJev(error=AssertionError("must not be called"))

    assert rerank("q", [], jev=jev) == []
    assert jev.calls == []


def test_rerank_missing_text_falls_back_to_serialized_candidate():
    seen = {}

    class CaptureJev(StubJev):
        def score_batch(self, question, levels, states):
            seen["states"] = states
            return [JevScore(1.0, 0.9) for _ in states]

    candidates = [{"id": "x", "payload": {"title": "no text key"}}]
    rerank("q", candidates, top_n=1, jev=CaptureJev())

    assert '"no text key"' in seen["states"][0]


def test_rerank_bounds_the_jev_payload():
    """48 long candidates must fit Jev's input limit (HTTP 400 max_tokens_exceeded)."""
    from brain.ai.rerank import STATE_CHAR_BUDGET

    candidates = [{"id": str(i), "text": "שלום " * 2000} for i in range(48)]
    jev = StubJev([JevScore(1.0, 0.9)] * 48)

    rerank("q", candidates, top_n=5, jev=jev)

    states = jev.calls[0][2]
    assert len(states) == 48
    assert sum(len(s) for s in states) <= STATE_CHAR_BUDGET
    assert all(s.startswith("שלום") for s in states)


def test_rerank_keeps_short_texts_whole():
    jev = StubJev([JevScore(1.0, 0.9)] * 3)

    rerank("q", CANDIDATES, top_n=3, jev=jev)

    assert jev.calls[0][2] == [c["text"] for c in CANDIDATES]


def test_jev_http_error_names_the_reason():
    """A 400 must say why (the body), not only the status line."""
    import httpx

    from brain.ai.jev import JevClient

    def handler(request):
        return httpx.Response(400, json={"detail": {"error_type": "max_tokens_exceeded"}})

    client = JevClient(api_key="k", transport=httpx.MockTransport(handler))
    with pytest.raises(JevError, match="HTTP 400.*max_tokens_exceeded"):
        client.score_batch("q", ["low", "high"], ["x"])
