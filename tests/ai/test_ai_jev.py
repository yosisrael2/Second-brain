"""Unit tests for the Jev client (all HTTP via httpx.MockTransport, no network)."""

import json

import httpx
import pytest

from brain.ai.jev import DEFAULT_BASE_URL, JevClient, JevError, parse_choice, parse_score


def make_jev(handler, api_key="test-key", **kwargs) -> JevClient:
    return JevClient(api_key=api_key, transport=httpx.MockTransport(handler), **kwargs)


def jev_questions_body(request: httpx.Request) -> dict:
    return json.loads(request.content.decode("utf-8"))


class TestRequestShape:
    def test_posts_single_choice_question_with_bearer_key(self):
        seen = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["url"] = str(request.url)
            seen["auth"] = request.headers.get("Authorization")
            seen["body"] = jev_questions_body(request)
            return httpx.Response(
                200, json={"answers": {"q": {"option": "task", "confidence": 0.93}}}
            )

        jev = make_jev(handler, base_url="https://jev.example/v1/systemone")
        result = jev.choice("What is this?", ["task", "note"], "buy milk tomorrow")

        assert seen["url"] == "https://jev.example/v1/systemone"
        assert seen["auth"] == "Bearer test-key"
        assert seen["body"]["model"] == "jev-latest"
        assert seen["body"]["state"] == "buy milk tomorrow"
        question = seen["body"]["questions"]["q"]
        assert question["type"] == "choice"
        assert question["criteria"] == {"task": "task", "note": "note"}
        assert result.option == "task"
        assert result.confidence == pytest.approx(0.93)

    def test_options_map_is_forwarded_as_criteria(self):
        seen = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["body"] = jev_questions_body(request)
            return httpx.Response(200, json={"answers": {"q": {"option": "a", "confidence": 0.5}}})

        jev = make_jev(handler)
        jev.choice("Pick", {"a": "first option", "b": "second option"}, "state")

        assert seen["body"]["questions"]["q"]["criteria"] == {
            "a": "first option",
            "b": "second option",
        }


class TestScoreBatching:
    def test_score_batch_sends_one_call_with_indexed_questions(self):
        seen = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["body"] = jev_questions_body(request)
            answers = {f"q{i}": {"score": 3 - i, "confidence": 0.8} for i in range(3)}
            return httpx.Response(200, json={"answers": answers})

        jev = make_jev(handler)
        scores = jev.score_batch(
            "Relevance?", ["irrelevant", "related"], ["c0 text", "c1 text", "c2 text"]
        )

        assert isinstance(seen["body"]["state"], list)
        assert len(seen["body"]["questions"]) == 3
        assert [s.score for s in scores] == [3.0, 2.0, 1.0]
        assert all(s.confidence == pytest.approx(0.8) for s in scores)

    def test_score_batch_empty_states_skips_api_call(self):
        def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover
            raise AssertionError("no request expected")

        jev = make_jev(handler)
        assert jev.score_batch("q", ["a", "b"], []) == []


class TestFailureModes:
    def test_missing_api_key_raises_without_network(self):
        calls = []

        def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover
            calls.append(request)
            return httpx.Response(200, json={})

        jev = JevClient(api_key="", transport=httpx.MockTransport(handler))
        with pytest.raises(JevError, match="no API key"):
            jev.choice("q", ["a", "b"], "state")
        assert calls == []

    def test_timeout_maps_to_jev_error(self):
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectTimeout("too slow", request=request)

        jev = make_jev(handler)
        with pytest.raises(JevError, match="timed out"):
            jev.score("q", ["low", "high"], "state")

    def test_http_error_maps_to_jev_error(self):
        jev = make_jev(lambda request: httpx.Response(500, text="boom"))
        with pytest.raises(JevError, match="500"):
            jev.score("q", ["low", "high"], "state")

    def test_invalid_json_maps_to_jev_error(self):
        jev = make_jev(lambda request: httpx.Response(200, text="not json"))
        with pytest.raises(JevError, match="invalid JSON"):
            jev.choice("q", ["a", "b"], "state")

    def test_missing_answers_object_maps_to_jev_error(self):
        jev = make_jev(lambda request: httpx.Response(200, json={"unexpected": True}))
        with pytest.raises(JevError, match="answers"):
            jev.choice("q", ["a", "b"], "state")

    def test_missing_question_answer_maps_to_jev_error(self):
        jev = make_jev(lambda request: httpx.Response(200, json={"answers": {}}))
        with pytest.raises(JevError, match="missing answers for: q"):
            jev.choice("q", ["a", "b"], "state")


class TestParsing:
    def test_parse_choice_derives_option_from_probabilities(self):
        answer = parse_choice({"probabilities": {"task": 0.6, "note": 0.4}})
        assert answer.option == "task"
        assert answer.confidence == pytest.approx(0.6)
        assert answer.probabilities == {"task": 0.6, "note": 0.4}

    def test_parse_choice_rejects_garbage(self):
        with pytest.raises(JevError):
            parse_choice("nope")
        with pytest.raises(JevError):
            parse_choice({})
        with pytest.raises(JevError):
            parse_choice({"option": "task", "probabilities": {"task": "high"}})

    def test_parse_score_rejects_garbage(self):
        with pytest.raises(JevError):
            parse_score({"score": "high", "confidence": 0.5})
        with pytest.raises(JevError):
            parse_score({"score": 3})  # missing confidence

    def test_confidence_is_clamped(self):
        assert parse_choice({"option": "a", "confidence": 1.7}).confidence == 1.0
        assert parse_score({"score": 2, "confidence": -3}).confidence == 0.0


def test_default_base_url_matches_server_js():
    jev = JevClient(api_key="k")
    assert jev._base_url == DEFAULT_BASE_URL  # noqa: SLF001 - config assertion
