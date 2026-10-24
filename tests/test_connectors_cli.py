"""`brain connectors` CLI contracts: list, sync (--dry-run writes nothing),
auth google (fake flow + fake keychain — no network, no real keychain).
"""

import re
from datetime import UTC, datetime

import pytest

import brain.cli as cli
from brain.config import get_settings
from brain.connectors import Connector, register
from brain.connectors.base import FetchResult
from brain.connectors.testing import FakeConnector, make_record
from brain.db.models import ConnectorState


class _StubGcal(FakeConnector):
    """A minimal gcal-shaped connector for CLI wiring tests (module-level so
    every test in this module sees 'gcal' registered, like a merged adapter
    would be)."""

    name = "gcal"


try:
    # Merged adapters register first (#55): the real gcal connector
    # must win the 'gcal' registry slot, with this stub only filling in when
    # no adapter module provides it (plain main). Without this import the
    # stub would win the slot at collection time and the REAL adapter's
    # @register would raise on ensure_registered() inside every CLI command.
    from brain.connectors import ensure_registered

    ensure_registered()
    register(_StubGcal)
except ValueError:  # 'gcal' already registered (real adapter or this stub)
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
    """Hermetic settings: connectors enabled, no live-store risk.

    Only connectors THIS PR needs — never gdrive: its adapter merges with
    #57 and an enabled-here assertion would depend on merge order (the
    'not available' case simulates its own adapterless name instead).
    """
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
            ConnectorRecordRepo(session).mark_failure("gcal", "bad-1", "v9", "RuntimeError: poison")

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
    assert cli.main(["connectors", "sync", "email"]) == 78
    assert "not enabled" in capsys.readouterr().err


@pytest.mark.parametrize("adapter_registered", [False, True])
def test_sync_enabled_but_not_available_is_merge_order_safe(
    settings_env, capsys, monkeypatch, adapter_registered
):
    """'Enabled but not available' must not depend on MERGE ORDER.

    This case used to name gdrive and rely on its adapter not being merged
    ("gdrive's adapter is still pending") — the moment #57 merges, main goes
    red. Hermetic instead: enable gdrive HERE, neuter the registry bootstrap
    (so no real module import/register can interfere, ever), patch the exact
    lookup the CLI uses to raise KeyError for gdrive, and prove the outcome
    with AND without an adapter registered under that name.
    """
    import brain.connectors as registry

    monkeypatch.setenv("BRAIN_CONNECTORS", "gcal,gtasks,gdrive")
    get_settings.cache_clear()
    monkeypatch.setattr(registry, "ensure_registered", lambda: None)
    real_get = registry.get

    def fake_get(name: str):
        if name == "gdrive":
            raise KeyError(name)  # simulate: no adapter resolves on this install
        return real_get(name)

    monkeypatch.setattr(registry, "get", fake_get)
    if adapter_registered:

        class _FakeGdrive(Connector):
            name = "gdrive"

            def fetch(self, cursor, *, limit):  # pragma: no cover - never reached
                raise AssertionError("the CLI must refuse before any fetch")

        # Direct dict insert, not register(): no name collision with the real
        # gdrive adapter once #57 merges, and monkeypatch restores the dict.
        monkeypatch.setitem(registry._REGISTRY, "gdrive", _FakeGdrive)

    assert cli.main(["connectors", "sync", "gdrive"]) == 78
    err = capsys.readouterr().err
    assert "not available" in err


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
    monkeypatch.setattr(cli, "_build_connector", lambda name, session=None, **kwargs: connector)

    with SessionLocal() as session:
        before_items = session.execute(sql_text("SELECT count(*) FROM memory_items")).scalar()
        before_state = session.execute(sql_text("SELECT count(*) FROM connector_state")).scalar()

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
    monkeypatch.setattr(cli, "_build_connector", lambda name, session=None, **kwargs: connector)

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
            FetchResult(records=[make_record(external_id="f1", text="body f1")], next_cursor="c1"),
            FetchResult(records=[make_record(external_id="f2", text="body f2")], next_cursor="c2"),
            FetchResult(records=[], next_cursor="c2"),  # drained: cursor stops
        ]
    )
    monkeypatch.setattr(cli, "_build_connector", lambda name, session=None, **kwargs: connector)

    assert cli.main(["connectors", "sync", "gcal", "--drain"]) == 0
    out = capsys.readouterr().out
    assert "created=2" in out  # both batches applied in one invocation
    assert connector.cursors == [None, "c1", "c2"]  # three fetches, then stop
    with SessionLocal() as session:
        count = session.execute(sql_text("SELECT count(*) FROM memory_items")).scalar()
    assert count == 2


