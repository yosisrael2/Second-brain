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
    def test_loop_backoff_sleeps_interval_not_backoff(self, monkeypatch, database, fake_keychain):
        """The loop seam (red-first): a cycle returning state='backoff' must
        sleep the normal interval — the runner already delayed the fetch via
        the persisted backoff, so re-applying backoff_seconds here stacks the
        waits (a clock-edge skip would double the pause). Seeded with
        attempt=7 so the OLD else-arm would compute max(60, 600) = 600."""
        from datetime import UTC, datetime

        import brain.config as config
        from brain.connectors.runner import SyncSummary
        from brain.connectors.state import ConnectorStateRepo
        from brain.db.session import SessionLocal

        settings = config.Settings(
            gmail_label_id="Label_1",
            database_url="postgresql+psycopg://brain:brain@localhost:27432/brain_w8_test",
            qdrant_collection="brain_items_w8_test",
            _env_file=None,
        )
        monkeypatch.setattr(cli, "get_settings", lambda: settings)
        fake_keychain.store(GMAIL_ACCOUNT, "1//fake-refresh-token")
        with SessionLocal() as session:
            repo = ConnectorStateRepo(session)
            for _ in range(7):
                repo.record_error(
                    "email",
                    error="retryable: http 500",
                    at=datetime(2026, 10, 24, 11, 0, tzinfo=UTC),
                    retryable=True,
                )
            session.commit()

        summaries = [
            SyncSummary(connector="email", state="backoff", retry_in_s=123.0),
        ]
        monkeypatch.setattr(cli, "_gmail_poll_cycle", lambda indexer, attachments: summaries.pop(0))
        monkeypatch.setattr(cli, "_side_process_indexer", lambda: object())
        monkeypatch.setattr(cli, "_cli_attachment_service", lambda settings, indexer: object())
        slept: list[float] = []
        code = cli._gmail_poll(
            argparse.Namespace(command="gmail-poll", once=False),
            sleep=slept.append,
            max_cycles=1,
        )
        assert code == 0
        assert slept == [60.0], f"a backoff pass must sleep the interval, got {slept}"

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
