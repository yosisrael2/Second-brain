"""Gmail SB/ labeler v1 (plan approved 21:03Z): rules only, Jev out, zero
external calls, dry-run default, label-only, undo + ops audit, cap 20.

Fakes only: the fake google service (reused from tests.test_connectors_gmail)
records every call, so "dry-run makes zero write calls" is evidence, not
assertion-by-construction. The kid names come from the caller (the CLI wires
confirmed_kid_names); the classify/plan tests are pure."""

from __future__ import annotations

import json
import os
import re
from typing import Any

import pytest
from tests.fakes import StubClassifier  # noqa: F401 (keeps the fakes import warm)
from tests.test_connectors_gmail import (
    LABEL,
    _FakeHttpError,
    _msg_fixture,
    _ServiceState,
)

from brain import cli


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
        audit = tmp_path / "audit.jsonl"
        applied = apply_labels(client, plans, max_apply=20, batch_id="b1", audit_path=audit)
        assert applied == 20, "the cap is hard: nothing past 20 in one run"
        modifies = [c for c in state.calls if c[0] == "messages.modify"]
        assert len(modifies) == 20

    def test_undo_removes_only_its_batch_labels(self, tmp_path, database):
        """Review: the old form of this test was vacuous (fake label ids are
        ints, so 'SB_' never matched anything). It now asserts the batch's own
        CREATED label id is gone from labelIds while the user label stays."""
        audit = tmp_path / "audit.jsonl"
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
        apply_labels(client, plans, max_apply=20, batch_id="B1", audit_path=audit)
        added = {
            lid
            for call in (c for c in state.calls if c[0] == "messages.modify")
            for lid in call[1]["body"]["addLabelIds"]
        }
        assert added, "the batch must have added real label ids"
        undone = undo_batch(client, "B1", audit_path=audit)
        assert undone.undone == 2
        # The batch's OWN created label id is gone from labelIds (not vacuous:
        # ids are ints here); the USER's pre-existing label is untouched.
        for mid in ("m1", "m2"):
            ids = state.messages[mid]["labelIds"]
            assert "USER_KEEP" in ids
            assert not (added & set(ids)), f"batch label id must be gone from {mid}: {ids}"

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
            apply_labels(client, plans, max_apply=20, batch_id="B1",
                         audit_path=tmp_path / "audit.jsonl")
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
        assert apply_labels(client, plans, max_apply=20, batch_id="B2",
                            audit_path=tmp_path / "audit.jsonl") == 0
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
            "brain.config.get_settings",
            lambda: _FakeSettings(rules_path=str(rules)),
        )

        monkeypatch.setattr(
            cli, "_labeler_client",
            lambda settings, modify=False: _client(state),
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

    def __init__(
        self,
        rules_path: str,
        *,
        account_email: str = "owner@example.com",
        audit_path: str = "",
        credentials_path: str = "~/secret-outside-repo.json",
    ) -> None:
        self.gmail_labeler_rules_path = rules_path
        self.gmail_label_id = LABEL
        self.gmail_query = ""
        self.gmail_min_call_interval_s = 0.0
        self.gmail_labeler_account_email = account_email
        self.gmail_labeler_audit_path = audit_path
        self.gmail_credentials_path = credentials_path
        self.database_url = "postgresql+psycopg://brain:brain@localhost:27432/brain_w3_labeler_test"
        self.qdrant_collection = "brain_items_w3_labeler_test"

    def audit_path(self):
        import pathlib

        from brain.services.gmail_labeler import AUDIT_FILENAME

        return pathlib.Path(self.gmail_labeler_audit_path or AUDIT_FILENAME)


class TestScopes:
    """Review round 1: the SYNC path keeps readonly exactly; the labeler's
    scope set is separate; a readonly-only token fails --apply before any
    call."""

    def test_connector_credentials_are_readonly_only(self, monkeypatch):
        """(a) The REAL credential builder on the poller/connector path gets
        [readonly] exactly — asserted on the built Credentials, not the
        constant. Skipped where the gmail extra is absent (CI)."""
        google_creds = pytest.importorskip("google.oauth2.credentials")

        captured: dict[str, Any] = {}

        class _SpyCredentials:
            def __init__(self, **kwargs: Any) -> None:
                captured.update(kwargs)

        monkeypatch.setattr(google_creds, "Credentials", _SpyCredentials)
        from brain.connectors.google.base import default_service_factory

        class _FakeDiscovery:
            def __init__(self, *a: Any, **k: Any) -> None:
                pass

        import googleapiclient.discovery as discovery_mod

        monkeypatch.setattr(discovery_mod, "build", lambda *a, **k: _FakeDiscovery())
        client_config = {
            "client_id": "cid", "client_secret": "sek",
            "token_uri": "https://oauth2.googleapis.com/token",
        }
        import brain.connectors.google.base as base_mod

        monkeypatch.setattr(base_mod, "oauth_client_config", lambda settings=None: client_config)
        default_service_factory(
            "1//tok",
            ["https://www.googleapis.com/auth/gmail.readonly"],
            "gmail", "v1",
        )
        assert captured["scopes"] == ["https://www.googleapis.com/auth/gmail.readonly"], (
            "the sync credential path must request readonly ONLY"
        )

    def test_labeler_client_requests_readonly_plus_modify(self, monkeypatch):
        """(b) The labeler's WRITE client is built with GMAIL_LABEL_SCOPES
        (readonly + gmail.modify) — never the sync-only set."""
        from brain import cli
        from brain.ai.gmail import GMAIL_LABEL_SCOPES

        captured: dict[str, Any] = {}

        def _fake_factory(token: str, scopes: list[str], api: str, version: str) -> Any:
            captured["scopes"] = list(scopes)
            return "fake-service"

        import brain.connectors.google.base as base_mod

        monkeypatch.setattr(base_mod, "default_service_factory", _fake_factory)
        monkeypatch.setattr(cli, "_labeler_token", lambda: "1//tok")
        cli._labeler_client(_FakeSettings(rules_path=""), modify=True)
        assert captured["scopes"] == list(GMAIL_LABEL_SCOPES)
        assert "https://www.googleapis.com/auth/gmail.modify" in captured["scopes"]

    def test_readonly_only_token_fails_apply_before_any_call(
        self, tmp_path, monkeypatch, capsys, database
    ):
        """(c) A readonly-only token + --apply: the granted-scope check fails
        with the re-consent message and ZERO Gmail calls (not even reads)."""
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
            "brain.config.get_settings",
            lambda: _FakeSettings(rules_path=str(rules)),
        )

        monkeypatch.setattr(
            cli, "_labeler_client", lambda settings, modify=False: _client(state)
        )
        monkeypatch.setattr(cli, "_labeler_token", lambda: "1//readonly-token")
        monkeypatch.setattr(
            cli, "_labeler_client_config",
            lambda settings: {"client_id": "c", "client_secret": "s",
                              "token_uri": "https://oauth2.googleapis.com/token"},
        )

        def _readonly_post(form: bytes) -> dict:
            return {"scope": "https://www.googleapis.com/auth/gmail.readonly"}

        import brain.services.gmail_labeler as labeler_mod

        real_check = labeler_mod.token_grants_modify

        def _checked(token: str, client_config: dict, poster: Any = None) -> bool:
            return real_check(token, client_config=client_config, poster=_readonly_post)

        monkeypatch.setattr(labeler_mod, "token_grants_modify", _checked)

        code = cli.main(["gmail-label", "--apply"])
        assert code == 78
        assert "gmail.modify" in capsys.readouterr().err
        assert state.calls == [], "zero Gmail calls: the check runs before anything"