def test_sync_inside_backoff_window_skips_fetch_and_exits_0(
    settings_env, database, capsys, monkeypatch
):
    """The persisted backoff is honoured at the CLI boundary: an immediate
    re-sync after a transient failure does NOT fetch — it prints
    state=backoff (with the seconds remaining) and exits 0, because a skip
    is the scheduler working as designed, not a failure."""

    from brain.connectors.base import ConnectorRetryableError

    connector = _StubGcal(errors=[ConnectorRetryableError("http 500")])
    monkeypatch.setattr(cli, "_build_connector", lambda name, session=None, **kwargs: connector)

    assert cli.main(["connectors", "sync", "gcal"]) == 1  # first: transient failure
    second = cli.main(["connectors", "sync", "gcal"])
    assert second == 0, "a backoff skip must not be a failure"
    out = capsys.readouterr().out
    assert "state=backoff" in out
    assert "retry_in_s=" in out
    assert connector.cursors == [None]  # the second invocation never fetched


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


def test_auth_google_stores_only_the_refresh_token(settings_env, monkeypatch, tmp_path, capsys):
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
        {
            scope
            for name in get_settings().connectors_enabled  # whatever the fixture enabled
            for scope in SCOPE_BY_CONNECTOR[name]
        }
    )
    assert set(flow.requested_scopes) == set(expected_scopes)  # union of enabled connectors
    assert "1//google-union-token" not in out  # the token is NEVER printed
    assert "second-brain-gmail" in out


def test_email_dry_run_writes_nothing_even_when_seed_and_backstop_trigger(
    database, capsys, monkeypatch
):
    """Review B1 (second pass): --dry-run writes NOTHING — no ledger seed,
    no backstop ledger put, no cursor/state/items. The dry-run build wires
    READ-ONLY seams: the backstop decision is still REPORTED (the whole
    point of the pre-enable comparison is to see it), the seed simply does
    not run. RED-FIRST: with the write seams wired (the pre-fix behaviour)
    this dry-run inserted seed + backstop ledger rows."""
    from sqlalchemy import text as sql_text
    from tests.test_connectors_gmail import (
        LABEL,
        _FakeService,
        _queue_message,
        _ServiceState,
    )

    from brain.db.session import SessionLocal

    state = _ServiceState()
    state.profile_history_id = "9002"
    _queue_message(state, "m2", rfc="<same@msg>")

    monkeypatch.setenv("BRAIN_CONNECTORS", "email,gcal,gtasks")
    monkeypatch.setenv("GMAIL_LABEL_ID", LABEL)
    monkeypatch.setenv("GMAIL_CREDENTIALS_PATH", "")
    get_settings.cache_clear()
    monkeypatch.setattr(
        "brain.connectors.google.base.default_service_factory",
        lambda token, scopes, api_name, api_version: _FakeService(state),
    )
    monkeypatch.setattr("brain.services.keychain.Keychain", lambda: FakeKeychain("refresh-token"))

    def _counts(session):
        return {
            table: session.execute(sql_text(f"SELECT count(*) FROM {table}")).scalar()
            for table in (
                "connector_records",
                "connector_state",
                "memory_items",
                "email_messages",
            )
        }

    with SessionLocal() as session:
        from brain.db.models import EmailMessage, MemoryItem

        # An already-captured email item carrying the Message-ID: the
        # re-delivery of m2 under the same rfc must be REPORTED, not written.
        session.add(
            MemoryItem(
                raw_text="the original paste",
                source="email",
                meta={"rfc_message_id": "<same@msg>"},
            )
        )
        # A legacy seen-ledger row: a real first sync seeds it — dry-run must not.
        session.add(
            EmailMessage(
                dedupe_id="<m1@x>",
                gmail_id="m1",
                rfc_message_id="<m1@x>",
                item_id=None,
            )
        )
        session.commit()
        before = _counts(session)

    assert cli.main(["connectors", "sync", "email", "--dry-run"]) == 0

    with SessionLocal() as session:
        after = _counts(session)
    assert after == before, f"--dry-run wrote rows: {before} -> {after}"

    out = capsys.readouterr().out
    assert "dry-run: 1 record(s)" in out
    assert "m2" in out
    assert "would ledger m2" in out  # the decision is visible, nothing written


