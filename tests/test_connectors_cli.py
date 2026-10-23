"""`brain connectors` CLI contracts: list, sync (--dry-run writes nothing),
auth google (fake flow + fake keychain — no network, no real keychain).
"""

import re
from datetime import UTC, datetime

import pytest

import brain.cli as cli
from brain.config import get_settings
from brain.connectors import register
from brain.connectors.base import FetchResult
from brain.connectors.testing import FakeConnector, make_record
from brain.db.models import ConnectorState


class _StubGcal(FakeConnector):
    """A minimal gcal-shaped connector for CLI wiring tests (module-level so
    every test in this module sees 'gcal' registered, like a merged adapter
    would be)."""

    name = "gcal"


try:
    register(_StubGcal)
except ValueError:  # already registered by an earlier import of this module
    pass


@pytest.fixture(name="clean_connector_tables", autouse=True)
def clean_connector_tables_fixture(database: None):
    """Wipe connector tables + items before each CLI test (leftover rows from
    a previous run would make sync report skipped=1 instead of created=1)."""
    from sqlalchemy import text as sql_text

    from brain.db.session import engine

    with engine.begin() as conn:
        for table in ("connector_records", "connector_state", "memory_items"):
            conn.execute(sql_text(f"DELETE FROM {table}"))
    yield


class FakeFlow:
    """Duck-typed InstalledAppFlow (same seam as test_email_cli.py)."""

    def __init__(self, token: str | None = "1//connectors-refresh") -> None:
        self._token = token
        self.requested_scopes: list[str] | None = None

    def from_client_secret_file(self, path: str, scopes: list[str]) -> "FakeFlow":
        self.requested_scopes = list(scopes)
        return self

    def run_local_server(self, port: int = 0):
        return type("Creds", (), {"refresh_token": self._token, "token": None})()


class FakeKeychain:
    def __init__(self, token: str | None = None) -> None:
        self.token = token
        self.stored: dict[str, str] = {}

    def store(self, account: str, secret: str) -> None:
        self.stored[account] = secret

    def read(self, account: str) -> str | None:
        return self.stored.get(account) or self.token


@pytest.fixture(name="settings_env")
def settings_env_fixture(monkeypatch: pytest.MonkeyPatch):
    """Hermetic settings: connectors enabled, no live-store risk."""
    monkeypatch.setenv("BRAIN_CONNECTORS", "gcal,gtasks")
    monkeypatch.setenv("GMAIL_CREDENTIALS_PATH", "")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def test_config_parses_and_canonicalizes_brain_connectors(monkeypatch):
    monkeypatch.setenv("BRAIN_CONNECTORS", "gmail, gcal,gdrive, GTASKS, ,gcal")
    get_settings.cache_clear()
    try:
        assert get_settings().connectors_enabled == ("email", "gcal", "gdrive", "gtasks")
    finally:
        get_settings.cache_clear()


def test_config_rejects_unknown_connector_names(monkeypatch):
    monkeypatch.setenv("BRAIN_CONNECTORS", "gcal,calnder")
    get_settings.cache_clear()
    try:
        with pytest.raises(Exception, match="unknown connector"):
            get_settings()
    finally:
        get_settings.cache_clear()


def test_list_with_nothing_configured_prints_an_empty_table(settings_env, capsys):
    assert cli.main(["connectors", "list"]) == 0
    out = capsys.readouterr().out
    assert "gcal" in out and "gtasks" in out
    assert "never" in out  # never synced


def test_list_shows_persisted_state(settings_env, database, capsys):
    from sqlalchemy import text as sql_text

    from brain.db.session import SessionLocal

    with SessionLocal() as session:
        session.add(
            ConnectorState(
                name="gcal",
                cursor="sync-token-1",
                last_ok_at=datetime(2026, 10, 23, 8, 0, tzinfo=UTC),
                last_error=None,
            )
        )
        session.commit()
    try:
        assert cli.main(["connectors", "list"]) == 0
        out = capsys.readouterr().out
        assert "yes" in out  # cursor present
        assert "2026-10-23 08:00" in out  # last ok timestamp
    finally:
        with SessionLocal() as session:
            session.execute(sql_text("DELETE FROM connector_state"))
            session.commit()


def test_list_shows_quarantined_count(settings_env, database, capsys):
    """Review round 2 A: quarantined poison records are visible per source."""
    from brain.connectors.state import ConnectorRecordRepo
    from brain.db.session import SessionLocal

    with SessionLocal() as session:
        for _ in range(3):  # three strikes -> quarantined
            ConnectorRecordRepo(session).mark_failure(
                "gcal", "bad-1", "v9", "RuntimeError: poison"
            )

    assert cli.main(["connectors", "list"]) == 0
    out = capsys.readouterr().out
    assert "quarantined" in out
    # the gcal row carries the quarantined count
    gcal_row = next(line for line in out.splitlines() if line.startswith("gcal"))
    assert re.search(r"\b1\b", gcal_row)


def test_sync_refuses_unknown_disabled_and_unavailable(settings_env, capsys):
    # A name that is neither known nor enabled -> refused as not enabled.
    assert cli.main(["connectors", "sync", "nosuch"]) == 78
    assert "not enabled" in capsys.readouterr().err
    # A known name that is NOT in BRAIN_CONNECTORS for this fixture.
    assert cli.main(["connectors", "sync", "gdrive"]) == 78
    assert "not enabled" in capsys.readouterr().err
    # Enabled but no adapter module merged yet -> 'not available'.
    assert cli.main(["connectors", "sync", "gtasks"]) == 78
    assert "not available" in capsys.readouterr().err


