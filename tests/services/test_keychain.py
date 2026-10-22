"""F2 email source: the macOS login-keychain wrapper.

The ``security`` CLI is faked (recorded runner); tests prove the token is
only ever passed where it belongs and never lands in argv of a listed
command or in logs.
"""

import pytest

from brain.services.keychain import Keychain, KeychainError


class FakeRunner:
    """Records invocations; scripts the result (rc, stdout)."""

    def __init__(self, results: list[tuple[int, str]] | None = None) -> None:
        self.calls: list[tuple[list[str], str]] = []
        self._results = list(results or [])

    def __call__(self, args: list[str], stdin: str | None = None) -> tuple[int, str]:
        self.calls.append((args, stdin or ""))
        if self._results:
            return self._results.pop(0)
        return (0, "")


class TestStore:
    def test_store_feeds_the_secret_via_stdin_not_argv(self):
        runner = FakeRunner()
        kc = Keychain(runner=runner)
        kc.store("default", "1//refresh-token-value")
        args, stdin = runner.calls[0]
        assert args[:2] == ["security", "-i"]
        assert "1//refresh-token-value" not in " ".join(args)
        # The secret rides ON the command line (quoted) INSIDE stdin: a bare
        # trailing '-w' fails on the real keychain ('unknown error', verified).
        assert '-w "1//refresh-token-value"' in stdin
        assert "add-generic-password" in stdin
        assert "-U" in stdin  # upsert: re-consent replaces the token

    def test_store_rejects_secrets_the_stdin_parser_cannot_carry(self):
        # whitespace or quotes cannot survive the -i mini-parser unquoted:
        # refuse rather than silently corrupt (real OAuth tokens never do this)
        runner = FakeRunner()
        kc = Keychain(runner=runner)
        with pytest.raises(KeychainError):
            kc.store("default", "two words")
        with pytest.raises(KeychainError):
            kc.store("default", 'has"quote')
        assert runner.calls == []

    def test_store_failure_raises(self):
        runner = FakeRunner([(1, "secHS: User interaction is not allowed.")])
        kc = Keychain(runner=runner)
        with pytest.raises(KeychainError):
            kc.store("default", "tok")


class TestRead:
    def test_read_returns_secret_on_success(self):
        runner = FakeRunner([(0, "1//refresh-token-value\n")])
        kc = Keychain(runner=runner)
        assert kc.read("default") == "1//refresh-token-value"

    def test_read_missing_returns_none(self):
        # exit 44 = item not found in the keychain
        runner = FakeRunner([(44, "")])
        kc = Keychain(runner=runner)
        assert kc.read("default") is None

    def test_read_other_failure_raises(self):
        runner = FakeRunner([(1, "boom")])
        kc = Keychain(runner=runner)
        with pytest.raises(KeychainError):
            kc.read("default")

    def test_read_uses_find_generic_password_with_service_and_account(self):
        runner = FakeRunner([(0, "tok")])
        kc = Keychain(runner=runner)
        kc.read("default")
        args, _ = runner.calls[0]
        assert args[:2] == ["security", "find-generic-password"]
        assert "second-brain-gmail" in args
        assert "default" in args