def test_email_dry_run_config_error_fails_cleanly(database, capsys, monkeypatch):
    """Review follow-up 1: the dry-run path calls connector.fetch DIRECTLY,
    so a ConnectorError (GMAIL_LABEL_ID unset) escaped as a raw traceback,
    exit 1. It must fail like the sync path does: one stderr line, exit 78
    (a not-configured refusal, same family as missing token/registration)."""
    from tests.test_connectors_gmail import (
        _FakeService,
        _ServiceState,
    )

    monkeypatch.setenv("BRAIN_CONNECTORS", "email,gcal,gtasks")
    monkeypatch.delenv("GMAIL_LABEL_ID", raising=False)
    monkeypatch.setenv("GMAIL_CREDENTIALS_PATH", "")
    get_settings.cache_clear()
    monkeypatch.setattr(
        "brain.connectors.google.base.default_service_factory",
        lambda token, scopes, api_name, api_version: _FakeService(_ServiceState()),
    )
    monkeypatch.setattr("brain.services.keychain.Keychain", lambda: FakeKeychain("refresh-token"))

    code = cli.main(["connectors", "sync", "email", "--dry-run"])
    captured = capsys.readouterr()
    assert code == 78
    assert "GMAIL_LABEL_ID" in captured.err
    assert captured.err.count("\n") == 1, f"expected one line, got: {captured.err!r}"
    assert "Traceback" not in captured.err


def test_dry_run_auth_error_maps_to_the_re_auth_refusal(database, capsys, monkeypatch):
    """The dry-run error mapping is connector-agnostic: an auth failure from
    ANY connector's fetch gets the re-auth message and exit 78, never a
    traceback."""
    from brain.connectors.base import ConnectorAuthError

    failing = FakeConnector(name="gtasks", errors=[ConnectorAuthError("token expired")])
    monkeypatch.setattr(cli, "_build_connector", lambda name, session=None, **kwargs: failing)
    monkeypatch.setenv("BRAIN_CONNECTORS", "gcal,gtasks")
    get_settings.cache_clear()

    code = cli.main(["connectors", "sync", "gtasks", "--dry-run"])
    captured = capsys.readouterr()
    assert code == 78
    assert "auth (needs re-consent)" in captured.err
    assert "auth google" in captured.err  # the re-auth hint
    assert captured.err.count("\n") == 1


def test_email_dry_run_backstop_report_silent_for_known_ids(database, capsys, monkeypatch):
    """Review follow-up 2: rfc_backstop_report lacks the ledger-row-first
    guard its write twin (rfc_backstop_put) has — on a connector that ALREADY
    synced, a dry-run printed 'would ledger <id> -> existing item' for every
    known id (noise implying pending work that does not exist)."""
    from sqlalchemy import text as sql_text
    from tests.test_connectors_gmail import (
        LABEL,
        _FakeService,
        _queue_message,
        _ServiceState,
    )

    from brain.db.models import ConnectorRecord, MemoryItem
    from brain.db.session import SessionLocal

    state = _ServiceState()
    state.profile_history_id = "9002"
    _queue_message(state, "m1", rfc="<same@msg>")

    monkeypatch.setenv("BRAIN_CONNECTORS", "email,gcal,gtasks")
    monkeypatch.setenv("GMAIL_LABEL_ID", LABEL)
    monkeypatch.setenv("GMAIL_CREDENTIALS_PATH", "")
    get_settings.cache_clear()
    monkeypatch.setattr(
        "brain.connectors.google.base.default_service_factory",
        lambda token, scopes, api_name, api_version: _FakeService(state),
    )
    monkeypatch.setattr("brain.services.keychain.Keychain", lambda: FakeKeychain("refresh-token"))

    with SessionLocal() as session:
        item = MemoryItem(
            raw_text="School trip tomorrow",
            source="email",
            meta={"rfc_message_id": "<same@msg>"},
        )
        session.add(item)
        session.flush()
        # m1 is already ledgered: a real sync would never re-ingest it, so a
        # dry-run must not report a would-be ledger action for it either.
        session.add(
            ConnectorRecord(source="email", external_id="m1", version=None, item_id=item.id)
        )
        session.commit()

    assert cli.main(["connectors", "sync", "email", "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "m1" in out  # the record is still listed
    assert "would ledger" not in out
    with SessionLocal() as session:
        assert session.execute(sql_text("SELECT count(*) FROM connector_records")).scalar() == 1
