"""labeler/label-guard: the gmail-label paths must REFUSE when
GMAIL_LABEL_ID is unset/blank — before any Gmail call.

The runbook promises "the labeler cannot reach outside the pipeline's
scope" (it only scans inside the connector's label). With the label unset,
the planning path passed ``label_ids=[None]``/``[""]`` into
``list_messages_page`` — an obscure API error or a whole-mailbox scan.
Repro first (red on 85f8066): the dry-run reached Gmail with the empty
label id; the fixes make every gmail-label subcommand except --auth refuse
with exit 78 and a message naming GMAIL_LABEL_ID.

Fakes only: the shared fake google service, no real keychain; DB
brain_w21_labeler_test / collection brain_items_w21_labeler_test.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from tests.test_connectors_gmail import LABEL, _ServiceState

TEST_DB_URL = "postgresql+psycopg://brain:brain@localhost:27432/brain_w21_labeler_test"
TEST_COLLECTION = "brain_items_w21_labeler_test"


def _client(state: _ServiceState) -> Any:
    from brain.ai.gmail import RestGmailClient

    return RestGmailClient(
        token_loader=lambda: "refresh-token",
        service_factory=lambda token, user_id: _FakeServiceShim(state),
        min_interval_s=0.0,
        max_retries=0,
    )


class _FakeServiceShim:
    def __init__(self, state: _ServiceState) -> None:
        self._state = state

    def users(self) -> Any:
        from tests.test_connectors_gmail import _FakeUsers

        return _FakeUsers(self._state)


def _msg_with_headers(mid: str, *, sender: str, subject: str) -> dict:
    from tests.test_connectors_gmail import _msg_fixture

    raw = _msg_fixture(mid)
    raw["labelIds"] = [LABEL]
    headers = raw["payload"]["headers"]
    headers[:] = [h for h in headers if h["name"] not in ("From", "Subject")]
    headers.append({"name": "From", "value": sender})
    headers.append({"name": "Subject", "value": subject})
    return raw


class _GuardSettings:
    """Just the fields the gmail-label paths read; ``gmail_label_id`` is the
    knob under test (unset = "", blank = "   ")."""

    def __init__(self, gmail_label_id: str = "", rules_path: str = "") -> None:
        self.gmail_label_id = gmail_label_id
        self.gmail_query = ""
        self.gmail_labeler_rules_path = rules_path
        self.gmail_min_call_interval_s = 0.0
        self.gmail_account = "owner@gmail.com"
        self.gmail_credentials_path = "/tmp/client-secret.json"
        self.database_url = TEST_DB_URL
        self.qdrant_collection = TEST_COLLECTION


def _granted_scope(monkeypatch: pytest.MonkeyPatch) -> None:
    """The consent gate passes; the REAL _labeler_token stays wired."""
    import brain.cli as cli
    import brain.services.gmail_labeler as labeler_mod

    monkeypatch.setattr(
        cli, "_labeler_client_config",
        lambda settings: {"client_id": "c", "client_secret": "s",
                          "token_uri": "https://oauth2.googleapis.com/token"},
    )
    real_check = labeler_mod.token_grants_modify

    def _checked(token: str, client_config: dict, poster: Any = None) -> bool:
        return real_check(token, client_config=client_config, poster=lambda f: {
            "scope": "https://www.googleapis.com/auth/gmail.readonly "
                     "https://www.googleapis.com/auth/gmail.modify"
        })

    monkeypatch.setattr(labeler_mod, "token_grants_modify", _checked)


def _patch_keychain(monkeypatch: pytest.MonkeyPatch,
                    items: dict[tuple[str, str], str] | None = None) -> Any:
    import brain.services.keychain as keychain_mod

    class _FakeKeychain:
        stores: list[tuple[str, str, str]] = []
        items: dict[tuple[str, str], str] = {}

        def __init__(self, runner: Any = None,
                     service: str = "second-brain-gmail") -> None:
            self._service = service

        def store(self, account: str, secret: str) -> None:
            _FakeKeychain.stores.append((self._service, account, secret))

        def read(self, account: str) -> str | None:
            return _FakeKeychain.items.get((self._service, account))

        def delete(self, account: str) -> None:
            pass

    _FakeKeychain.stores = []
    _FakeKeychain.items = dict(items or {})
    monkeypatch.setattr(keychain_mod, "Keychain", _FakeKeychain)
    return _FakeKeychain


@pytest.fixture(name="state")
def state_fixture() -> _ServiceState:
    state = _ServiceState()
    state.profile_history_id = "9002"
    state.messages["m1"] = _msg_with_headers(
        "m1", sender="office@school.example", subject="trip"
    )
    state.list_result = {"messages": [{"id": "m1"}]}
    return state


def _run_cli(monkeypatch: pytest.MonkeyPatch, state: _ServiceState,
             argv: list[str], *, label: str = "", tmp_path: Any = None) -> int:
    import brain.cli as cli

    settings = _GuardSettings(gmail_label_id=label)
    if tmp_path is not None:
        rules = tmp_path / "rules.json"
        rules.write_text(json.dumps({"School": {"domains": ["school.example"],
                                                "list_ids": []}}))
        settings.gmail_labeler_rules_path = str(rules)
    monkeypatch.setattr("brain.config.get_settings", lambda: settings)
    monkeypatch.setattr(cli, "_labeler_client",
                        lambda s, modify=False: _client(state))
    _granted_scope(monkeypatch)
    return cli.main(argv)


class TestCliRefusesWhenLabelUnset:
    @pytest.mark.parametrize("argv,label", [
        ([], ""),                       # dry-run
        (["--apply"], ""),              # apply
        ([], "   "),                    # whitespace-only label, dry-run
        (["--apply"], "   "),
    ])
    def test_scan_paths_refuse(self, argv, label, state, tmp_path, monkeypatch, capsys):
        """The planning paths (dry-run AND --apply) refuse with exit 78, a
        message naming GMAIL_LABEL_ID, and ZERO Gmail calls when the label
        id is unset or blank. On main, the dry-run reached Gmail with
        label_ids [''] (and --apply would have gone on to label the
        mailbox; it exited 78 only via the later token gate, with the wrong
        message)."""
        import brain.cli as cli

        settings = _GuardSettings(gmail_label_id=label)
        rules = tmp_path / "rules.json"
        rules.write_text(json.dumps({"School": {"domains": ["school.example"],
                                                "list_ids": []}}))
        settings.gmail_labeler_rules_path = str(rules)
        monkeypatch.setattr("brain.config.get_settings", lambda: settings)
        monkeypatch.setattr(cli, "_labeler_client",
                            lambda s, modify=False: _client(state))
        _granted_scope(monkeypatch)
        try:
            code = cli.main(["gmail-label", *argv])
        except SystemExit as exc:  # main's token-gate exit shape
            code = exc.code
        assert code == 78, "an unset GMAIL_LABEL_ID must refuse (exit 78)"
        err = capsys.readouterr().err
        assert "GMAIL_LABEL_ID" in err, "the refusal must name the GMAIL_LABEL_ID env var"
        list_calls = [c for c in state.calls if c[0] in ("messages.list", "labels.list")]
        assert list_calls == [], (
            "the refusal must happen before ANY Gmail call (the labeler only "
            f"ever scans inside GMAIL_LABEL_ID; on main the calls were {state.calls!r})"
        )

    def test_dry_run_repro_on_main_the_list_call_carries_the_blank_label(
        self, state, tmp_path, monkeypatch, capsys
    ):
        """The literal repro: with GMAIL_LABEL_ID unset, main's planning path
        called list_messages_page with the BLANK label id (label_ids ['']) —
        an obscure API error at best, a whole-mailbox scan at worst. After
        the fix this is a refusal and the fake never sees a list call."""
        import brain.cli as cli

        settings = _GuardSettings(gmail_label_id="")
        rules = tmp_path / "rules.json"
        rules.write_text(json.dumps({"School": {"domains": ["school.example"],
                                                "list_ids": []}}))
        settings.gmail_labeler_rules_path = str(rules)
        monkeypatch.setattr("brain.config.get_settings", lambda: settings)
        monkeypatch.setattr(cli, "_labeler_client",
                            lambda s, modify=False: _client(state))
        _granted_scope(monkeypatch)
        cli.main(["gmail-label"])
        list_calls = [c for c in state.calls if c[0] == "messages.list"]
        assert list_calls == [], (
            "with GMAIL_LABEL_ID unset the labeler must refuse BEFORE listing "
            f"(on main it sent {list_calls!r} — the blank label id)"
        )

    def test_undo_refuses(self, state, monkeypatch, capsys, database):
        """--undo refuses too (exit 78, GMAIL_LABEL_ID named, zero Gmail
        calls). On main it ran the gate and the DB undo, exit 0."""
        import brain.cli as cli

        monkeypatch.setattr("brain.config.get_settings",
                            lambda: _GuardSettings(gmail_label_id=""))
        monkeypatch.setattr(cli, "_labeler_client",
                            lambda s, modify=False: _client(state))
        _granted_scope(monkeypatch)
        try:
            code = cli.main(["gmail-label", "--undo", "deadbeef1234"])
        except SystemExit as exc:  # main's token-gate exit shape
            code = exc.code
        assert code == 78
        assert "GMAIL_LABEL_ID" in capsys.readouterr().err, (
            "the refusal must name GMAIL_LABEL_ID (on main --undo hit the "
            "token gate first, with the wrong message)"
        )
        assert state.calls == [], "the refusal must happen before ANY Gmail call"

    def test_audit_refuses(self, monkeypatch, capsys, database):
        """--audit refuses too (per the review decision: every subcommand
        except --auth). On main it exited 0."""
        import brain.cli as cli

        monkeypatch.setattr("brain.config.get_settings",
                            lambda: _GuardSettings(gmail_label_id=""))
        code = cli.main(["gmail-label", "--audit"])
        assert code == 78
        assert "GMAIL_LABEL_ID" in capsys.readouterr().err

    @pytest.mark.parametrize("label", [LABEL, "Label_123"])
    def test_a_set_label_proceeds_as_before(self, label, state, tmp_path,
                                            monkeypatch, capsys, database):
        """A set label id: the dry-run behaves exactly as before (plan
        printed, exit 0; --apply runs behind the gate)."""
        import brain.cli as cli

        settings = _GuardSettings(gmail_label_id=label)
        rules = tmp_path / "rules.json"
        rules.write_text(json.dumps({"School": {"domains": ["school.example"],
                                                "list_ids": []}}))
        settings.gmail_labeler_rules_path = str(rules)
        monkeypatch.setattr("brain.config.get_settings", lambda: settings)
        monkeypatch.setattr(cli, "_labeler_client",
                            lambda s, modify=False: _client(state))
        code = cli.main(["gmail-label"])
        assert code == 0
        out = capsys.readouterr().out
        assert "m1 -> SB/School" in out
        assert "dry-run" in out

    def test_auth_still_works_without_the_label(self, monkeypatch, capsys):
        """--auth does NOT scan and is NOT affected by the guard: with
        GMAIL_LABEL_ID unset it still performs the consent flow (faked here)
        and stores the labeler token."""
        from types import SimpleNamespace

        import brain.cli as cli

        monkeypatch.setattr("brain.config.get_settings",
                            lambda: _GuardSettings(gmail_label_id=""))

        class _FakeFlow:
            def __init__(self) -> None:
                self.credentials = SimpleNamespace(
                    refresh_token="1//fresh", token_uri="t", client_id="c",
                    client_secret="s",
                )

            def run_local_server(self, port: int = 0) -> None:
                pass

        monkeypatch.setattr(cli, "_build_oauth_flow", lambda path, scopes: _FakeFlow())
        monkeypatch.setattr(cli, "_labeler_profile_email",
                            lambda credentials: "owner@gmail.com")
        fake_keychain = _patch_keychain(monkeypatch)
        code = cli.main(["gmail-label", "--auth"])
        assert code == 0, "--auth must keep working when GMAIL_LABEL_ID is unset"
        assert any(e[0] == "second-brain-gmail-labeler"
                   for e in fake_keychain.stores), "the labeler token is stored"


class TestServiceGuard:
    def test_plan_labels_rejects_a_falsy_label_id(self, state, tmp_path):
        """Defense in depth: plan_labels raises ValueError for a falsy or
        whitespace-only label_id (it must never scan the whole mailbox). On
        main it happily issued the list call."""
        from brain.services.gmail_labeler import load_rules, plan_labels

        rules = tmp_path / "rules.json"
        rules.write_text(json.dumps({"School": {"domains": ["school.example"],
                                                "list_ids": []}}))
        client = _client(state)
        for bad in ("", "   "):
            with pytest.raises(ValueError):
                plan_labels(client, rules=load_rules(str(rules)), kid_names=[],
                            label_id=bad, query="")
        assert not [c for c in state.calls if c[0] == "messages.list"], (
            "a refused label_id must never reach Gmail"
        )

    def test_plan_labels_accepts_a_real_label(self, state, tmp_path):
        """A set label id: unchanged behavior (one page walk, plans built)."""
        from brain.services.gmail_labeler import load_rules, plan_labels

        rules = tmp_path / "rules.json"
        rules.write_text(json.dumps({"School": {"domains": ["school.example"],
                                                "list_ids": []}}))
        client = _client(state)
        plans = plan_labels(client, rules=load_rules(str(rules)), kid_names=[],
                            label_id=LABEL, query="")
        assert [(p.gmail_id, p.label_path) for p in plans] == [("m1", "SB/School")]
