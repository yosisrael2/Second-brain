"""macOS login-keychain wrapper for the Gmail refresh token (F2).

The token is a secret: it is stored under service ``second-brain-gmail``
and is ONLY ever fed to ``security`` via stdin — never argv (argv is
visible in ``ps``), never logs, never returned in errors. The runner is
injectable so tests exercise the exact invocation without touching the
real keychain.

Launchd note: ``security`` works from a per-user LaunchAgent (same user
session); the first read may raise one ACL prompt the user confirms once.
"""

from __future__ import annotations

import subprocess
from collections.abc import Callable

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


class Keychain:
    """Store/read the refresh token in the user's login keychain."""

    def __init__(self, runner: Runner | None = None, service: str = SERVICE) -> None:
        self._runner = runner or _subprocess_runner
        self._service = service

    def store(self, account: str, secret: str) -> None:
        """Create or update the item (``-U`` upsert) via ``security -i`` stdin.

        The secret goes on stdin, not argv.
        """
        command = f"add-generic-password -U -s {self._service} -a {account} -w\n"
        code, out = self._runner(["security", "-i"], stdin=command + secret + "\n")
        if code != 0:
            raise KeychainError(
                f"keychain store failed (exit {code}): {out.strip()[:200] or 'unknown error'}"
            )

    def read(self, account: str) -> str | None:
        """Return the stored secret, or None when the item does not exist."""
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
