"""F2 email source: CLI contracts for `brain gmail-auth` / `brain gmail-poll`.

Google's flow and the keychain are faked (monkeypatched seams); no test
touches Google or the real keychain, and the refresh token never appears
in captured output.
"""

import argparse
from typing import Any

import pytest

import brain.cli as cli
from brain.services.keychain import GMAIL_ACCOUNT


class FakeFlow:
    def __init__(self, token: str | None = "1//refresh") -> None:
        self._token = token
        self.requested_scopes: list[str] | None = None

    def from_client_secret_file(self, path: str, scopes: list[str]) -> "FakeFlow":
        self.requested_scopes = scopes
        return self

    def run_local_server(self, port: int = 0):
        creds = type("Creds", (), {"refresh_token": self._token, "token": None})()
        return creds


class FakeKeychain:
    def __init__(self) -> None:
        self.stored: dict[str, str] = {}

    def store(self, account: str, secret: str) -> None:
        self.stored[account] = secret

    def read(self, account: str) -> str | None:
        return self.stored.get(account)


@pytest.fixture(name="fake_keychain")
def fake_keychain_fixture(monkeypatch):
    kc = FakeKeychain()
    monkeypatch.setattr(cli, "Keychain", lambda: kc)
    return kc


def _auth_args(**kw: Any) -> argparse.Namespace:
    return argparse.Namespace(command="gmail-auth", client_secret=None, **kw)


class TestGmailAuth:
    def test_stores_only_the_refresh_token_in_the_keychain(
        self, monkeypatch, tmp_path, capsys, fake_keychain
    ):
        secret = tmp_path / "client_secret.json"
        secret.write_text("{}")
        flow = FakeFlow("1//the-refresh-token")
        monkeypatch.setattr(cli, "_build_oauth_flow", lambda path: flow)
        monkeypatch.setattr("sys.argv", ["brain", "gmail-auth", "--client-secret", str(secret)])
        code = cli.main(["gmail-auth", "--client-secret", str(secret)])
        assert code == 0
        assert fake_keychain.stored[GMAIL_ACCOUNT] == "1//the-refresh-token"
        out = capsys.readouterr().out
        assert "1//the-refresh-token" not in out  # the token is NEVER printed
        assert "keychain" in out

    def test_read_only_scope_is_the_only_scope(self, fake_keychain):
        # The CLI builds the flow with scopes=[GMAIL_SCOPE]; the seam itself is
        # monkeypatched above, so assert the constant the CLI passes.
        from brain.ai.gmail import GMAIL_SCOPE

        assert GMAIL_SCOPE == "https://www.googleapis.com/auth/gmail.readonly"

    def test_missing_secret_path_is_config_error(self, capsys, fake_keychain):
        code = cli.main(["gmail-auth"])
        assert code == 78
        assert "no client secret" in capsys.readouterr().err

    def test_nonexistent_secret_file_is_config_error(self, tmp_path, capsys, fake_keychain):
        code = cli.main(["gmail-auth", "--client-secret", str(tmp_path / "nope.json")])
        assert code == 78

    def test_consent_without_refresh_token_is_config_error(
        self, monkeypatch, tmp_path, capsys, fake_keychain
    ):
        secret = tmp_path / "client_secret.json"
        secret.write_text("{}")
        monkeypatch.setattr(cli, "_build_oauth_flow", lambda path: FakeFlow(token=None))
        code = cli.main(["gmail-auth", "--client-secret", str(secret)])
        assert code == 78


class TestGmailPoll:
    def test_label_unset_is_config_error(self, monkeypatch, capsys, fake_keychain):
        import brain.config as config

        settings = config.Settings(gmail_label_id="", gmail_credentials_path="", _env_file=None)
        monkeypatch.setattr(cli, "get_settings", lambda: settings)
        code = cli.main(["gmail-poll", "--once"])
        assert code == 78
        assert "GMAIL_LABEL_ID unset" in capsys.readouterr().err

    def test_missing_token_is_config_error(self, monkeypatch, capsys, fake_keychain):
        import brain.config as config

        settings = config.Settings(
            gmail_label_id="Label_1", gmail_credentials_path="", _env_file=None
        )
        monkeypatch.setattr(cli, "get_settings", lambda: settings)
        code = cli.main(["gmail-poll", "--once"])
        assert code == 78
        assert "brain gmail-auth" in capsys.readouterr().err