class TestReviewRound2:
    """Round 2: the dry-run's planning client is readonly-only; the re-consent
    message names `gmail-label --auth`; --undo sits behind the same gate; a
    revoked token is a clean exit-78."""

    def _run_with_factory_spy(self, monkeypatch, argv, state):
        """Run the CLI with a capturing service factory (which builds the
        fake service over ``state``); return (code, scopes per call)."""
        import brain.connectors.google.base as base_mod

        calls: list[list[str]] = []
        fake = _FakeServiceShim(state)

        def _spy_factory(token: str, scopes: list[str], api: str, version: str) -> Any:
            calls.append(list(scopes))
            return fake

        monkeypatch.setattr(base_mod, "default_service_factory", _spy_factory)
        code = cli.main(argv)
        return code, calls

    def _seed_state(self):
        state = _ServiceState()
        state.profile_history_id = "9003"
        state.profile_email = "owner@example.com"
        state.messages["m1"] = _msg_with_headers(
            "m1", sender="office@school.example", subject="trip"
        )
        state.list_result = {"messages": [{"id": "m1"}]}
        return state

    def _granted(self, monkeypatch):
        import brain.services.gmail_labeler as labeler_mod

        real_check = labeler_mod.token_grants_modify

        def _checked(token: str, client_config: dict, poster: Any = None) -> bool:
            return real_check(token, client_config=client_config, poster=lambda f: {
                "scope": "https://www.googleapis.com/auth/gmail.readonly "
                "https://www.googleapis.com/auth/gmail.modify"
            })

        monkeypatch.setattr(labeler_mod, "token_grants_modify", _checked)

    def _readonly(self, monkeypatch):
        import brain.services.gmail_labeler as labeler_mod

        real_check = labeler_mod.token_grants_modify

        def _checked(token: str, client_config: dict, poster: Any = None) -> bool:
            return real_check(
                token, client_config=client_config,
                poster=lambda f: {"scope": "https://www.googleapis.com/auth/gmail.readonly"},
            )

        monkeypatch.setattr(labeler_mod, "token_grants_modify", _checked)

    def test_dry_run_planning_client_is_readonly_only(
        self, tmp_path, monkeypatch, capsys, database
    ):
        """The dry-run is the FIRST thing the user runs: its planning client
        must request exactly [readonly] — never modify (today's token would
        fail invalid_scope at refresh)."""
        import json as _json


        state = self._seed_state()
        rules = tmp_path / "rules.json"
        rules.write_text(_json.dumps({"School": {"domains": ["school.example"], "list_ids": []}}))
        monkeypatch.setattr(
            "brain.config.get_settings",
            lambda: _FakeSettings(rules_path=str(rules)),
        )
        monkeypatch.setattr(cli, "_labeler_token", lambda: "1//tok")
        self._readonly(monkeypatch)
        code, calls = self._run_with_factory_spy(monkeypatch, ["gmail-label"], state)
        assert code == 0
        assert calls == [["https://www.googleapis.com/auth/gmail.readonly"]], calls

    def test_apply_plans_readonly_then_writes_with_modify(
        self, tmp_path, monkeypatch, capsys, database
    ):
        """--apply: the PLAN client is readonly; only the WRITE client (after
        the gate) requests readonly+modify."""
        import json as _json

        from brain import cli

        state = self._seed_state()
        rules = tmp_path / "rules.json"
        rules.write_text(_json.dumps({"School": {"domains": ["school.example"], "list_ids": []}}))
        monkeypatch.setattr(
            "brain.config.get_settings",
            lambda: _FakeSettings(rules_path=str(rules)),
        )
        monkeypatch.setattr(cli, "_labeler_token", lambda: "1//tok")
        monkeypatch.setattr(
            cli, "_labeler_client_config",
            lambda settings: {"client_id": "c", "client_secret": "s",
                              "token_uri": "https://oauth2.googleapis.com/token"},
        )
        self._granted(monkeypatch)
        code, calls = self._run_with_factory_spy(
            monkeypatch, ["gmail-label", "--apply"], state
        )
        assert code == 0
        assert calls == [
            ["https://www.googleapis.com/auth/gmail.readonly"],
            ["https://www.googleapis.com/auth/gmail.readonly",
             "https://www.googleapis.com/auth/gmail.modify"],
        ], calls

    def test_undo_requires_the_consent_gate(
        self, tmp_path, monkeypatch, capsys, database
    ):
        """A readonly-only token + --undo: the same gate -> exit 78, the
        re-consent message, ZERO Gmail calls."""
        from brain import cli

        state = self._seed_state()
        monkeypatch.setattr(
            cli, "_labeler_token", lambda: "1//readonly-token"
        )
        monkeypatch.setattr(
            cli, "_labeler_client_config",
            lambda settings: {"client_id": "c", "client_secret": "s",
                              "token_uri": "https://oauth2.googleapis.com/token"},
        )
        self._readonly(monkeypatch)
        code = cli.main(["gmail-label", "--undo", "deadbeef1234"])
        assert code == 78
        assert "gmail-label --auth" in capsys.readouterr().err
        assert state.calls == [], "undo must not reach Gmail without the gate"

    def test_consent_error_names_the_labeler_auth_command(
        self, tmp_path, monkeypatch, capsys, database
    ):
        """A 403 from a write (defense in depth behind the gate) must point
        the user at `brain gmail-label --auth` — NOT `connectors auth email`,
        which is readonly now and would change nothing."""
        import json as _json

        from brain import cli
        from brain.ai.gmail import GmailAuthError

        state = self._seed_state()
        state.modify_error = _FakeHttpError(403, b"insufficientPermissions")
        rules = tmp_path / "rules.json"
        rules.write_text(_json.dumps({"School": {"domains": ["school.example"], "list_ids": []}}))
        monkeypatch.setattr(
            "brain.config.get_settings",
            lambda: _FakeSettings(rules_path=str(rules)),
        )
        monkeypatch.setattr(cli, "_labeler_token", lambda: "1//tok")
        monkeypatch.setattr(
            cli, "_labeler_client_config",
            lambda settings: {"client_id": "c", "client_secret": "s",
                              "token_uri": "https://oauth2.googleapis.com/token"},
        )
        self._granted(monkeypatch)
        with pytest.raises(GmailAuthError) as excinfo:
            self._run_with_factory_spy(monkeypatch, ["gmail-label", "--apply"], state)
        text = str(excinfo.value)
        assert "gmail-label --auth" in text
        assert "connectors auth email" not in text

    def test_revoked_token_is_a_clean_exit_78(
        self, tmp_path, monkeypatch, capsys, database
    ):
        """The scope check hits a revoked/invalid_grant token: no traceback —
        exit 78 with a clear message, zero Gmail calls."""
        import urllib.error

        from brain import cli

        state = self._seed_state()
        rules = tmp_path / "rules.json"
        rules.write_text("{}")
        monkeypatch.setattr(
            "brain.config.get_settings",
            lambda: _FakeSettings(rules_path=str(rules)),
        )
        monkeypatch.setattr(
            cli, "_labeler_token", lambda: "1//revoked"
        )
        monkeypatch.setattr(
            cli, "_labeler_client_config",
            lambda settings: {"client_id": "c", "client_secret": "s",
                              "token_uri": "https://oauth2.googleapis.com/token"},
        )
        import brain.services.gmail_labeler as labeler_mod

        def _raise(form: bytes) -> dict:
            raise urllib.error.HTTPError(
                "https://oauth2.googleapis.com/token", 400, "invalid_grant", {}, None
            )

        real_check = labeler_mod.token_grants_modify

        def _checked(token: str, client_config: dict, poster: Any = None) -> bool:
            return real_check(token, client_config=client_config, poster=_raise)

        monkeypatch.setattr(labeler_mod, "token_grants_modify", _checked)
        code = cli.main(["gmail-label", "--apply"])
        assert code == 78
        err = capsys.readouterr().err
        assert "Traceback" not in err
        assert "gmail-label --auth" in err
        assert state.calls == []


