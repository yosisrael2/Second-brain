"""Gmail SB/ labeler v1 (plan approved 21:03Z): rules only, Jev out, zero
external calls, dry-run default, label-only, undo + ops audit, cap 20.

Fakes only: the fake google service (reused from tests.test_connectors_gmail)
records every call, so "dry-run makes zero write calls" is evidence, not
assertion-by-construction. The kid names come from the caller (the CLI wires
confirmed_kid_names); the classify/plan tests are pure."""

from __future__ import annotations

from typing import Any

import pytest
from tests.fakes import StubClassifier  # noqa: F401 (keeps the fakes import warm)
from tests.test_connectors_gmail import (
    LABEL,
    _FakeHttpError,
    _msg_fixture,
    _ServiceState,
)


def _client(state: _ServiceState) -> Any:
    from brain.ai.gmail import RestGmailClient

    return RestGmailClient(
        token_loader=lambda: "refresh-token",
        service_factory=lambda token, user_id: _FakeServiceShim(state),
        min_interval_s=0.0,
        max_retries=0,
    )


class _FakeServiceShim:
    """Adapts the test_connectors_gmail fake to RestGmailClient's factory
    signature (token, user_id)."""

    def __init__(self, state: _ServiceState) -> None:
        self._state = state

    def users(self) -> Any:
        from tests.test_connectors_gmail import _FakeUsers

        return _FakeUsers(self._state)


def _msg_with_headers(
    mid: str,
    *,
    sender: str,
    subject: str,
    label_ids: list[str] | None = None,
    list_unsubscribe: bool = False,
) -> dict:
    raw = _msg_fixture(mid)
    raw["labelIds"] = label_ids if label_ids is not None else [LABEL]
    headers = raw["payload"]["headers"]
    headers[:] = [h for h in headers if h["name"] not in ("From", "Subject", "List-Unsubscribe")]
    headers.append({"name": "From", "value": sender})
    headers.append({"name": "Subject", "value": subject})
    if list_unsubscribe:
        headers.append({"name": "List-Unsubscribe", "value": "<mailto:leave@lists.example>"})
    return raw


RULES_PATH = "rules.json"


def _rules_file(tmp_path: Any, body: dict) -> str:
    import json

    path = tmp_path / RULES_PATH
    path.write_text(json.dumps(body))
    return str(path)


class TestClassify:
    def test_kid_subject_full_token_matches(self):
        from brain.services.gmail_labeler import classify

        assert (
            classify(sender="a@x", subject="רונה", list_header=None, kid_names=["רונה"], rules=None)
            == "SB/Kids/רונה"
        )
        # The Hebrew prefix run still full-matches (is_name_token).
        assert (
            classify(
                sender="a@x", subject="לרונה", list_header=None,
                kid_names=["רונה"], rules=None,
            )
            == "SB/Kids/רונה"
        )

    def test_classmate_full_name_and_substring_get_no_kid_label(self):
        from brain.services.gmail_labeler import classify

        # A classmate's FULL NAME in the subject: no standalone kid token.
        assert (
            classify(
                sender="a@x", subject="רונה לוי עדכנה", list_header=None,
                kid_names=["רונה"], rules=None,
            )
            == "SB/Unsorted"
        )
        # A substring ('Ronald' contains 'Rona') is NOT a full-token match.
        assert (
            classify(
                sender="a@x", subject="Ronald", list_header=None,
                kid_names=["Rona"], rules=None,
            )
            == "SB/Unsorted"
        )

    def test_list_unsubscribe_goes_to_noise_unless_kid_wins(self):
        from brain.services.gmail_labeler import RuleSet, classify

        rules = RuleSet(list_ids={"Noise": ("lists.example",)})
        assert (
            classify(
                sender="news@lists.example", subject="newsletter 42",
                list_header="<lists.example>", kid_names=[], rules=rules,
            )
            == "SB/Noise"
        )
        # Kids outrank Noise (precedence: Kids > ... > Noise).
        assert (
            classify(
                sender="news@lists.example", subject="רונה",
                list_header="<lists.example>", kid_names=["רונה"], rules=rules,
            )
            == "SB/Kids/רונה"
        )

    def test_domain_rule_routes_sender(self):
        from brain.services.gmail_labeler import RuleSet, classify

        rules = RuleSet(domains={"School": ("school.example",)})
        assert (
            classify(
                sender="office@school.example", subject="trip form",
                list_header=None, kid_names=[], rules=rules,
            )
            == "SB/School"
        )


