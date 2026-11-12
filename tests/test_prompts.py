"""Editable prompts + the user memory file (brain.services.prompts, /prompts).

Pins the contract the feature promises:
- with no overrides every prompt renders byte-identical to the hard-coded
  text (sha256 of each default pinned against origin/main at branch time);
- an edit that drops or adds a placeholder the code formats is rejected
  (an edit can never crash at format time);
- lookup falls back to the default on any DB error, and caches;
- an overridden extraction / triage prompt records a new version;
- the memory-file block rides in EVERY LLM prompt when set, absent when empty;
- the API: list / get / put / reset / restore / memory file, history kept.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text as sql_text
from tests.conftest import assert_test_database

from brain.ai.ask import CITATION_RULES, CONFLICT_RULES, build_prompt
from brain.db.models import MemoryItem
from brain.db.session import engine
from brain.services import prompts
from brain.services.prompts import (
    MEMORY_FILE_KEY,
    PromptValidationError,
    ResolvedPrompt,
    memory_file_block,
    template_fields,
    validate,
    version_tag,
)

# sha256 of every code default, computed from origin/main (342e1ff) before
# this change: the registry must hand out the hard-coded texts unchanged.
_DEFAULT_SHA256 = {
    "ask.citation_rules": "2ba10f7a4cbfe451265addae0ff31188a7b12c5c15b0e905ef3ab792066983ab",
    "ask.conflict_rules": "3a5b00f1a3d320d453fc31456b25ecf3af31888d9438e3f4a822298f6b03a3e2",
    "extraction": "105aac11c83bd61f1f76dc2ed1c7a30a07ab3a4dd6353d9f3b873580b5469eba",
    "recon_judge": "b18e19c8d571b7e48f088ea184df569462b52f683ca4ae5c270eb85592db5bd0",
    "whatsapp_triage": "c42f167d59b98cdf89a64c8475a515d013f416f532d3160a609032cb24cae896",
    "attachments.read_instruction": (
        "635cc27bd926af64d99dccdaa5198b5915783008d724a491002b42f9dc707b4b"
    ),
    MEMORY_FILE_KEY: hashlib.sha256(b"").hexdigest(),
}

MEMORY_TEXT = "# Family\nRomi is in grade 5 — רומי בכיתה ה׳.\nAlways answer in Hebrew."


# --- fixtures ---------------------------------------------------------------------


def _set_overrides(monkeypatch: pytest.MonkeyPatch, overrides: dict[str, tuple[int, str]]) -> None:
    """Serve ``overrides`` from the (mocked) DB and drop the cache."""
    monkeypatch.setattr(prompts, "_load_overrides", lambda: dict(overrides))
    prompts.invalidate_cache()


@pytest.fixture(autouse=True)
def _fresh_cache() -> Iterator[None]:
    prompts.invalidate_cache()
    yield
    prompts.invalidate_cache()


@pytest.fixture(name="no_db_overrides")
def no_db_overrides_fixture(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_overrides(monkeypatch, {})


def _wipe_prompt_rows() -> None:
    assert_test_database(str(engine.url))
    with engine.begin() as conn:
        conn.execute(sql_text("DELETE FROM prompt_versions"))
    prompts.invalidate_cache()


@pytest.fixture(name="prompt_db")
def prompt_db_fixture(database: None) -> Iterator[None]:
    _wipe_prompt_rows()
    yield
    _wipe_prompt_rows()


# --- registry defaults ----------------------------------------------------------------


def test_registry_lists_every_llm_prompt_and_the_memory_file() -> None:
    assert list(prompts.specs()) == list(_DEFAULT_SHA256)


@pytest.mark.parametrize("key", list(_DEFAULT_SHA256))
def test_defaults_are_byte_identical_to_the_hard_coded_texts(key: str) -> None:
    default = prompts.get_spec(key).default
    assert hashlib.sha256(default.encode("utf-8")).hexdigest() == _DEFAULT_SHA256[key]


def test_defaults_are_the_module_constants_themselves() -> None:
    from brain.services.attachments import _READ_INSTRUCTION
    from brain.services.extraction.judge import _LLM_PROMPT_TEMPLATE
    from brain.services.extraction.llm_extractor import _PROMPT_TEMPLATE as extraction
    from brain.services.triage import _PROMPT_TEMPLATE as triage

    assert prompts.get_spec("ask.citation_rules").default is CITATION_RULES
    assert prompts.get_spec("ask.conflict_rules").default is CONFLICT_RULES
    assert prompts.get_spec("extraction").default is extraction
    assert prompts.get_spec("recon_judge").default is _LLM_PROMPT_TEMPLATE
    assert prompts.get_spec("whatsapp_triage").default is triage
    assert prompts.get_spec("attachments.read_instruction").default is _READ_INSTRUCTION


@pytest.mark.parametrize("key", list(_DEFAULT_SHA256))
def test_every_default_passes_its_own_validation(key: str) -> None:
    spec = prompts.get_spec(key)
    validate(key, spec.default)
    if spec.is_template:
        assert template_fields(spec.default) == set(spec.placeholders)


def test_template_placeholders_match_what_the_code_formats() -> None:
    assert prompts.get_spec("extraction").placeholders == {
        "sent_local",
        "tz_name",
        "today_local",
        "memory_context",
        "email_text",
    }
    assert prompts.get_spec("recon_judge").placeholders == {"candidate", "existing", "version"}
    assert prompts.get_spec("whatsapp_triage").placeholders == {"text", "version"}
    for key in ("ask.citation_rules", "ask.conflict_rules", "attachments.read_instruction"):
        assert prompts.get_spec(key).placeholders == frozenset()


# --- validation ---------------------------------------------------------------------


def _triage_text(body: str) -> str:
    return body + "\nConversation:\n{text}\nPrompt version: {version}"


def test_dropping_a_placeholder_is_rejected() -> None:
    with pytest.raises(PromptValidationError, match=r"missing placeholder\(s\): \{version\}"):
        validate("whatsapp_triage", "Label it.\n{text}")


def test_adding_an_unknown_placeholder_is_rejected() -> None:
    with pytest.raises(PromptValidationError, match=r"unknown placeholder\(s\): \{kid\}"):
        validate("whatsapp_triage", _triage_text("Label it for {kid}."))


@pytest.mark.parametrize(
    "body, reason",
    [
        ('Answer {"label": "x"}', "not allowed"),  # an undoubled JSON brace
        ("a lone } brace", "malformed braces"),
        ("a lone { brace", "malformed braces"),
        ("positional {} field", "positional"),
        ("positional {0} field", "positional"),
        ("attribute {text.upper}", "not allowed"),
        ("index {text[0]}", "not allowed"),
        ("conversion {text!r}", "conversion or format spec"),
        ("format spec {text:>10}", "conversion or format spec"),
    ],
)
def test_texts_that_would_crash_or_misrender_at_format_time_are_rejected(
    body: str, reason: str
) -> None:
    with pytest.raises(PromptValidationError, match=reason):
        validate("whatsapp_triage", _triage_text(body))


def test_a_valid_edit_formats_cleanly() -> None:
    text = _triage_text('Be strict. JSON: {{"label": "noise"}}')
    validate("whatsapp_triage", text)
    assert '{"label": "noise"}' in text.format(text="hi", version="v")


def test_non_template_prompts_take_braces_verbatim() -> None:
    # the ask rules / attachment instruction are never .format()-ed
    validate("ask.citation_rules", "Cite like {this} and } that.")


def test_empty_prompt_rejected_but_empty_memory_file_allowed() -> None:
    with pytest.raises(PromptValidationError, match="must not be empty"):
        validate("ask.citation_rules", "   \n")
    validate(MEMORY_FILE_KEY, "")


def test_length_limits() -> None:
    validate(MEMORY_FILE_KEY, "x" * prompts.MEMORY_FILE_MAX_CHARS)
    with pytest.raises(PromptValidationError, match="limit"):
        validate(MEMORY_FILE_KEY, "x" * (prompts.MEMORY_FILE_MAX_CHARS + 1))
    with pytest.raises(PromptValidationError, match="limit"):
        validate("ask.citation_rules", "x" * (prompts.PROMPT_MAX_CHARS + 1))


def test_unknown_key_is_a_key_error() -> None:
    with pytest.raises(KeyError):
        validate("nope", "x")


# --- lookup: override, fallback, cache ---------------------------------------------


def test_resolve_returns_the_override(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_overrides(monkeypatch, {"ask.citation_rules": (3, "Cite everything.")})
    resolved = prompts.resolve("ask.citation_rules")
    assert resolved == ResolvedPrompt("ask.citation_rules", "Cite everything.", 3)
    assert resolved.overridden
    assert prompts.resolve("ask.conflict_rules") == ResolvedPrompt(
        "ask.conflict_rules", CONFLICT_RULES
    )


def test_db_error_falls_back_to_defaults_with_a_warning(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    def broken() -> dict[str, tuple[int, str]]:
        raise RuntimeError("database is down")

    monkeypatch.setattr(prompts, "_load_overrides", broken)
    prompts.invalidate_cache()
    with caplog.at_level(logging.WARNING, logger="brain.services.prompts"):
        resolved = prompts.resolve("extraction")
    assert resolved.text == prompts.get_spec("extraction").default
    assert resolved.version is None
    assert "override lookup failed" in caplog.text


def test_lookup_is_cached_between_calls(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = []

    def load() -> dict[str, tuple[int, str]]:
        calls.append(1)
        return {}

    monkeypatch.setattr(prompts, "_load_overrides", load)
    prompts.invalidate_cache()
    for _ in range(5):
        prompts.resolve("extraction")
        prompts.resolve(MEMORY_FILE_KEY)
    assert len(calls) == 1
    # an expired entry reloads
    monkeypatch.setattr(prompts, "CACHE_TTL_S", -1.0)
    prompts.invalidate_cache()
    prompts.resolve("extraction")
    prompts.resolve("extraction")
    assert len(calls) == 3


def test_a_stored_text_that_no_longer_validates_falls_back(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    # e.g. the code changed a template's placeholders after the user saved it
    _set_overrides(monkeypatch, {"whatsapp_triage": (2, "old text with {conversation}")})
    with caplog.at_level(logging.WARNING, logger="brain.services.prompts"):
        resolved = prompts.resolve("whatsapp_triage")
    assert resolved.version is None
    assert resolved.text == prompts.get_spec("whatsapp_triage").default
    assert "invalid now" in caplog.text


def test_the_real_db_lookup_failure_never_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """A real (not mocked) failure path: a bad DATABASE_URL -> defaults."""
    from brain.db import session as db_session

    class _Boom:
        def __call__(self, *args: Any, **kwargs: Any) -> Any:
            raise ConnectionError("no route to the database")

    monkeypatch.setattr(db_session, "SessionLocal", _Boom())
    prompts.invalidate_cache()
    assert build_prompt("q", [], now=datetime(2026, 11, 8, tzinfo=UTC)).endswith(CONFLICT_RULES)


# --- memory-file block + provenance tags --------------------------------------------


def test_memory_file_block_shape() -> None:
    assert memory_file_block("") == ""
    assert memory_file_block("  \n ") == ""
    assert memory_file_block(" hello \n") == (
        "User memory file (standing instructions and context from the user):\n"
        "<<<BEGIN_MEMORY_FILE>>>\nhello\n<<<END_MEMORY_FILE>>>"
    )


def test_version_tag() -> None:
    default = ResolvedPrompt("extraction", "t")
    user3 = ResolvedPrompt("extraction", "t", 3)
    empty_memory = ResolvedPrompt(MEMORY_FILE_KEY, "")
    memory2 = ResolvedPrompt(MEMORY_FILE_KEY, "x", 2)
    cleared_memory = ResolvedPrompt(MEMORY_FILE_KEY, "  ", 4)
    assert version_tag("v8", default, empty_memory) == "v8"
    assert version_tag("v8", user3, empty_memory) == "v8+u3"
    assert version_tag("v8", default, memory2) == "v8+m2"
    assert version_tag("v8", user3, memory2) == "v8+u3+m2"
    assert version_tag("v8", default, cleared_memory) == "v8"


# --- every prompt: default render, override render, memory-file block ---------------


class _FakeLLM:
    def __init__(self, reply: str) -> None:
        self.reply = reply
        self.prompts: list[str] = []

    def complete(self, prompt: str) -> Any:
        self.prompts.append(prompt)
        reply = self.reply

        class _Completion:
            text = reply
            model = "fake"

        return _Completion()


def _ask_prompt() -> str:
    return build_prompt(
        "When is the trip?",
        [{"id": "item-1", "text": "Trip on Tuesday", "when": "Tue 10 Nov"}],
        now=datetime(2026, 11, 8, 9, 0, tzinfo=UTC),
        tz=ZoneInfo("Asia/Jerusalem"),
    )


def _extraction_run() -> tuple[str, Any]:
    from brain.services.extraction.llm_extractor import LLMExtractor

    llm = _FakeLLM('{"candidates": []}')
    extractor = LLMExtractor(llm)
    source = MemoryItem(
        raw_text="טיול ביום שלישי",
        source="email",
        occurred_at=datetime(2026, 11, 8, 7, 0, tzinfo=UTC),
    )
    assert extractor.extract(source, memory_context="") == []
    return llm.prompts[0], extractor


def _extraction_prompt() -> str:
    return _extraction_run()[0]


def _triage_run() -> tuple[str, Any]:
    from brain.ai.jev import JevError
    from brain.services.triage import triage_conversation

    class _DownJev:
        def choice(self, *args: Any, **kwargs: Any) -> Any:
            raise JevError("down")

    llm = _FakeLLM('{"label": "relevant", "confidence": 0.9}')
    result = triage_conversation("[WhatsApp] trip tomorrow", jev=_DownJev(), llm=llm)
    assert result is not None
    return llm.prompts[0], result


def _triage_prompt() -> str:
    return _triage_run()[0]


def _judge_pair() -> tuple[Any, Any]:
    from brain.services.extraction.reconciliation import ExistingItem, ReconCandidate

    jlm = ZoneInfo("Asia/Jerusalem")
    candidate = ReconCandidate(
        kind="event",
        title="טיול שנתי",
        start=datetime(2026, 11, 10, 16, 0, tzinfo=jlm),
        applies_to=("unknown",),
    )
    existing = ExistingItem(
        id="e1",
        type="event",
        title="טיול שנתי לפארק",
        start=datetime(2026, 11, 10, 16, 0, tzinfo=jlm),
        applies_to=("unknown",),
    )
    return candidate, existing


def _judge_prompt() -> str:
    from brain.services.extraction.judge import ReconJudge

    llm = _FakeLLM(json.dumps({"same": True, "confidence": 0.95}))
    judge = ReconJudge(jev=None, llm=llm)
    assert judge(*_judge_pair()) is True
    return llm.prompts[0]


def _attachment_system_prompt(tmp_path: Path) -> str:
    from claude_agent_sdk import AssistantMessage, TextBlock

    from brain.services.attachments import ClaudeFileReader

    seen: list[Any] = []

    def query(*, prompt: str, options: Any) -> Any:
        seen.append(options)

        async def gen() -> Any:
            yield AssistantMessage(content=[TextBlock(text="hello\nDESCRIPTION: x")], model="fake")

        return gen()

    reader = ClaudeFileReader(query_fn=query, scratch_root=tmp_path)
    asyncio.run(reader.read(b"\x89PNG fake", ".png"))
    return seen[0].system_prompt


_RENDERERS = {
    "ask": lambda tmp_path: _ask_prompt(),
    "extraction": lambda tmp_path: _extraction_prompt(),
    "recon_judge": lambda tmp_path: _judge_prompt(),
    "whatsapp_triage": lambda tmp_path: _triage_prompt(),
    "attachments": _attachment_system_prompt,
}


def test_default_renders_match_the_hard_coded_format(no_db_overrides: None, tmp_path: Path) -> None:
    """No overrides: each prompt is exactly what the pre-registry code built."""
    from brain.services.attachments import _READ_INSTRUCTION
    from brain.services.extraction.judge import _LLM_PROMPT_TEMPLATE, RECON_JUDGE_VERSION
    from brain.services.extraction.judge import _state as _judge_state
    from brain.services.triage import _PROMPT_TEMPLATE, TRIAGE_VERSION

    ask = _ask_prompt()
    assert ask.endswith(f"\n\n{CITATION_RULES}\n\n{CONFLICT_RULES}")
    assert ask.startswith("Question: When is the trip?\n")
    assert _triage_prompt() == _PROMPT_TEMPLATE.format(
        text="[WhatsApp] trip tomorrow", version=TRIAGE_VERSION
    )
    state = _judge_state(*_judge_pair())
    assert (
        _judge_prompt()
        == _LLM_PROMPT_TEMPLATE.format(
            candidate=json.dumps(state["candidate"], ensure_ascii=False),
            existing=json.dumps(state["existing"], ensure_ascii=False),
            version=RECON_JUDGE_VERSION,
        )[:4000]
    )
    assert _attachment_system_prompt(tmp_path) == _READ_INSTRUCTION
    from brain.config import get_settings
    from brain.services.extraction.llm_extractor import _PROMPT_TEMPLATE as extraction

    tz = ZoneInfo(get_settings().timezone)
    sent = datetime(2026, 11, 8, 7, 0, tzinfo=UTC).astimezone(tz)
    assert _extraction_prompt() == extraction.format(
        sent_local=sent.strftime("%a %d %b %Y %H:%M"),
        today_local=sent.strftime("%a %d %b %Y"),
        tz_name=tz.key,
        memory_context="(no confirmed memory yet)",
        email_text="טיול ביום שלישי",
    )


@pytest.mark.parametrize("name", list(_RENDERERS))
def test_memory_file_block_rides_in_every_prompt(
    name: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    render = _RENDERERS[name]
    _set_overrides(monkeypatch, {})
    plain = render(tmp_path)
    assert "User memory file" not in plain
    assert "BEGIN_MEMORY_FILE" not in plain

    _set_overrides(monkeypatch, {MEMORY_FILE_KEY: (1, MEMORY_TEXT)})
    with_memory = render(tmp_path)
    block = memory_file_block(MEMORY_TEXT)
    assert block.startswith("User memory file (standing instructions and context")
    if name == "whatsapp_triage":
        # the rendered {version} carries the memory-file suffix
        plain = plain.replace("Prompt version: triage-v3", "Prompt version: triage-v3+m1")
    assert with_memory == f"{block}\n\n{plain}"

    # an empty / whitespace memory file adds nothing at all
    _set_overrides(monkeypatch, {MEMORY_FILE_KEY: (2, "  \n")})
    if name == "whatsapp_triage":
        plain = plain.replace("triage-v3+m1", "triage-v3")
    assert render(tmp_path) == plain


def test_ask_rules_override_renders(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_overrides(
        monkeypatch,
        {
            "ask.citation_rules": (1, "CITE: always."),
            "ask.conflict_rules": (4, "CONFLICTS: quote both."),
        },
    )
    assert _ask_prompt().endswith("\n\nCITE: always.\n\nCONFLICTS: quote both.")


def test_attachment_instruction_override_renders(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _set_overrides(monkeypatch, {"attachments.read_instruction": (2, "Transcribe {verbatim}.")})
    assert _attachment_system_prompt(tmp_path) == "Transcribe {verbatim}."


def test_judge_override_renders(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_overrides(
        monkeypatch,
        {"recon_judge": (5, 'SAME? {{"same": true}}\nA={candidate}\nB={existing}\n{version}')},
    )
    prompt = _judge_prompt()
    assert prompt.startswith('SAME? {"same": true}\nA={"title": "טיול שנתי"')
    assert prompt.endswith("\nrecon-judge-v1")


def test_extraction_override_changes_the_recorded_prompt_version(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from brain.services.extraction.llm_extractor import EXTRACTION_PROMPT_VERSION, LLMExtractor

    _set_overrides(monkeypatch, {})
    _, extractor = _extraction_run()
    assert extractor.prompt_version == EXTRACTION_PROMPT_VERSION == "v8"
    assert extractor.name == "claude-sonnet-5-5/extraction-prompt-v8"

    custom = (
        'Extract children as JSON {{"candidates": []}}.\n'
        "Sent {sent_local} ({tz_name}); today {today_local}.\n"
        "Memory: {memory_context}\nTEXT: {email_text}"
    )
    _set_overrides(monkeypatch, {"extraction": (3, custom)})
    prompt, extractor = _extraction_run()
    assert prompt.startswith('Extract children as JSON {"candidates": []}.\nSent ')
    assert prompt.endswith("TEXT: טיול ביום שלישי")
    assert extractor.prompt_version == "v8+u3"
    assert extractor.name == "claude-sonnet-5-5/extraction-prompt-v8+u3"

    _set_overrides(monkeypatch, {"extraction": (3, custom), MEMORY_FILE_KEY: (7, "note")})
    _, extractor = _extraction_run()
    assert extractor.prompt_version == "v8+u3+m7"

    # back on the default: the class-level provenance is untouched
    assert LLMExtractor.prompt_version == "v8"


def test_triage_override_changes_the_recorded_version(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_overrides(monkeypatch, {})
    _, result = _triage_run()
    assert result.version == "triage-v3"

    _set_overrides(monkeypatch, {"whatsapp_triage": (2, _triage_text("Strict triage."))})
    prompt, result = _triage_run()
    assert result.version == "triage-v3+u2"
    assert prompt == (
        "Strict triage.\nConversation:\n[WhatsApp] trip tomorrow\nPrompt version: triage-v3+u2"
    )


# --- store (DB) ------------------------------------------------------------------------


def _store() -> Any:
    from brain.db.session import SessionLocal

    return prompts.PromptStore(SessionLocal())


def test_store_save_history_reset_restore(prompt_db: None) -> None:
    store = _store()
    assert store.save("ask.citation_rules", "Cite A.").version == 1
    assert store.save("ask.citation_rules", "Cite B.").version == 2
    assert store.save("ask.citation_rules", "Cite B.") is None  # unchanged: no new row
    assert prompts.resolve("ask.citation_rules") == ResolvedPrompt(
        "ask.citation_rules", "Cite B.", 2
    )

    reset = store.reset("ask.citation_rules")
    assert reset is not None and reset.version == 3 and reset.text is None
    assert store.reset("ask.citation_rules") is None  # already on the default
    assert prompts.resolve("ask.citation_rules") == ResolvedPrompt(
        "ask.citation_rules", CITATION_RULES
    )

    restored = store.restore("ask.citation_rules", 1)
    assert restored is not None and restored.version == 4 and restored.source == "restore"
    assert prompts.resolve("ask.citation_rules").text == "Cite A."

    history = store.history("ask.citation_rules")
    assert [(row.version, row.source, row.text) for row in history] == [
        (4, "restore", "Cite A."),
        (3, "reset", None),
        (2, "edit", "Cite B."),
        (1, "edit", "Cite A."),
    ]
    with pytest.raises(LookupError):
        store.restore("ask.citation_rules", 99)


def test_store_rejects_invalid_text_without_writing(prompt_db: None) -> None:
    store = _store()
    with pytest.raises(PromptValidationError):
        store.save("whatsapp_triage", "no placeholders at all")
    assert store.history("whatsapp_triage") == []


def test_saving_the_default_text_is_a_reset(prompt_db: None) -> None:
    store = _store()
    store.save("ask.citation_rules", "Cite A.")
    row = store.save("ask.citation_rules", CITATION_RULES)
    assert row is not None and row.text is None
    assert not prompts.resolve("ask.citation_rules").overridden


def test_db_override_reaches_the_rendered_prompt(prompt_db: None) -> None:
    _store().save(MEMORY_FILE_KEY, MEMORY_TEXT)
    assert _ask_prompt().startswith(memory_file_block(MEMORY_TEXT) + "\n\nQuestion:")


# --- API --------------------------------------------------------------------------------


def test_api_list(client: TestClient, prompt_db: None) -> None:
    body = client.get("/prompts").json()
    rows = {row["key"]: row for row in body["prompts"]}
    assert list(rows) == list(_DEFAULT_SHA256)
    assert rows["extraction"]["is_overridden"] is False
    assert rows["extraction"]["version"] is None
    assert rows["extraction"]["updated_at"] is None
    assert rows[MEMORY_FILE_KEY]["kind"] == "memory_file"
    assert rows["extraction"]["kind"] == "prompt"
    assert rows["extraction"]["description"]


def test_api_get_one(client: TestClient, prompt_db: None) -> None:
    body = client.get("/prompts/whatsapp_triage").json()
    from brain.services.triage import _PROMPT_TEMPLATE

    assert body["text"] == body["default_text"] == _PROMPT_TEMPLATE
    assert body["placeholders"] == ["text", "version"]
    assert body["history"] == []
    assert client.get("/prompts/nope").status_code == 404


def test_api_put_reset_restore_round_trip(client: TestClient, prompt_db: None) -> None:
    text = _triage_text("Hebrew too: שיחה רלוונטית.")
    response = client.put("/prompts/whatsapp_triage", json={"text": text})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["text"] == text and body["is_overridden"] is True and body["version"] == 1

    listed = {row["key"]: row for row in client.get("/prompts").json()["prompts"]}
    assert listed["whatsapp_triage"]["is_overridden"] is True
    assert listed["whatsapp_triage"]["updated_at"] is not None

    body = client.post("/prompts/whatsapp_triage/reset").json()
    assert body["is_overridden"] is False and body["version"] is None
    assert body["text"] == body["default_text"]
    assert [(h["version"], h["source"], h["is_default"]) for h in body["history"]] == [
        (2, "reset", True),
        (1, "edit", False),
    ]

    body = client.post("/prompts/whatsapp_triage/restore/1").json()
    assert body["text"] == text and body["version"] == 3
    assert body["history"][0]["source"] == "restore"
    assert client.post("/prompts/whatsapp_triage/restore/42").status_code == 404
    assert client.post("/prompts/nope/reset").status_code == 404


def test_api_put_rejects_a_broken_template_with_a_readable_detail(
    client: TestClient, prompt_db: None
) -> None:
    response = client.put("/prompts/extraction", json={"text": "Extract {email_text} please"})
    assert response.status_code == 422
    detail = response.json()["detail"]
    assert "missing placeholder(s)" in detail and "{sent_local}" in detail
    response = client.put("/prompts/extraction", json={"text": 'JSON {"a": 1}'})
    assert response.status_code == 422
    assert client.get("/prompts/extraction").json()["history"] == []


def test_api_memory_file(client: TestClient, prompt_db: None) -> None:
    body = client.get("/prompts/memory-file").json()
    assert body["key"] == MEMORY_FILE_KEY and body["text"] == "" and body["max_chars"] == 8000

    body = client.put("/prompts/memory-file", json={"text": MEMORY_TEXT}).json()
    assert body["text"] == MEMORY_TEXT and body["version"] == 1
    assert client.get("/prompts/memory_file").json()["text"] == MEMORY_TEXT

    too_long = client.put("/prompts/memory-file", json={"text": "x" * 8001})
    assert too_long.status_code == 422 and "limit" in too_long.json()["detail"]

    body = client.put("/prompts/memory-file", json={"text": ""}).json()
    assert body["text"] == "" and body["is_overridden"] is False
    assert [h["version"] for h in body["history"]] == [2, 1]


def test_cockpit_serves_the_prompts_tab(client: TestClient) -> None:
    html = client.get("/cockpit/").text
    assert 'data-tab="prompts"' in html and 'id="tab-prompts"' in html
    assert '<script src="/cockpit/prompts.js"></script>' in html
    js = client.get("/cockpit/prompts.js")
    assert js.status_code == 200
    assert "window.PromptsTab" in js.text
    assert 'dir: "auto"' in js.text  # RTL/Hebrew renders correctly in the editor