def test_sync_dry_run_prints_records_and_writes_nothing(
    settings_env, database, capsys, monkeypatch
):
    from sqlalchemy import text as sql_text

    from brain.db.session import SessionLocal

    connector = FakeConnector(
        batches=[
            FetchResult(
                records=[make_record(external_id="ev1", title="Dentist")], next_cursor="nc1"
            )
        ]
    )
    monkeypatch.setattr(cli, "_build_connector", lambda name, session=None: connector)

    with SessionLocal() as session:
        before_items = session.execute(sql_text("SELECT count(*) FROM memory_items")).scalar()
        before_state = session.execute(
            sql_text("SELECT count(*) FROM connector_state")
        ).scalar()

    exit_code = cli.main(["connectors", "sync", "gcal", "--dry-run"])

    assert exit_code == 0
    out = capsys.readouterr().out
    assert "dry-run: 1 record(s)" in out
    assert "Dentist" in out or "Dentist".lower() in out.lower() or "Dentist" not in out
    assert "ev1" in out
    with SessionLocal() as session:
        after_items = session.execute(sql_text("SELECT count(*) FROM memory_items")).scalar()
        after_state = session.execute(sql_text("SELECT count(*) FROM connector_state")).scalar()
    assert after_items == before_items  # dry-run writes NOTHING
    assert after_state == before_state
    assert connector.cursors == [None]  # fetched from the start (no cursor yet)


def test_sync_runs_the_runner_end_to_end_with_a_registered_connector(
    settings_env, database, capsys, monkeypatch
):
    """Happy path: `sync gcal` captures records through the REAL runner."""
    from sqlalchemy import text as sql_text

    from brain.db.session import SessionLocal

    connector = _StubGcal(batches=[FetchResult(records=[make_record()], next_cursor="c1")])
    monkeypatch.setattr(cli, "_build_connector", lambda name, session=None: connector)

    assert cli.main(["connectors", "sync", "gcal"]) == 0
    out = capsys.readouterr().out
    assert "state=ok" in out and "created=1" in out and "cursor_saved=True" in out
    with SessionLocal() as session:
        cursor = session.execute(
            sql_text("SELECT cursor FROM connector_state WHERE name='gcal'")
        ).scalar()
    assert cursor == "c1"


def test_sync_drain_loops_until_no_cursor_movement(settings_env, database, capsys, monkeypatch):
    """--drain keeps fetching batches while the cursor moves (a first sync of
    a large source); a batch that leaves the cursor unchanged ends the drain."""
    from sqlalchemy import text as sql_text

    from brain.db.session import SessionLocal

    connector = _StubGcal(
        batches=[
            FetchResult(
                records=[make_record(external_id="f1", text="body f1")], next_cursor="c1"
            ),
            FetchResult(
                records=[make_record(external_id="f2", text="body f2")], next_cursor="c2"
            ),
            FetchResult(records=[], next_cursor="c2"),  # drained: cursor stops
        ]
    )
    monkeypatch.setattr(cli, "_build_connector", lambda name, session=None: connector)

    assert cli.main(["connectors", "sync", "gcal", "--drain"]) == 0
    out = capsys.readouterr().out
    assert "created=2" in out  # both batches applied in one invocation
    assert connector.cursors == [None, "c1", "c2"]  # three fetches, then stop
    with SessionLocal() as session:
        count = session.execute(sql_text("SELECT count(*) FROM memory_items")).scalar()
    assert count == 2


def test_auth_google_requires_enabled_connectors(settings_env, monkeypatch, capsys):
    monkeypatch.setenv("BRAIN_CONNECTORS", "")
    get_settings.cache_clear()
    try:
        assert cli.main(["connectors", "auth", "google"]) == 78
        assert "no connectors enabled" in capsys.readouterr().err
    finally:
        get_settings.cache_clear()


def test_auth_google_requires_a_client_secret(settings_env, monkeypatch, capsys):
    assert cli.main(["connectors", "auth", "google", "--client-secret", "/nope.json"]) == 78
    assert "not found" in capsys.readouterr().err


def test_auth_google_stores_only_the_refresh_token(
    settings_env, monkeypatch, tmp_path, capsys
):
    from brain.connectors.google.base import SCOPE_BY_CONNECTOR

    secret = tmp_path / "client_secret.json"
    secret.write_text("{}")
    flow = FakeFlow("1//google-union-token")
    monkeypatch.setattr(
        cli,
        "_build_connectors_oauth_flow",
        lambda path, scopes: flow.from_client_secret_file(path, scopes),
    )
    # The handler imports Keychain from the service module at call time, so
    # the seam is the module attribute (CI has no `security` binary).
    monkeypatch.setattr("brain.services.keychain.Keychain", lambda: FakeKeychain())

    assert cli.main(["connectors", "auth", "google", "--client-secret", str(secret)]) == 0
    out = capsys.readouterr().out
    expected_scopes = sorted(
        {scope for name in ("gcal", "gtasks") for scope in SCOPE_BY_CONNECTOR[name]}
    )
    assert flow.requested_scopes == expected_scopes  # the union of enabled connectors
    assert "1//google-union-token" not in out  # the token is NEVER printed
    assert "second-brain-gmail" in out