class TestPlanAndApply:
    def test_dry_run_makes_zero_write_calls(self, tmp_path):
        state = _ServiceState()
        state.profile_history_id = "9002"
        state.messages["m1"] = _msg_with_headers(
            "m1", sender="office@school.example", subject="trip", list_unsubscribe=False
        )
        state.list_result = {"messages": [{"id": "m1"}]}
        state.labels: dict[str, str] = {}
        rules = _rules_file(tmp_path, {"School": {"domains": ["school.example"], "list_ids": []}})
        from brain.services.gmail_labeler import plan_labels

        client = _client(state)
        from brain.services.gmail_labeler import load_rules

        plans = plan_labels(
            client, rules=load_rules(rules), kid_names=[], label_id=LABEL, query=""
        )
        assert [(p.gmail_id, p.label_path) for p in plans] == [("m1", "SB/School")]
        writes = [c for c in state.calls if c[0] in ("messages.modify", "labels.create")]
        assert writes == [], "dry-run must make ZERO write calls"

    def test_apply_caps_at_max_apply(self, tmp_path, database):
        state = _ServiceState()
        state.profile_history_id = "9002"
        for i in range(30):
            mid = f"m{i:02d}"
            state.messages[mid] = _msg_with_headers(
                mid, sender="office@school.example", subject=f"trip {i}"
            )
        state.list_result = {"messages": [{"id": f"m{i:02d}"} for i in range(30)]}
        rules = _rules_file(tmp_path, {"School": {"domains": ["school.example"], "list_ids": []}})
        from brain.services.gmail_labeler import apply_labels, load_rules, plan_labels

        client = _client(state)
        plans = plan_labels(client, rules=load_rules(rules), kid_names=[], label_id=LABEL, query="")
        assert len(plans) == 30
        applied = apply_labels(client, plans, max_apply=20, batch_id="b1")
        assert applied == 20, "the cap is hard: nothing past 20 in one run"
        modifies = [c for c in state.calls if c[0] == "messages.modify"]
        assert len(modifies) == 20

    def test_undo_removes_only_its_batch_labels(self, tmp_path, database):
        state = _ServiceState()
        state.profile_history_id = "9002"
        # m2 carries a PRE-EXISTING user label (added outside any batch).
        state.messages["m1"] = _msg_with_headers(
            "m1", sender="office@school.example", subject="a", label_ids=[LABEL, "USER_KEEP"]
        )
        state.messages["m2"] = _msg_with_headers(
            "m2", sender="office@school.example", subject="b", label_ids=[LABEL, "USER_KEEP"]
        )
        state.list_result = {"messages": [{"id": "m1"}, {"id": "m2"}]}
        rules = _rules_file(tmp_path, {"School": {"domains": ["school.example"], "list_ids": []}})
        from brain.services.gmail_labeler import apply_labels, load_rules, plan_labels, undo_batch

        client = _client(state)
        plans = plan_labels(client, rules=load_rules(rules), kid_names=[], label_id=LABEL, query="")
        apply_labels(client, plans, max_apply=20, batch_id="B1")
        undone = undo_batch(client, "B1")
        assert undone == 2
        # The batch's SB label is gone; the USER's label is untouched.
        assert "USER_KEEP" in state.messages["m1"]["labelIds"]
        assert "USER_KEEP" in state.messages["m2"]["labelIds"]
        assert not [lid for lid in state.messages["m1"]["labelIds"] if lid.startswith("SB_")]

    def test_403_fails_before_any_message_write(self, tmp_path, database):
        state = _ServiceState()
        state.profile_history_id = "9002"
        state.messages["m1"] = _msg_with_headers(
            "m1", sender="office@school.example", subject="trip"
        )
        state.list_result = {"messages": [{"id": "m1"}]}
        state.create_error = _FakeHttpError(403, b"insufficientPermissions")
        rules = _rules_file(tmp_path, {"School": {"domains": ["school.example"], "list_ids": []}})
        from brain.services.gmail_labeler import apply_labels, load_rules, plan_labels

        client = _client(state)
        plans = plan_labels(client, rules=load_rules(rules), kid_names=[], label_id=LABEL, query="")
        with pytest.raises(Exception) as excinfo:
            apply_labels(client, plans, max_apply=20, batch_id="B1")
        assert "gmail.modify" in str(excinfo.value)
        modifies = [c for c in state.calls if c[0] == "messages.modify"]
        assert modifies == [], "a 403 aborts before ANY message write"

    def test_already_labeled_messages_are_skipped(self, tmp_path):
        state = _ServiceState()
        state.profile_history_id = "9002"
        state.labels = {}
        state.messages["m1"] = _msg_with_headers(
            "m1", sender="office@school.example", subject="a", label_ids=[LABEL, "SB_0001"]
        )
        state.list_labels_result = {
            "labels": [{"id": "SB_0001", "name": "SB/School", "type": "user"}]
        }
        rules = _rules_file(tmp_path, {"School": {"domains": ["school.example"], "list_ids": []}})
        from brain.services.gmail_labeler import apply_labels, load_rules, plan_labels

        client = _client(state)
        plans = plan_labels(client, rules=load_rules(rules), kid_names=[], label_id=LABEL, query="")
        assert plans == [], "a message already carrying an SB/ label is skipped"
        assert apply_labels(client, plans, max_apply=20, batch_id="B2") == 0
        assert not [c for c in state.calls if c[0] == "messages.modify"]


