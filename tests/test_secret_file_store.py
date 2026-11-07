"""The Linux secret backend: Keychain over a 0600 JSON file."""

from __future__ import annotations

import io
import json
import stat
import sys

import pytest

from brain import cli
from brain.services import keychain as kc
from brain.services.keychain import Keychain, KeychainError


@pytest.fixture
def secrets_file(tmp_path, monkeypatch):
    path = tmp_path / "cfg" / "second-brain" / "secrets.json"
    monkeypatch.setenv("BRAIN_SECRETS_FILE", str(path))
    monkeypatch.setenv("BRAIN_SECRET_STORE", "file")
    return path


def test_round_trip_and_permissions(secrets_file):
    Keychain().store("default", "1//token-a")
    Keychain(service="second-brain-gmail-labeler").store("default", "1//token-b")

    assert Keychain().read("default") == "1//token-a"
    assert Keychain(service="second-brain-gmail-labeler").read("default") == "1//token-b"
    assert json.loads(secrets_file.read_text()) == {
        "second-brain-gmail-labeler/default": "1//token-b",
        "second-brain-gmail/default": "1//token-a",
    }
    assert stat.S_IMODE(secrets_file.stat().st_mode) == 0o600
    assert stat.S_IMODE(secrets_file.parent.stat().st_mode) == 0o700


def test_missing_file_and_item_read_none(secrets_file):
    assert Keychain().read("default") is None
    Keychain().store("other", "1//x")
    assert Keychain().read("default") is None


def test_upsert_and_idempotent_delete(secrets_file):
    Keychain().store("default", "1//old")
    Keychain().store("default", "1//new")
    assert Keychain().read("default") == "1//new"
    Keychain().delete("default")
    Keychain().delete("default")
    assert Keychain().read("default") is None


def test_same_validation_as_keychain(secrets_file):
    with pytest.raises(KeychainError):
        Keychain().store("default", "has space")
    assert not secrets_file.exists()


def test_corrupt_file_raises_without_leaking(secrets_file):
    secrets_file.parent.mkdir(parents=True)
    secrets_file.write_text("1//not-json-secret")
    with pytest.raises(KeychainError) as excinfo:
        Keychain().read("default")
    assert "1//not-json-secret" not in str(excinfo.value)


def test_backend_selection(monkeypatch):
    monkeypatch.delenv("BRAIN_SECRET_STORE", raising=False)
    monkeypatch.setattr(sys, "platform", "linux")
    assert kc.use_file_store() is True
    monkeypatch.setattr(sys, "platform", "darwin")
    assert kc.use_file_store() is False
    monkeypatch.setenv("BRAIN_SECRET_STORE", "file")
    assert kc.use_file_store() is True
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setenv("BRAIN_SECRET_STORE", "keychain")
    assert kc.use_file_store() is False


def test_injected_runner_always_uses_keychain(secrets_file):
    calls = []

    def runner(args, stdin=None):
        calls.append(args)
        return 0, "1//from-keychain\n"

    assert Keychain(runner=runner).read("default") == "1//from-keychain"
    assert calls and calls[0][0] == "security"
    assert not secrets_file.exists()


def test_cli_secret_store_reads_stdin(secrets_file, monkeypatch, capsys):
    monkeypatch.setattr(sys, "stdin", io.StringIO("1//piped-token\n"))
    assert cli.main(["secret-store", "second-brain-gmail", "default"]) == 0
    out = capsys.readouterr().out
    assert "1//piped-token" not in out
    assert Keychain().read("default") == "1//piped-token"


def test_cli_secret_store_empty_stdin(secrets_file, monkeypatch):
    monkeypatch.setattr(sys, "stdin", io.StringIO(""))
    assert cli.main(["secret-store", "second-brain-gmail"]) == 78
    assert not secrets_file.exists()