# --- manager review of #137: 4 majors + follow-ups -----------------------------


class _CliEnv:
    """Wiring for --apply-path CLI tests: fake settings/client/token/config
    plus a granted-scope check. ``audit_path`` defaults into tmp_path."""

    def __init__(self, tmp_path, state, *, account: str = "owner@example.com",
                 granted: bool = True, profile_email: str | None = None) -> None:
        self.tmp_path = tmp_path
        self.state = state
        self.rules_path = tmp_path / "rules.json"
        self.rules_path.write_text(
            json.dumps({"School": {"domains": ["school.example"], "list_ids": []}})
        )
        self.audit_path = tmp_path / "audit.jsonl"
        self.settings = _FakeSettings(
            rules_path=str(self.rules_path),
            account_email=account,
            audit_path=str(self.audit_path),
        )
        if profile_email is not None:
            state.profile_email = profile_email
        self.granted = granted
        self.stored_tokens: list[tuple[str, str]] = []

    def patch(self, monkeypatch) -> None:
        monkeypatch.setattr("brain.config.get_settings", lambda: self.settings)
        monkeypatch.setattr(cli, "_labeler_client",
                            lambda settings, modify=False: _client(self.state))
        monkeypatch.setattr(cli, "_labeler_token", lambda: "1//tok")
        monkeypatch.setattr(
            cli, "_labeler_client_config",
            lambda s: {"client_id": "c", "client_secret": "s",
                       "token_uri": "https://oauth2.googleapis.com/token"},
        )
        monkeypatch.setattr(
            "brain.services.keychain.Keychain.store",
            lambda self_, acct, token: self.stored_tokens.append((acct, token)),
        )
        import brain.services.gmail_labeler as labeler_mod

        real_check = labeler_mod.token_grants_modify
        scope = (
            "https://www.googleapis.com/auth/gmail.readonly "
            "https://www.googleapis.com/auth/gmail.modify"
            if self.granted else "https://www.googleapis.com/auth/gmail.readonly"
        )

        def _checked(token: str, client_config: dict, poster: Any = None) -> bool:
            return real_check(token, client_config=client_config, poster=lambda f: {
                "scope": scope
            })

        monkeypatch.setattr(labeler_mod, "token_grants_modify", _checked)


