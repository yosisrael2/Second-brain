"""Tests for brain.ai.ask: prompt building, citations, and the Claude client."""

import json

import httpx
import pytest

from brain.ai.ask import (
    CITATION_RULES,
    ClaudeLLMClient,
    FakeLLMClient,
    LLMError,
    answer,
    build_prompt,
    extract_cited_ids,
)

ITEMS = [
    {"id": "item-1", "text": "The server runs on port 8000."},
    {"id": "item-2", "text": "Deployments go through Jenkins."},
]


class TestAnswer:
    def test_answer_cites_item_ids_present_in_text(self):
        llm = FakeLLMClient(reply="The server runs on [item-1] and deploys via [item-2].")

        result = answer("how do deploys work?", ITEMS, llm=llm)

        assert result["answer"].startswith("The server runs")
        assert result["cited_ids"] == ["item-1", "item-2"]

    def test_answer_without_citations_returns_empty_cited_ids(self):
        result = answer("question", ITEMS, llm=FakeLLMClient(reply="I have no idea."))

        assert result["cited_ids"] == []

    def test_cited_ids_only_include_known_context_items(self):
        reply = "Mentions [item-1] and a hallucinated [item-99]."

        result = answer("q", ITEMS, llm=FakeLLMClient(reply=reply))

        assert result["cited_ids"] == ["item-1"]

    def test_prompt_contains_question_sources_and_citation_rules(self):
        llm = FakeLLMClient()

        answer("what port?", ITEMS, llm=llm)

        prompt = llm.prompts[0]
        assert "Question: what port?" in prompt
        assert "- [item-1] The server runs on port 8000." in prompt
        assert "- [item-2] Deployments go through Jenkins." in prompt
        assert CITATION_RULES in prompt

    def test_item_without_text_is_serialized_into_prompt(self):
        llm = FakeLLMClient()

        answer("q", [{"id": "x", "payload": {"title": "T"}}], llm=llm)

        assert "[x]" in llm.prompts[0]
        assert "title" in llm.prompts[0]

    def test_answer_raises_llm_error_from_client(self):
        class ExplodingLLM:
            def complete(self, prompt: str) -> str:
                raise LLMError("boom")

        with pytest.raises(LLMError):
            answer("q", ITEMS, llm=ExplodingLLM())


class TestClaudeLLMClient:
    def test_sends_messages_api_request_and_extracts_text(self):
        seen = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["url"] = str(request.url)
            seen["key_header"] = request.headers.get("x-api-key")
            seen["version"] = request.headers.get("anthropic-version")
            seen["body"] = json.loads(request.content.decode("utf-8"))
            return httpx.Response(
                200,
                json={
                    "content": [
                        {"type": "text", "text": "Port "},
                        {"type": "tool_use", "id": "t1"},
                        {"type": "text", "text": "8000."},
                    ]
                },
            )

        client = ClaudeLLMClient(api_key="k", transport=httpx.MockTransport(handler))
        text = client.complete("which port?")

        assert seen["url"].endswith("/v1/messages")
        assert seen["key_header"] == "k"
        assert seen["version"] == "2023-06-01"
        assert seen["body"]["model"] == "claude-sonnet-5-5"
        assert seen["body"]["messages"] == [{"role": "user", "content": "which port?"}]
        assert text == "Port 8000."  # non-text blocks skipped, texts joined

    def test_model_override_via_config(self):
        seen = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["body"] = json.loads(request.content.decode("utf-8"))
            return httpx.Response(200, json={"content": [{"type": "text", "text": "ok"}]})

        client = ClaudeLLMClient(
            api_key="k", model="claude-opus-4-6", transport=httpx.MockTransport(handler)
        )
        client.complete("hi")

        assert seen["body"]["model"] == "claude-opus-4-6"

    def test_close_releases_the_http_client(self):
        client = ClaudeLLMClient(api_key="k")

        client.close()  # must not raise

    def test_missing_api_key_fails_before_any_request(self):
        calls = []

        def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover
            calls.append(request)
            return httpx.Response(200, json={"content": []})

        client = ClaudeLLMClient(api_key="", transport=httpx.MockTransport(handler))

        with pytest.raises(LLMError, match="no API key"):
            client.complete("hi")
        assert calls == []

    def test_timeout_maps_to_llm_error(self):
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectTimeout("slow", request=request)

        client = ClaudeLLMClient(api_key="k", transport=httpx.MockTransport(handler))
        with pytest.raises(LLMError, match="timed out"):
            client.complete("hi")

    def test_http_error_maps_to_llm_error(self):
        handler = lambda request: httpx.Response(500, text="nope")  # noqa: E731
        client = ClaudeLLMClient(api_key="k", transport=httpx.MockTransport(handler))

        with pytest.raises(LLMError, match="request failed"):
            client.complete("hi")

    @pytest.mark.parametrize(
        "response",
        [
            httpx.Response(200, json={"content": "not-a-list"}),
            httpx.Response(200, json={"content": []}),
            httpx.Response(200, json={"content": [{"type": "text", "text": "   "}]}),
            httpx.Response(200, json={"nope": 1}),
            httpx.Response(200, text="not-json"),
        ],
    )
    def test_malformed_responses_map_to_llm_error(self, response):
        client = ClaudeLLMClient(
            api_key="k", transport=httpx.MockTransport(lambda request: response)
        )

        with pytest.raises(LLMError, match="malformed"):
            client.complete("hi")


def test_extract_cited_ids_requires_bracket_syntax():
    assert extract_cited_ids("see [item-1] and [item-2]", ITEMS) == ["item-1", "item-2"]
    assert extract_cited_ids("plain item-1 mention", ITEMS) == []


def test_build_prompt_shapes_sources_as_bracketed_lines():
    prompt = build_prompt("q", ITEMS)
    lines = prompt.splitlines()
    assert lines[0] == "Question: q"
    assert lines[2] == "Sources:"
    assert lines[3].startswith("- [item-1]")
