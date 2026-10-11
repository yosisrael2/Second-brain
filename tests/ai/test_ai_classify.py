"""classify() behavior: happy path, project picking, and Jev-failure fallback."""

import pytest

from brain.ai.classify import FALLBACK_CLASSIFICATION, IMPORTANCE_MAX, classify, route
from brain.ai.enums import Actionability, ItemStatus, ItemType
from brain.ai.jev import JevError


class StubJev:
    """JevClientProtocol stub returning canned answers per question id."""

    def __init__(self, answers: dict, fail: bool = False) -> None:
        self._answers = answers
        self._fail = fail
        self.calls: list[tuple] = []

    def decide(self, state, questions):
        self.calls.append((state, dict(questions)))
        if self._fail:
            raise JevError("jev is down")
        return {qid: self._answers[qid] for qid in questions}

    def score_batch(self, question, levels, states):  # pragma: no cover - unused here
        raise AssertionError("classify must not use score_batch")


HAPPY_ANSWERS = {
    "type": {"option": "task", "confidence": 0.93, "probabilities": {"task": 0.93, "note": 0.07}},
    "importance": {"score": 4.2, "confidence": 0.8},
    "actionability": {"score": 3.9, "confidence": 0.75},
}


def test_classify_happy_path_returns_all_fields():
    result = classify("Renew the TLS certificate before Friday", StubJev(HAPPY_ANSWERS))

    assert set(result) == {"type", "project", "importance", "actionability", "confidence"}
    assert result["type"] is ItemType.TASK
    assert result["importance"] == 4
    assert result["actionability"] is Actionability.NOW
    assert result["confidence"] == pytest.approx(0.93)
    assert result["project"] is None


def test_classify_sends_all_questions_in_one_call():
    jev = StubJev(HAPPY_ANSWERS)
    classify("some text", jev)

    assert len(jev.calls) == 1
    state, questions = jev.calls[0]
    assert state == "some text"
    assert set(questions) == {"type", "importance", "actionability"}


def test_classify_importance_is_rounded_and_clamped():
    answers = {**HAPPY_ANSWERS, "importance": {"score": 9.9, "confidence": 1.0}}
    assert classify("text", StubJev(answers))["importance"] == IMPORTANCE_MAX

    answers = {**HAPPY_ANSWERS, "importance": {"score": 0.2, "confidence": 1.0}}
    assert classify("text", StubJev(answers))["importance"] == 1


def test_classify_actionability_maps_onto_three_level_scale():
    answers = {**HAPPY_ANSWERS, "actionability": {"score": 1.2, "confidence": 0.9}}
    assert classify("text", StubJev(answers))["actionability"] is Actionability.NONE

    answers = {**HAPPY_ANSWERS, "actionability": {"score": 2.4, "confidence": 0.9}}
    assert classify("text", StubJev(answers))["actionability"] is Actionability.LATER

    answers = {**HAPPY_ANSWERS, "actionability": {"score": 9.9, "confidence": 0.9}}
    assert classify("text", StubJev(answers))["actionability"] is Actionability.NOW


def test_classify_unknown_type_maps_to_none():
    answers = {**HAPPY_ANSWERS, "type": {"option": "poem", "confidence": 0.99}}
    result = classify("text", StubJev(answers))
    assert result["type"] is None


def test_classify_picks_project_from_candidates():
    answers = {**HAPPY_ANSWERS, "project": {"option": "home", "confidence": 0.95}}
    result = classify("text", StubJev(answers), projects=["work", "home"])
    assert result["project"] == "home"


def test_classify_unknown_project_maps_to_none():
    answers = {**HAPPY_ANSWERS, "project": {"option": "none", "confidence": 0.9}}
    result = classify("text", StubJev(answers), projects=["work"])
    assert result["project"] is None


@pytest.mark.parametrize("failure", [JevError("down"), JevError("timeout")])
def test_classify_jev_failure_falls_back_to_inbox(failure):
    result = classify("text", StubJev({}, fail=True))

    assert result == FALLBACK_CLASSIFICATION
    assert result["confidence"] == 0.0
    assert route(result["confidence"]) is ItemStatus.INBOX


def test_classify_never_raises_on_malformed_answers():
    malformed = {"type": "garbage", "importance": None, "actionability": 42}
    assert classify("text", StubJev(malformed)) == FALLBACK_CLASSIFICATION


def test_classify_uses_real_jevclient_by_default(monkeypatch):
    monkeypatch.delenv("JEV_API_KEY", raising=False)

    # No key configured: the default client must fail closed via the fallback,
    # not raise.
    result = classify("text")

    assert result == FALLBACK_CLASSIFICATION
    assert route(result["confidence"]) is ItemStatus.INBOX


def test_item_type_enum_covers_the_contract():
    assert {member.value for member in ItemType} == {
        "task", "decision", "note", "event", "reference", "idea",
    }