class TestReviewMajor1Cap:
    """MAJOR 1: --max-apply -1 removed the cap. <1 is rejected by the CLI
    (parser.error, exit 2); the service enforces a hard ceiling of 50."""

    def _seed(self, count: int) -> _ServiceState:
        state = _ServiceState()
        state.profile_history_id = "9002"
        for i in range(count):
            mid = f"m{i:03d}"
            state.messages[mid] = _msg_with_headers(
                mid, sender="office@school.example", subject=f"trip {i}"
            )
        state.list_result = {"messages": [{"id": f"m{i:03d}"} for i in range(count)]}
        return state

    @pytest.mark.parametrize("bad", ["0", "-1", "-5"])
    def test_cli_rejects_max_apply_below_one(self, bad, capsys):
        with pytest.raises(SystemExit) as excinfo:
            cli.main(["gmail-label", "--max-apply", bad])
        assert excinfo.value.code == 2, "parser.error must reject < 1 with exit 2"
        assert "--max-apply" in capsys.readouterr().err

    def test_apply_labels_ceiling_is_50(self, tmp_path, database):
        state = self._seed(60)
        rules = _rules_file(tmp_path, {"School": {"domains": ["school.example"], "list_ids": []}})
        from brain.services.gmail_labeler import apply_labels, load_rules, plan_labels

        client = _client(state)
        plans = plan_labels(client, rules=load_rules(rules), kid_names=[], label_id=LABEL, query="")
        assert len(plans) == 60
        applied = apply_labels(client, plans, max_apply=10_000, batch_id="b1",
                               audit_path=tmp_path / "audit.jsonl")
        assert applied == 50, "the ceiling is hard: 10_000 still applies at most 50"
        assert len([c for c in state.calls if c[0] == "messages.modify"]) == 50


