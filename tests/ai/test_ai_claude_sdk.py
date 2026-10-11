"""Tests for ClaudeSdkLLMClient: real SDK message DTOs, faked transport."""

from collections.abc import AsyncIterator

import pytest
from claude_agent_sdk import AssistantMessage, ProcessError, ResultError, TextBlock

from brain.ai.ask import LLMError
from brain.ai.claude_sdk import ClaudeSdkLLMClient


class FakeQuery:
    """Records options and yields canned SDK messages (or raises)."""

    def __init__(self, messages: list | Exception | None = None) -> None:
        self.messages = messages if messages is not None else []
        self.calls: list[dict] = []

    def __call__(self, *, prompt: str, options) -> AsyncIterator:  # noqa: ANN001 - SDK shape
        self.calls.append({"prompt": prompt, "options": options})

        async def gen() -> AsyncIterator:
            if isinstance(self.messages, Exception):
                raise self.messages
            for message in self.messages:
                yield message

        return gen()


def test_complete_returns_joined_assistant_text():
    fake = FakeQuery(
        [
            AssistantMessage(content=[TextBlock(text="The ")], model="claude-sonnet-4-5"),
            AssistantMessage(content=[TextBlock(text="4")], model="claude-sonnet-4-5"),
        ]
    )
    client = ClaudeSdkLLMClient(query_fn=fake)

    assert client.complete("What is 2+2?") == "The 4"


def test_complete_configures_a_locked_down_single_turn_session():
    fake = FakeQuery([AssistantMessage(content=[TextBlock(text="ok")], model="m")])
    client = ClaudeSdkLLMClient(model="claude-sonnet-4-5", query_fn=fake)

    client.complete("question + sources + citation rules")

    options = fake.calls[0]["options"]
    assert options.allowed_tools == []
    assert options.max_turns == 1
    assert options.permission_mode == "default"
    assert options.model == "claude-sonnet-4-5"
    # The full ask prompt rides as the system prompt.
    assert options.system_prompt == "question + sources + citation rules"
    # The user turn is only a nudge; no credential-bearing env is passed.
    assert fake.calls[0]["prompt"] == "Answer the request in your system prompt."
    assert options.env is None or options.env == {}


def test_complete_maps_sdk_process_error_to_llm_error():
    fake = FakeQuery(ProcessError("process died"))
    client = ClaudeSdkLLMClient(query_fn=fake)

    with pytest.raises(LLMError, match="claude sdk failed"):
        client.complete("prompt")


def test_complete_maps_result_error_to_llm_error():
    client = ClaudeSdkLLMClient(query_fn=FakeQuery(ResultError("max turns")))

    with pytest.raises(LLMError, match="claude sdk failed"):
        client.complete("prompt")


def test_complete_maps_timeout_to_llm_error():
    import asyncio

    class SlowQuery:
        def __call__(self, *, prompt: str, options) -> AsyncIterator:  # noqa: ANN001
            async def gen() -> AsyncIterator:
                await asyncio.sleep(5)
                yield AssistantMessage(content=[TextBlock(text="late")], model="m")

            return gen()

    client = ClaudeSdkLLMClient(timeout_s=0.01, query_fn=SlowQuery())

    with pytest.raises(LLMError, match="timed out"):
        client.complete("prompt")


def test_complete_empty_response_is_an_error():
    fake = FakeQuery([AssistantMessage(content=[], model="m")])
    client = ClaudeSdkLLMClient(query_fn=fake)

    with pytest.raises(LLMError, match="no text"):
        client.complete("prompt")
