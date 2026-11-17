"""Nango as the Google connectors' token source (BRAIN_GOOGLE_AUTH=nango).

No network: Nango is an ``httpx.MockTransport``; the google libs are faked
via importlib (same approach as test_connectors_google.py).
"""

from __future__ import annotations

import importlib
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from brain.connectors.base import ConnectorAuthError, ConnectorError, ConnectorRetryableError
from brain.connectors.google.base import GoogleConnector
from brain.connectors.google.nango import (
    NangoConnection,
    NangoTokenClient,
    nango_service_factory,
)

NOW = datetime(2026, 10, 29, 12, 0, tzinfo=UTC)
SECRET = "nango-secret-xyz"
CONN = NangoConnection(provider_config_key="google-calendar", connection_id="conn-1")


def _client(handler: Any) -> NangoTokenClient:
    return NangoTokenClient(
        secret_key=SECRET,
        host="https://nango.test/",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        now=lambda: NOW,
    )


def _ok(token: str, expires_at: str | None) -> httpx.Response:
    credentials: dict[str, Any] = {"type": "OAUTH2", "access_token": token}
    if expires_at is not None:
        credentials["expires_at"] = expires_at
    return httpx.Response(200, json={"credentials": credentials})


def test_access_token_requests_the_connection_with_the_secret():
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return _ok("ya29.tok", "2026-10-29T13:00:00.000Z")

    token, expiry = _client(handler).access_token(CONN)

    assert token == "ya29.tok"
    # naive UTC — what google-auth compares against
    assert expiry == datetime(2026, 10, 29, 13, 0)
    assert expiry.tzinfo is None
    (request,) = seen
    assert request.url.path == "/connection/conn-1"
    assert request.url.params["provider_config_key"] == "google-calendar"
    assert "force_refresh" not in request.url.params
    assert request.headers["Authorization"] == f"Bearer {SECRET}"