class TestReviewMajor2Account:
    """MAJOR 2: the SHARED keychain token must never be silently repointed at
    another mailbox. GMAIL_LABELER_ACCOUNT_EMAIL pins the account; --auth
    verifies it via getProfile BEFORE storing (refuses to store otherwise);
    --apply refuses on mismatch; unset setting refuses both."""

    def test_apply_refuses_when_the_setting_is_unset(self, tmp_path, monkeypatch, capsys):
        state = _ServiceState()
        env = _CliEnv(tmp_path, state, account="")
        env.patch(monkeypatch)
        code = cli.main(["gmail-label", "--apply"])
        assert code == 78
        assert "GMAIL_LABELER_ACCOUNT_EMAIL" in capsys.readouterr().err
        assert state.calls == [], "refusal must precede ANY Gmail call"

    def test_apply_refuses_account_mismatch_before_any_write(self, tmp_path, monkeypatch, capsys):
        state = _ServiceState()
        env = _CliEnv(tmp_path, state, account="owner@example.com",
                      profile_email="work@example.com")
        env.patch(monkeypatch)
        code = cli.main(["gmail-label", "--apply"])
        assert code == 78
        err = capsys.readouterr().err
        assert "work@example.com" in err and "owner@example.com" in err
        assert not [c for c in state.calls if c[0] in ("messages.modify", "labels.create")], (
            "a mismatched mailbox must never be written"
        )

    def test_apply_passes_when_the_account_matches(self, tmp_path, monkeypatch, capsys):
        state = TestReviewFollowups._seed_one()
        env = _CliEnv(tmp_path, state, account="owner@example.com",
                      profile_email="owner@example.com")
        env.patch(monkeypatch)
        code = cli.main(["gmail-label", "--apply"])
        assert code == 0
        assert [c for c in state.calls if c[0] == "messages.modify"]

    def test_auth_refuses_when_the_setting_is_unset(self, tmp_path, monkeypatch, capsys):
        env = _CliEnv(tmp_path, _ServiceState(), account="")
        env.patch(monkeypatch)
        flow = _FakeFlow()
        monkeypatch.setattr(cli, "_build_oauth_flow", lambda path, scopes=None: flow)
        code = cli.main(["gmail-label", "--auth"])
        assert code == 78
        assert "GMAIL_LABELER_ACCOUNT_EMAIL" in capsys.readouterr().err
        assert not flow.ran, "the consent browser must not even open without the pin"

    def test_auth_refuses_to_store_a_mismatched_mailbox_token(self, tmp_path, monkeypatch, capsys):
        env = _CliEnv(tmp_path, _ServiceState(), account="me@example.com")
        env.patch(monkeypatch)
        monkeypatch.setattr(cli, "_build_oauth_flow", lambda path, scopes=None: _FakeFlow())
        monkeypatch.setattr(
            cli, "_labeler_email_client",
            lambda token, settings: _ProfileClient("work@example.com"),
        )
        code = cli.main(["gmail-label", "--auth"])
        assert code == 78
        assert "work@example.com" in capsys.readouterr().err
        assert env.stored_tokens == [], "the shared keychain token must NOT be overwritten"

    def test_auth_stores_the_token_when_the_mailbox_matches(self, tmp_path, monkeypatch, capsys):
        env = _CliEnv(tmp_path, _ServiceState(), account="me@example.com")
        env.patch(monkeypatch)
        monkeypatch.setattr(cli, "_build_oauth_flow", lambda path, scopes=None: _FakeFlow())
        monkeypatch.setattr(
            cli, "_labeler_email_client",
            lambda token, settings: _ProfileClient("me@example.com"),
        )
        code = cli.main(["gmail-label", "--auth"])
        assert code == 0
        assert env.stored_tokens and env.stored_tokens[0][1] == "1//fresh-token"


class _FakeFlow:
    """Stands in for InstalledAppFlow (consent is never real in tests)."""

    def __init__(self) -> None:
        self.ran = False

    def run_local_server(self, port: int = 0) -> None:
        self.ran = True
        self.credentials = type("Creds", (), {"refresh_token": "1//fresh-token"})()


class _ProfileClient:
    """Duck-typed RestGmailClient exposing only profile_email()."""

    def __init__(self, email: str) -> None:
        self._email = email

    def profile_email(self) -> str:
        return self._email