class TestCli:
    def test_cli_dry_run_is_the_default_and_writes_nothing(
        self, tmp_path, monkeypatch, capsys, database
    ):
        """`brain gmail-label` (no flags) is a DRY RUN: zero write calls on
        the recorded fake, and the plan is printed."""
        import json as _json

        from brain import cli

        state = _ServiceState()
        state.profile_history_id = "9002"
        state.messages["m1"] = _msg_with_headers(
            "m1", sender="office@school.example", subject="trip"
        )
        state.list_result = {"messages": [{"id": "m1"}]}
        rules = tmp_path / "rules.json"
        rules.write_text(_json.dumps({"School": {"domains": ["school.example"], "list_ids": []}}))

        monkeypatch.setattr(
            cli, "_labeler_client",
            lambda settings: _client(state),
        )
        monkeypatch.setattr(
            "brain.config.get_settings",
            lambda: _FakeSettings(rules_path=str(rules)),
        )

        code = cli.main(["gmail-label"])
        assert code == 0
        out = capsys.readouterr().out
        assert "m1 -> SB/School" in out
        assert "dry-run" in out
        writes = [c for c in state.calls if c[0] in ("messages.modify", "labels.create")]
        assert writes == [], "the CLI default must not write"


class _FakeSettings:
    """Just the fields _gmail_label reads."""

    def __init__(self, rules_path: str) -> None:
        self.gmail_labeler_rules_path = rules_path
        self.gmail_label_id = LABEL
        self.gmail_query = ""
        self.gmail_min_call_interval_s = 0.0
        self.database_url = "postgresql+psycopg://brain:brain@localhost:27432/brain_w3_labeler_test"
        self.qdrant_collection = "brain_items_w3_labeler_test"