def test_near_expiry_token_forces_a_nango_refresh():
    calls: list[str | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        forced = request.url.params.get("force_refresh")
        calls.append(forced)
        if forced:
            return _ok("fresh", "2026-10-29T13:00:00Z")
        return _ok("stale", "2026-10-29T12:02:00Z")

    token, _ = _client(handler).access_token(CONN)

    assert token == "fresh"
    assert calls == [None, "true"]


def test_missing_expiry_is_treated_as_short_lived():
    token, expiry = _client(lambda r: _ok("tok", None)).access_token(CONN)
    assert token == "tok"
    assert expiry == (NOW + timedelta(minutes=10)).replace(tzinfo=None)


@pytest.mark.parametrize(
    ("status", "error"),
    [
        (401, ConnectorAuthError),
        (404, ConnectorAuthError),
        (429, ConnectorRetryableError),
        (503, ConnectorRetryableError),
        (400, ConnectorError),
    ],
)
def test_nango_http_errors_map_onto_the_taxonomy(status, error):
    with pytest.raises(error) as excinfo:
        _client(lambda r: httpx.Response(status, json={})).access_token(CONN)
    assert excinfo.value.status == status
    assert SECRET not in str(excinfo.value)


def test_transport_failure_is_retryable():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("slow", request=request)

    with pytest.raises(ConnectorRetryableError):
        _client(handler).access_token(CONN)


def test_connection_without_access_token_is_an_auth_error():
    handler = lambda r: httpx.Response(200, json={"credentials": {"type": "API_KEY"}})  # noqa: E731
    with pytest.raises(ConnectorAuthError):
        _client(handler).access_token(CONN)


def test_empty_secret_is_refused():
    with pytest.raises(ConnectorAuthError):
        NangoTokenClient(secret_key="")


def test_service_factory_wires_a_refresh_handler(monkeypatch):
    built: list[tuple[str, str, Any]] = []

    class _FakeCredentialsMod:
        @staticmethod
        def Credentials(**kwargs: Any) -> Any:
            return SimpleNamespace(**kwargs)

    class _FakeDiscovery:
        @staticmethod
        def build(api_name: str, api_version: str, **kwargs: Any) -> Any:
            assert kwargs["cache_discovery"] is False
            built.append((api_name, api_version, kwargs["credentials"]))
            return "service"

    fakes = {
        "google.oauth2.credentials": _FakeCredentialsMod,
        "googleapiclient.discovery": _FakeDiscovery,
    }
    monkeypatch.setattr(importlib, "import_module", lambda name: fakes[name])
    client = _client(lambda r: _ok("ya29.tok", "2026-10-29T13:00:00Z"))

    factory = nango_service_factory(client, CONN)
    scopes = ["https://www.googleapis.com/auth/calendar.readonly"]
    assert factory("conn-1", scopes, "calendar", "v3") == "service"

    ((api_name, api_version, credentials),) = built
    assert (api_name, api_version) == ("calendar", "v3")
    assert credentials.token is None
    assert credentials.scopes == scopes
    assert "refresh_token" not in vars(credentials)
    assert credentials.refresh_handler(object(), scopes=scopes) == (
        "ya29.tok",
        datetime(2026, 10, 29, 13, 0),
    )


def test_connector_errors_from_the_refresh_handler_pass_through():
    """A Nango auth error raised inside google-auth's refresh must stay an
    auth error, not become a generic 'google call failed'."""
    original = ConnectorAuthError("nango google-mail http 404", status=404)
    assert GoogleConnector._translate_error(original) is original


# --- CLI wiring -------------------------------------------------------------


def _settings(**overrides: Any) -> SimpleNamespace:
    from pydantic import SecretStr

    values: dict[str, Any] = {
        "google_auth": "nango",
        "nango_secret_key": SecretStr(SECRET),
        "nango_host": "https://nango.test",
        "nango_gcal_connection_id": "conn-1",
        "nango_gcal_provider_config_key": "google-calendar",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_nango_seams_need_secret_and_connection_id(capsys):
    from brain import cli

    with pytest.raises(SystemExit) as excinfo:
        cli._nango_seams(_settings(nango_gcal_connection_id=""), "gcal")
    assert excinfo.value.code == 78
    assert "NANGO_GCAL_CONNECTION_ID" in capsys.readouterr().err

    with pytest.raises(SystemExit):
        cli._nango_seams(_settings(nango_secret_key=None), "gcal")


def test_build_connector_in_nango_mode_skips_the_keychain(monkeypatch):
    from brain import cli
    from brain.connectors import ensure_registered

    ensure_registered()

    class _NoKeychain:
        def read(self, account: str) -> str:
            raise AssertionError("nango mode must not read the keychain")

    monkeypatch.setattr("brain.services.keychain.Keychain", _NoKeychain)
    monkeypatch.setattr("brain.config.get_settings", lambda: _settings())

    connector = cli._build_connector("gcal")

    assert connector._token_loader() == "conn-1"
    assert connector._service_factory.__qualname__.startswith("nango_service_factory")


# --- gmail-poll stays on the keychain (review of #153) ------------------------


def _keychain_with(token: str) -> type:
    class _Keychain:
        reads: list[str] = []

        def read(self, account: str) -> str:
            _Keychain.reads.append(account)
            return token

    return _Keychain


def test_keychain_only_ignores_nango_mode(monkeypatch):
    """gmail-poll's seam: even with BRAIN_GOOGLE_AUTH=nango (and no Nango email
    connection configured) the email connector uses the keychain token."""
    from brain import cli
    from brain.connectors import ensure_registered
    from brain.connectors.google.base import default_service_factory

    ensure_registered()
    keychain = _keychain_with("1//keychain-refresh-token")
    monkeypatch.setattr("brain.services.keychain.Keychain", keychain)
    monkeypatch.setattr("brain.config.get_settings", lambda: _settings())

    connector = cli._build_connector("email", keychain_only=True)

    assert keychain.reads, "keychain_only must read the keychain"
    assert connector._token_loader() == "1//keychain-refresh-token"
    assert connector._service_factory is default_service_factory


def test_gmail_poll_cycle_builds_its_connector_keychain_only(monkeypatch):
    """The live email agent (gmail-poll) must not switch auth when
    BRAIN_GOOGLE_AUTH=nango lands in .env for the connector commands."""
    import contextlib

    from brain import cli

    class _Stop(Exception):
        pass

    seen: dict[str, Any] = {}

    def spy(name: str, session: Any = None, **kwargs: Any) -> Any:
        seen.update(name=name, **kwargs)
        raise _Stop

    monkeypatch.setattr(cli, "SessionLocal", lambda: contextlib.nullcontext(object()))
    monkeypatch.setattr(cli, "get_settings", lambda: _settings())
    monkeypatch.setattr(cli, "_build_poll_enrichment", lambda *a, **k: None)
    monkeypatch.setattr("brain.api.deps.get_classifier", lambda: object())
    monkeypatch.setattr("brain.api.deps.get_item_indexer", lambda: object())
    monkeypatch.setattr(cli, "_build_connector", spy)

    with pytest.raises(_Stop):
        cli._gmail_poll_cycle(object(), object())

    assert seen["name"] == "email"
    assert seen.get("keychain_only") is True


@pytest.mark.parametrize(
    ("mode", "expected", "absent"),
    [
        ("nango", "reconnect the account in Nango", "brain connectors auth google"),
        ("keychain", "brain connectors auth google", "Nango"),
    ],
)
def test_reconsent_hint_follows_the_token_source(monkeypatch, mode, expected, absent):
    from brain import cli

    monkeypatch.setattr(cli, "get_settings", lambda: _settings(google_auth=mode))

    hint = cli._google_reconsent_hint()

    assert expected in hint
    assert absent not in hint