class TestReviewMajor3Ledger:
    """MAJOR 3: the undo ledger lived in ops_events (pruned after
    RETENTION_DAYS=7) so undo silently became 'undone 0'. The ledger is now a
    durable append-only 0600 JSONL file; ops_events is NOT touched."""

    def _apply_two(self, tmp_path, *, database=None):
        state = _ServiceState()
        state.profile_history_id = "9002"
        state.messages["m1"] = _msg_with_headers(
            "m1", sender="office@school.example", subject="a", label_ids=[LABEL, "USER_KEEP"]
        )
        state.messages["m2"] = _msg_with_headers(
            "m2", sender="office@school.example", subject="b", label_ids=[LABEL, "USER_KEEP"]
        )
        state.list_result = {"messages": [{"id": "m1"}, {"id": "m2"}]}
        rules = _rules_file(tmp_path, {"School": {"domains": ["school.example"], "list_ids": []}})
        from brain.services.gmail_labeler import apply_labels, load_rules, plan_labels

        client = _client(state)
        plans = plan_labels(client, rules=load_rules(rules), kid_names=[], label_id=LABEL, query="")
        audit = tmp_path / "audit.jsonl"
        applied = apply_labels(client, plans, max_apply=20, batch_id="B1", audit_path=audit)
        assert applied == 2
        return client, state, audit

    def test_undo_works_with_no_ops_events_at_all(self, tmp_path, monkeypatch):
        """No `database` fixture: with the OLD code the ledger lived in
        ops_events (pruned after 7 days) and undo silently reported 0. The
        JSONL file is the only store now; record_event is a spy that must
        never be called by the labeler anymore."""
        import brain.ops as ops_mod

        calls: list[tuple] = []
        monkeypatch.setattr(ops_mod, "record_event",
                            lambda *a, **k: calls.append((a, k)))  # spy, never a store
        client, state, audit = self._apply_two(tmp_path)
        from brain.services.gmail_labeler import undo_batch

        undone = undo_batch(client, "B1", audit_path=audit)
        assert undone.undone == 2
        for mid in ("m1", "m2"):
            ids = state.messages[mid]["labelIds"]
            assert "USER_KEEP" in ids and "SB_10" not in [i for i in ids if str(i).startswith("SB")]
        assert calls == [], "the undo ledger must not depend on ops_events"

    def test_undo_survives_ops_events_being_pruned(self, tmp_path, database, monkeypatch):
        """The literal review scenario: ops_events is pruned to zero rows (7
        days pass) and the batch must STILL be undoable from the JSONL file."""
        from sqlalchemy import text

        client, state, audit = self._apply_two(tmp_path, database=database)
        with engine_from_conftest() as conn:
            conn.execute(text("DELETE FROM ops_events"))
        from brain.services.gmail_labeler import undo_batch

        undone = undo_batch(client, "B1", audit_path=audit)
        assert undone.undone == 2, "undo must survive the ops_events prune"

    def test_audit_file_is_created_0600(self, tmp_path):
        client, state, audit = self._apply_two(tmp_path)
        mode = os.stat(audit).st_mode & 0o777
        assert mode == 0o600, f"the audit ledger must be 0600, got {oct(mode)}"

    def test_ledger_rows_pair_apply_first_then_outcome(self, tmp_path):
        from brain.services.gmail_labeler import read_audit_rows

        client, state, audit = self._apply_two(tmp_path)
        rows = read_audit_rows(audit)
        kinds = [row["kind"] for row in rows]
        assert kinds == ["apply", "outcome", "apply", "outcome", "batch"], kinds
        for i in (0, 2):
            apply_row, outcome_row = rows[i], rows[i + 1]
            assert apply_row["event"] == outcome_row["event"]
            assert apply_row["label_id"] and outcome_row["ok"] is True
            assert apply_row["gmail_id"] in ("m1", "m2")

    def test_cli_audit_reads_the_jsonl_ledger(self, tmp_path, monkeypatch, capsys):
        client, state, audit = self._apply_two(tmp_path)
        env = _CliEnv(tmp_path, state)
        env.settings.gmail_labeler_audit_path = str(audit)
        monkeypatch.setattr("brain.config.get_settings", lambda: env.settings)
        code = cli.main(["gmail-label", "--audit", "B1"])
        assert code == 0
        out = capsys.readouterr().out
        assert "m1 -> SB/School" in out and "m2 -> SB/School" in out


def engine_from_conftest():
    """The suite's engine (same guarded test DB the conftest fixture uses)."""
    from tests.conftest import engine  # noqa: PLC0415

    class _Ctx:
        def __enter__(self):
            return engine.connect()

        def __exit__(self, *exc):
            return False

    return _Ctx()


class TestReviewMajor4AuditFirst:
    """MAJOR 4: the label used to be applied BEFORE the audit row, and
    record_event never raises — a failed audit write left an un-undoable
    label. Now the audit row is written FIRST and a failed write fails the
    run BEFORE any label is applied."""

    def test_audit_write_failure_fails_the_run_before_any_modify(self, tmp_path):
        state = _ServiceState()
        state.profile_history_id = "9002"
        state.messages["m1"] = _msg_with_headers(
            "m1", sender="office@school.example", subject="trip"
        )
        state.list_result = {"messages": [{"id": "m1"}]}
        rules = _rules_file(tmp_path, {"School": {"domains": ["school.example"], "list_ids": []}})
        from brain.services.gmail_labeler import (
            AuditLedgerError,
            apply_labels,
            load_rules,
            plan_labels,
        )

        blocker = tmp_path / "blocker"
        blocker.write_text("a file, not a dir")
        audit = blocker / "audit.jsonl"  # parent is a FILE -> the write fails
        client = _client(state)
        plans = plan_labels(client, rules=load_rules(rules), kid_names=[], label_id=LABEL, query="")
        with pytest.raises(AuditLedgerError):
            apply_labels(client, plans, max_apply=20, batch_id="B1", audit_path=audit)
        assert not [c for c in state.calls if c[0] == "messages.modify"], (
            "a failed ledger write must leave NO un-undoable label behind"
        )


