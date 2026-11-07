"""macOS login-keychain wrapper for the Gmail refresh token (F2).

The token is a secret: it is stored under service ``second-brain-gmail``
and is ONLY ever fed to ``security`` via stdin — never argv (argv is
visible in ``ps``), never logs, never returned in errors. The runner is
injectable so tests exercise the exact invocation without touching the
real keychain.

Launchd note: ``security`` works from a per-user LaunchAgent (same user
session); the first read may raise one ACL prompt the user confirms once.

Linux hosts have no login keychain. There (or with
``BRAIN_SECRET_STORE=file``) the same items live in one JSON file,
``BRAIN_SECRETS_FILE`` or ``~/.config/second-brain/secrets.json``, written
atomically with mode 0600 inside a 0700 directory. ``BRAIN_SECRET_STORE=
keychain`` forces the macOS backend. An injected runner always means the
keychain backend, so the runner-based tests stay exact.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from collections.abc import Callable
from pathlib import Path

SERVICE = "second-brain-gmail"
# Fixed account for the Gmail refresh token (auth helper and poller agree).
GMAIL_ACCOUNT = "default"

# `security find-generic-password` exit code when the item does not exist.
_NOT_FOUND = 44


class KeychainError(RuntimeError):
    """A keychain operation failed (the secret value is NEVER included)."""


Runner = Callable[[list[str], str | None], tuple[int, str]]


def _subprocess_runner(args: list[str], stdin: str | None = None) -> tuple[int, str]:
    proc = subprocess.run(  # noqa: S603
        args,
        input=stdin,
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    return proc.returncode, proc.stdout


def use_file_store() -> bool:
    """True when secrets live in the JSON file instead of the macOS keychain."""
    choice = os.environ.get("BRAIN_SECRET_STORE", "").strip().lower()
    if choice == "file":
        return True
    if choice == "keychain":
        return False
    return sys.platform != "darwin"


def secrets_file_path() -> Path:
    env = os.environ.get("BRAIN_SECRETS_FILE", "").strip()
    if env:
        return Path(env)
    return Path.home() / ".config" / "second-brain" / "secrets.json"


class _FileStore:
    """``{"<service>/<account>": "<secret>"}`` in one 0600 JSON file."""

    def __init__(self, path: Path) -> None:
        self._path = path

    def _load(self) -> dict[str, str]:
        try:
            raw = self._path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return {}
        except OSError as exc:
            raise KeychainError(f"secrets file unreadable: {type(exc).__name__}") from None
        try:
            data = json.loads(raw)
        except ValueError:
            raise KeychainError(f"secrets file {self._path} is not valid JSON") from None
        if not isinstance(data, dict) or not all(
            isinstance(k, str) and isinstance(v, str) for k, v in data.items()
        ):
            raise KeychainError(f"secrets file {self._path} must map strings to strings")
        return data

    def _save(self, data: dict[str, str]) -> None:
        directory = self._path.parent
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=directory, prefix=".secrets-", suffix=".tmp")
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(data, handle, indent=2, sort_keys=True)
                handle.write("\n")
            os.replace(tmp, self._path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise

    def store(self, key: str, secret: str) -> None:
        data = self._load()
        data[key] = secret
        self._save(data)

    def read(self, key: str) -> str | None:
        value = self._load().get(key)
        return value.strip() if value is not None else None

    def delete(self, key: str) -> None:
        data = self._load()
        if data.pop(key, None) is not None:
            self._save(data)


class Keychain:
    """Store/read the refresh token in the user's login keychain.

    On non-macOS hosts (or ``BRAIN_SECRET_STORE=file``) the same calls go to
    the 0600 secrets file instead; see the module docstring.
    """

    def __init__(self, runner: Runner | None = None, service: str = SERVICE) -> None:
        self._runner = runner or _subprocess_runner
        self._service = service
        self._file = (
            _FileStore(secrets_file_path()) if runner is None and use_file_store() else None
        )

    def _key(self, account: str) -> str:
        return f"{self._service}/{account}"

    def store(self, account: str, secret: str) -> None:
        """Create or update the item (``-U`` upsert) via ``security -i`` stdin.

        The command line travels on STDIN, so the secret is never in any
        process argv (``ps`` shows only ``security -i``). The secret must be
        ON the command line (quoted): a bare trailing ``-w`` in -i mode
        fails with 'unknown error' (verified on the real login keychain —
        the fake runner cannot prove this). Secrets containing whitespace
        or double quotes cannot be represented in that mini-parser and are
        rejected rather than silently corrupted (real OAuth tokens are
        URL-safe and never contain either).
        """
        if (
            not secret
            or any(ch in secret for ch in ('"', "\n", "\r"))
            or len(secret.split()) != 1
        ):
            raise KeychainError(
                "refusing to store: the secret contains whitespace or quotes "
                "(real OAuth tokens never do — refusing beats silent corruption)"
            )
        if self._file is not None:
            self._file.store(self._key(account), secret)
            return
        command = (
            f"add-generic-password -U -s {self._service} -a {account} "
            f'-w "{secret}"\n'
        )
        code, out = self._runner(["security", "-i"], stdin=command)
        if code != 0:
            raise KeychainError(
                f"keychain store failed (exit {code}): {out.strip()[:200] or 'unknown error'}"
            )

    def read(self, account: str) -> str | None:
        """Return the stored secret, or None when the item does not exist."""
        if self._file is not None:
            return self._file.read(self._key(account))
        code, out = self._runner(
            [
                "security",
                "find-generic-password",
                "-s",
                self._service,
                "-a",
                account,
                "-w",
            ],
            None,
        )
        if code == _NOT_FOUND:
            return None
        if code != 0:
            raise KeychainError(f"keychain read failed (exit {code})")
        return out.strip()

    def delete(self, account: str) -> None:
        """Remove the item; a missing item is fine (idempotent)."""
        if self._file is not None:
            self._file.delete(self._key(account))
            return
        code, out = self._runner(
            [
                "security",
                "delete-generic-password",
                "-s",
                self._service,
                "-a",
                account,
            ],
            None,
        )
        if code not in (0, _NOT_FOUND):
            raise KeychainError(f"keychain delete failed (exit {code}): {out.strip()[:200]}")
