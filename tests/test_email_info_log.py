"""The email agent's per-cycle INFO lines must reach the agent log.

launchd runs `brain gmail-poll` (scripts/run-email.sh) with NO Python
logging configured: the root logger stays at WARNING with only the
last-resort stderr handler, so the loop's per-cycle INFO line
("gmail-poll: state=ok listed=... new=...") is dropped and an operator
sees nothing in ~/Library/Logs/second-brain/email.log between warnings.

The plists route stdout -> email.log and stderr -> email.err.log
(scripts/service.sh), so the fix belongs in the gmail-poll dispatch path:
install an INFO stdout root handler there. The root.handlers simulation
follows tests/test_telegram_bot.py:656 (set inline in the test body —
pytest's logging plugin attaches its own root handlers per phase, so the
bare state must be established at the moment the code under test runs).
"""

import logging
import sys
import time

import pytest

import brain.cli as cli
from brain.connectors.runner import SyncSummary
from brain.services.keychain import GMAIL_ACCOUNT


@pytest.fixture(name="fake_keychain")
def fake_keychain_fixture(monkeypatch):
    class FakeKeychain:
        def __init__(self) -> None:
            self.stored: dict[str, str] = {}

        def store(self, account: str, secret: str) -> None:
            self.stored[account] = secret

        def read(self, account: str) -> str | None:
            return self.stored.get(account)

    kc = FakeKeychain()
    monkeypatch.setattr(cli, "Keychain", lambda: kc)
    return kc


class TestPollCycleLinesReachTheAgentLog:
    def test_info_cycle_line_reaches_stdout(self, monkeypatch, database, fake_keychain, capsys):
        """Red-first: with the launchd-bare root logger (no handlers, root at
        WARNING) the loop's per-cycle INFO line never reaches stdout — i.e.
        never lands in email.log."""
        root = logging.getLogger()
        saved_handlers, saved_level = root.handlers[:], root.level
        root.handlers = []  # simulate a bare launchd process
        try:
            fake_keychain.store(GMAIL_ACCOUNT, "1//fake-refresh-token")
            _one_ok_cycle_then_stop(monkeypatch)
            with pytest.raises(KeyboardInterrupt):
                cli.main(["gmail-poll"])
            out = capsys.readouterr().out
            assert "state=ok" in out, (
                "the loop's per-cycle INFO line never reached stdout (email.log): "
                "the gmail-poll entry point configures no logging, so the root "
                "logger stays at WARNING with no handler and drops INFO records"
            )
            assert "listed=12" in out
            assert "new=3" in out
            assert "next poll in" in out
        finally:
            root.handlers[:] = saved_handlers
            root.setLevel(saved_level)

    def test_cycle_line_carries_no_secrets(self, monkeypatch, database, fake_keychain, capsys):
        """The visible cycle line must carry counts only — never the refresh
        token, never an email address."""
        root = logging.getLogger()
        saved_handlers, saved_level = root.handlers[:], root.level
        root.handlers = []  # simulate a bare launchd process
        try:
            fake_keychain.store(GMAIL_ACCOUNT, "1//fake-refresh-token")
            _one_ok_cycle_then_stop(monkeypatch)
            with pytest.raises(KeyboardInterrupt):
                cli.main(["gmail-poll"])
            out = capsys.readouterr().out
            lines = [ln for ln in out.splitlines() if "gmail-poll: state=" in ln]
            assert lines, "no cycle line reached stdout at all"
            line = lines[0]
            assert "1//" not in line, "the refresh token must never appear in a log line"
            assert "@" not in line, "no email address may appear in a poll log line"
        finally:
            root.handlers[:] = saved_handlers
            root.setLevel(saved_level)

    def test_setup_leaves_preconfigured_agents_alone(self):
        """Agents that configure their own logging (api, telegram, whatsapp,
        connectors) must stay byte-identical: the new setup is a no-op when
        the root logger already has handlers."""
        from brain.logsetup import configure_agent_logging

        root = logging.getLogger()
        saved_handlers, saved_level = root.handlers[:], root.level
        existing = logging.StreamHandler(sys.stderr)
        try:
            root.handlers = [existing]  # telegram/api-style: already configured
            configure_agent_logging()
            assert root.handlers == [existing], (
                "configure_agent_logging must not touch an agent that already "
                "configured its own logging"
            )
        finally:
            root.handlers[:] = saved_handlers
            root.setLevel(saved_level)

    def test_setup_installs_one_info_stdout_handler(self, monkeypatch):
        """On a bare launchd root (no handlers) the setup installs exactly
        one stdout handler at INFO and pins the URL-echoing third-party
        loggers to WARNING."""
        from brain import logsetup

        root = logging.getLogger()
        saved_handlers, saved_level = root.handlers[:], root.level
        saved_httpx = logging.getLogger("httpx").level
        root.handlers = []
        try:
            logsetup.configure_agent_logging()
            mine = [h for h in root.handlers if getattr(h, "_brain_agent", False)]
            assert len(mine) == 1, f"expected exactly one installed handler, got {len(mine)}"
            assert mine[0].stream is sys.stdout, "the handler must write to stdout (email.log)"
            assert root.isEnabledFor(logging.INFO), "root must run at INFO"
            assert logging.getLogger("httpx").level >= logging.WARNING
            assert logging.getLogger("httpcore").level >= logging.WARNING
            assert logging.getLogger("googleapiclient").level >= logging.WARNING
        finally:
            root.handlers[:] = saved_handlers
            root.setLevel(saved_level)
            logging.getLogger("httpx").setLevel(saved_httpx)


def _patch_poll_seams(monkeypatch) -> None:
    """Seam the poll loop's dependencies: settings (interval 0), the cycle,
    the indexer, the attachment service — and time.sleep (the repo's seam
    pattern: the real sleep must never run under the test)."""

    import brain.config as config

    monkeypatch.setattr(time, "sleep", lambda seconds: None)
    settings = config.Settings(
        gmail_min_call_interval_s=0.0,
        gmail_poll_interval_s=0.0,
        gmail_label_id="Label_1",
        database_url="postgresql+psycopg://brain:brain@localhost:27432/brain_w22_email_test",
        qdrant_collection="brain_items_w22_email_test",
        _env_file=None,
    )
    monkeypatch.setattr(cli, "get_settings", lambda: settings)
    monkeypatch.setattr(cli, "_side_process_indexer", lambda: object())
    monkeypatch.setattr(cli, "_cli_attachment_service", lambda settings, indexer: object())


def _one_ok_cycle_then_stop(monkeypatch) -> None:
    """Cycle 1 returns a real ok summary (the INFO line must fire); cycle 2
    raises KeyboardInterrupt, which the poll loop re-raises."""
    calls = [0]

    def cycle(indexer, attachments):
        if calls[0] >= 1:
            raise KeyboardInterrupt
        calls[0] += 1
        return SyncSummary(
            connector="email", state="ok", fetched=12, created=3, skipped=1, errors=0,
        )

    monkeypatch.setattr(cli, "_gmail_poll_cycle", cycle)
    _patch_poll_seams(monkeypatch)