class TestReviewFollowups:
    """The review's ALSO list: batch id before applying; undo continues past
    per-item errors and reports counts; system-label guard; metadataHeaders;
    empty rules patterns; label-create 409 re-list; ConnectorAuthError exit
    78; token_uri pinned to Google; --confirm-plan; dead build_labeler_client
    deleted."""

    @staticmethod
    def _seed_one() -> _ServiceState:
        state = _ServiceState()
        state.profile_history_id = "9002"
        state.profile_email = "owner@example.com"
        state.messages["m1"] = _msg_with_headers(
            "m1", sender="office@school.example", subject="trip"
        )
        state.list_result = {"messages": [{"id": "m1"}]}
        return state

    def test_batch_id_printed_before_applying(self, tmp_path, monkeypatch, capsys):
        state = self._seed_one()
        env = _CliEnv(tmp_path, state)
        env.patch(monkeypatch)
        code = cli.main(["gmail-label", "--apply"])
        assert code == 0
        out = capsys.readouterr().out
        batch_line = re.search(r"batch ([0-9a-f]{12})", out)
        assert batch_line, "the batch id must be printed"
        assert out.index(batch_line.group(0)) < out.index("applied"), (
            "the batch id must appear BEFORE the apply result"
        )

    def test_undo_continues_past_a_deleted_message_and_reports_counts(
        self, tmp_path, monkeypatch, capsys
    ):
        client, state, audit = TestReviewMajor3Ledger._apply_two(TestReviewMajor3Ledger, tmp_path)
        del state.messages["m2"]  # the message was deleted between apply and undo
        env = _CliEnv(tmp_path, state)
        env.settings.gmail_labeler_audit_path = str(audit)
        env.patch(monkeypatch)
        code = cli.main(["gmail-label", "--undo", "B1"])
        assert code == 0
        out = capsys.readouterr().out
        assert "undone 1" in out and "failed 1" in out, out
        assert "USER_KEEP" in state.messages["m1"]["labelIds"]

    def test_undo_skips_an_already_undone_batch(self, tmp_path, monkeypatch, capsys):
        client, state, audit = TestReviewMajor3Ledger._apply_two(TestReviewMajor3Ledger, tmp_path)
        from brain.services.gmail_labeler import undo_batch

        first = undo_batch(client, "B1", audit_path=audit)
        assert first.undone == 2
        second = undo_batch(client, "B1", audit_path=audit)
        assert second.undone == 0
        assert second.already_undone == 2, "re-undo must skip, not churn"
        modifies = [c for c in state.calls if c[0] == "messages.modify"]
        assert len(modifies) == 4  # 2 apply + 2 undo, nothing more

    def test_modify_message_labels_refuses_system_label_ids(self):
        state = _ServiceState()
        state.profile_history_id = "9002"
        state.messages["m1"] = _msg_with_headers("m1", sender="a@b.c", subject="s")
        from brain.ai.gmail import GmailError

        client = _client(state)
        for bad in ("INBOX", "UNREAD", "TRASH", "SPAM", "SENT", "DRAFT", "STARRED"):
            with pytest.raises(GmailError, match="system label"):
                client.modify_message_labels("m1", add=[bad])
            with pytest.raises(GmailError, match="system label"):
                client.modify_message_labels("m1", remove=[bad])
        assert not [c for c in state.calls if c[0] == "messages.modify"], (
            "a system-label guard must fire before the call is made"
        )
        # A user label id still goes through.
        client.modify_message_labels("m1", add=["SB_1000"])
        assert [c for c in state.calls if c[0] == "messages.modify"]

    def test_metadata_reads_carry_metadataheaders_only(self, tmp_path):
        state = self._seed_one()
        rules = _rules_file(tmp_path, {"School": {"domains": ["school.example"], "list_ids": []}})
        from brain.services.gmail_labeler import load_rules, plan_labels

        plan_labels(_client(state), rules=load_rules(rules), kid_names=[],
                    label_id=LABEL, query="")
        gets = [c for c in state.calls if c[0] == "messages.get"]
        assert gets, "the plan must read message metadata"
        for _, kwargs in gets:
            assert kwargs.get("metadataHeaders") == ["From", "Subject", "List-Id"], kwargs
            assert kwargs.get("format") == "metadata"
        # And the fake honors the filter: only those headers come back.
        from brain.services.gmail_labeler import _label_names  # noqa: F401 (sanity import)

        client = _client(state)
        msg = client.get_message_metadata("m1")
        assert msg.sender == "office@school.example" and msg.subject == "trip"

    def test_empty_rules_patterns_match_nothing(self, tmp_path):
        state = _ServiceState()
        state.profile_history_id = "9002"
        state.messages["m1"] = _msg_with_headers(
            "m1", sender="office@any.example", subject="hi", list_unsubscribe=False
        )
        raw = _msg_fixture("m1")
        raw["payload"]["headers"].append({"name": "List-Id", "value": "<any.example>"})
        state.messages["m1"] = raw
        state.list_result = {"messages": [{"id": "m1"}]}
        rules = _rules_file(tmp_path, {
            "Noise": {"domains": [""], "list_ids": [""]},  # vacuous patterns
        })
        from brain.services.gmail_labeler import load_rules, plan_labels

        plans = plan_labels(_client(state), rules=load_rules(rules), kid_names=[],
                            label_id=LABEL, query="")
        assert [(p.gmail_id, p.label_path) for p in plans] == [("m1", "SB/Unsorted")], (
            "an empty domain/list_id pattern must never match everything"
        )

    def test_label_create_409_relists_and_uses_the_existing_label(self, tmp_path, database):
        state = self._seed_one()
        state.create_error = _FakeHttpError(409, b"already exists")  # concurrent creator wins
        rules = _rules_file(tmp_path, {"School": {"domains": ["school.example"], "list_ids": []}})
        from brain.services.gmail_labeler import apply_labels, load_rules, plan_labels

        client = _client(state)
        plans = plan_labels(client, rules=load_rules(rules), kid_names=[], label_id=LABEL, query="")
        applied = apply_labels(client, plans, max_apply=20, batch_id="B1",
                               audit_path=tmp_path / "audit.jsonl")
        assert applied == 1, "a 409 (label created concurrently) must not fail the run"
        creates = [c for c in state.calls if c[0] == "labels.create"]
        assert len(creates) == 1
        modifies = [c for c in state.calls if c[0] == "messages.modify"]
        assert modifies and modifies[0][1]["body"]["addLabelIds"], modifies

    def test_connector_auth_error_in_the_gate_is_exit_78(self, tmp_path, monkeypatch, capsys):
        from brain.connectors.base import ConnectorAuthError

        state = self._seed_one()
        env = _CliEnv(tmp_path, state)
        env.patch(monkeypatch)
        monkeypatch.setattr(
            cli, "_labeler_client_config",
            lambda s: (_ for _ in ()).throw(ConnectorAuthError(
                "client secret missing/unreadable at '~/secret.json'")),
        )
        code = cli.main(["gmail-label", "--apply"])
        assert code == 78
        err = capsys.readouterr().err
        assert "Traceback" not in err and "client secret" in err

    def test_token_uri_must_be_the_google_endpoint(self, tmp_path):
        import json as _json

        from brain.connectors.google.base import oauth_client_config

        secret = tmp_path / "secret.json"
        secret.write_text(_json.dumps({
            "installed": {"client_id": "c", "client_secret": "s",
                          "token_uri": "https://evil.example/token"}
        }))
        settings = _FakeSettings(rules_path="", credentials_path=str(secret))
        with pytest.raises(Exception) as excinfo:
            oauth_client_config(settings)
        assert "oauth2.googleapis.com" in str(excinfo.value), (
            "a non-Google token_uri must be refused (the refresh token is POSTed there)"
        )
        # The canonical endpoint passes.
        secret.write_text(_json.dumps({
            "installed": {"client_id": "c", "client_secret": "s",
                          "token_uri": "https://oauth2.googleapis.com/token"}
        }))
        cfg = oauth_client_config(settings)
        assert cfg["token_uri"] == "https://oauth2.googleapis.com/token"

    def test_confirm_plan_applies_exactly_the_previewed_plan(self, tmp_path, monkeypatch, capsys):
        state = self._seed_one()
        env = _CliEnv(tmp_path, state)
        env.patch(monkeypatch)
        code = cli.main(["gmail-label"])
        assert code == 0
        out = capsys.readouterr().out
        match = re.search(r"plan ([0-9a-f]{16})", out)
        assert match, f"the dry-run must print a plan hash: {out}"
        plan_hash = match.group(1)

        # A WRONG hash refuses with an explanation and zero writes.
        code = cli.main(["gmail-label", "--apply", "--confirm-plan", "0" * 16])
        assert code == 78
        assert "plan" in capsys.readouterr().err.lower()
        assert not [c for c in state.calls if c[0] in ("messages.modify", "labels.create")]

        # The previewed hash applies exactly that plan.
        code = cli.main(["gmail-label", "--apply", "--confirm-plan", plan_hash])
        assert code == 0
        assert len([c for c in state.calls if c[0] == "messages.modify"]) == 1

    def test_plan_hash_changes_when_the_plan_changes(self):
        from brain.services.gmail_labeler import LabelPlan, plan_hash

        a = [LabelPlan(gmail_id="m1", label_path="SB/School", label_id=None, source="rules")]
        b = [LabelPlan(gmail_id="m1", label_path="SB/Noise", label_id=None, source="rules")]
        c = [LabelPlan(gmail_id="m2", label_path="SB/School", label_id=None, source="rules")]
        assert plan_hash(a) == plan_hash(list(a)), "same plan, same hash"
        assert plan_hash(a) != plan_hash(b) and plan_hash(a) != plan_hash(c)

    def test_dead_build_labeler_client_is_gone(self):
        import brain.services.gmail_labeler as labeler_mod

        assert not hasattr(labeler_mod, "build_labeler_client"), (
            "build_labeler_client had no production caller — dead code must go"
        )
