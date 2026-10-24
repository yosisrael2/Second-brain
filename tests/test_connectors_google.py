"""GoogleConnector: shared OAuth creds, error translation, pagination.

All Google-family tests use injected fakes — no test ever builds a real
service, touches the keychain, or opens a socket (contract: no network in
tests, ever). Errors are duck-typed exactly like brain/ai/gmail.py does:
they only need ``.resp.status`` (googleapiclient HttpError) or a type name
(google.auth RefreshError), so the suite runs without the google extras.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from brain.connectors.base import (
    ConnectorAuthError,
    ConnectorError,
    ConnectorRetryableError,
    FetchResult,
)
from brain.connectors.google.base import GoogleConnector, is_rate_limited, scopes_for


class _HttpErrorLike(Exception):
    """Duck-typed googleapiclient.errors.HttpError (only .resp.status seen)."""

    def __init__(self, status: int) -> None:
        super().__init__(f"http {status}")
        self.resp = type("Resp", (), {"status": status})()


class RefreshError(Exception):
    """Duck-typed google.auth.exceptions.RefreshError (matched by name)."""


class _FakeRequest:
    """A google-api-python-client request stand-in (execute() -> payload)."""

    def __init__(self, payload: dict[str, Any]) -> None:
        self._payload = payload
        self.executed = False

    def execute(self) -> dict[str, Any]:
        self.executed = True
        return self._payload


class _StubGoogleConnector(GoogleConnector):
    """Minimal concrete subclass: fetch() surfaces iter_pages output."""

    name = "gcal"
    SCOPES = ("https://www.googleapis.com/auth/calendar.readonly",)
    API_NAME = "calendar"
    API_VERSION = "v3"

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)

    def fetch(self, cursor: str | None, *, limit: int) -> FetchResult:
        raise AssertionError("stub: adapters implement fetch")


def _fake_factory(calls: list[tuple[str, list[str]]], service: Any = None):
    def factory(token: str, scopes: list[str], api_name: str, api_version: str) -> Any:
        calls.append((token, list(scopes)))
        return service if service is not None else object()

    return factory


def test_default_service_factory_builds_the_subclass_api(monkeypatch, tmp_path):
    """Contract: the default factory passes the SUBCLASS's api name/version
    into discovery.build — a gcal adapter must never receive a gmail
    service. importlib.import_module is faked (no network, no google libs)."""
    import importlib
    import json as json_mod
    from types import SimpleNamespace

    from brain.connectors.base import FetchResult as _FR  # noqa: F401

    secret = tmp_path / "client_secret.json"
    secret.write_text(
        json_mod.dumps(
            {"installed": {
                "client_id": "cid",
                "client_secret": "sec",
                "token_uri": "https://token-uri",
            }}
        )
    )
    monkeypatch.setattr(
        "brain.config.get_settings",
        lambda: SimpleNamespace(gmail_credentials_path=str(secret)),
    )
    built: list[tuple[str, str]] = []

    class _FakeDiscovery:
        @staticmethod
        def build(api_name: str, api_version: str, **kwargs: Any) -> Any:
            built.append((api_name, api_version))
            assert kwargs.get("cache_discovery") is False
            return object()

    class _FakeCredentials:
        @staticmethod
        def Credentials(**kwargs: Any) -> Any:
            assert kwargs["refresh_token"] == "refresh-token"
            return object()

    def fake_import_module(name: str) -> Any:
        if name == "googleapiclient.discovery":
            return _FakeDiscovery
        if name == "google.oauth2.credentials":
            return _FakeCredentials
        raise AssertionError(f"unexpected import: {name}")

    monkeypatch.setattr(importlib, "import_module", fake_import_module)

    class _GcalDefaultFactory(_StubGoogleConnector):
        pass  # inherits API_NAME/API_VERSION from the stub

    _GcalDefaultFactory(token_loader=lambda: "refresh-token")._service()
    assert built == [("calendar", "v3")]


def test_subclass_must_declare_api_name_and_version() -> None:
    """A GoogleConnector subclass that forgets API_NAME/API_VERSION fails at
    construction — never as a mystery AttributeError on the first sync."""

    class _Forgotten(_StubGoogleConnector):
        API_NAME = ""
        API_VERSION = ""

    with pytest.raises(ConnectorError, match="API_NAME"):
        _Forgotten(token_loader=lambda: "refresh-token")


# --- construction + service caching -------------------------------------------


def test_missing_token_raises_auth_error_without_calling_the_factory():
    calls: list[tuple[str, list[str]]] = []
    connector = _StubGoogleConnector(
        token_loader=lambda: "", service_factory=_fake_factory(calls)
    )
    with pytest.raises(ConnectorAuthError):
        connector._service()
    assert calls == []  # no credential material, no factory call


def test_service_is_built_once_with_the_connector_scopes():
    calls: list[tuple[str, list[str]]] = []
    connector = _StubGoogleConnector(
        token_loader=lambda: "refresh-token", service_factory=_fake_factory(calls)
    )
    first = connector._service()
    second = connector._service()
    assert first is second  # cached
    assert calls == [("refresh-token", list(_StubGoogleConnector.SCOPES))]


def test_scope_union_covers_every_enabled_connector_exactly_once():
    scopes = scopes_for(["email", "gcal", "gdrive", "gtasks", "email"])
    assert scopes == [
        "https://www.googleapis.com/auth/gmail.readonly",
        "https://www.googleapis.com/auth/calendar.readonly",
        "https://www.googleapis.com/auth/drive.readonly",
        "https://www.googleapis.com/auth/tasks.readonly",
    ]
    assert scopes_for([]) == []
    assert scopes_for(["gmail"]) == scopes_for(["email"])  # alias resolves


# --- iter_pages ----------------------------------------------------------------


def test_iter_pages_follows_next_page_token_across_three_pages():
    pages = [
        {"items": [1], "nextPageToken": "t1"},
        {"items": [2], "nextPageToken": "t2"},
        {"items": [3]},
    ]
    seen_tokens: list[str | None] = []

    def request_fn(page_token: str | None) -> _FakeRequest:
        seen_tokens.append(page_token)
        return _FakeRequest(pages[len(seen_tokens) - 1])

    assert list(_StubGoogleConnector.iter_pages(request_fn)) == pages
    assert seen_tokens == [None, "t1", "t2"]


def test_iter_pages_translates_http_errors():
    def request_fn(page_token: str | None) -> _FakeRequest:
        class _Boom:
            def execute(self) -> None:
                raise _HttpErrorLike(500)

        return _Boom()

    with pytest.raises(ConnectorRetryableError):
        list(_StubGoogleConnector.iter_pages(request_fn))


# --- error translation (single source of truth for connector-family) ----------


@pytest.mark.parametrize(
    ("exc", "expected"),
    [
        (_HttpErrorLike(401), ConnectorAuthError),
        (_HttpErrorLike(403), ConnectorAuthError),
        (_HttpErrorLike(429), ConnectorRetryableError),
        (_HttpErrorLike(500), ConnectorRetryableError),
        (_HttpErrorLike(503), ConnectorRetryableError),
        (_HttpErrorLike(404), ConnectorError),
        (_HttpErrorLike(400), ConnectorError),
        (RefreshError("token expired"), ConnectorAuthError),
        (RuntimeError("connection reset"), ConnectorError),
    ],
)
def test_translate_error_maps_the_google_taxonomy(exc, expected):
    translated = _StubGoogleConnector._translate_error(exc)
    assert type(translated) is expected


def test_translate_error_preserves_the_status_for_cursor_invalidation():
    translated = _StubGoogleConnector._translate_error(_HttpErrorLike(410))
    assert isinstance(translated, ConnectorRetryableError)
    assert translated.status == 410  # adapters catch this and return reset=True


class _RateLimitedHttpError(_HttpErrorLike):
    """403 with Google's quota body (see is_rate_limited's docstring)."""

    def __init__(self, reason: str = "rateLimitExceeded") -> None:
        super().__init__(403)
        self.content = json.dumps(
            {
                "error": {
                    "code": 403,
                    "errors": [{"reason": reason}],
                    "message": "Rate limit exceeded",
                }
            }
        ).encode()


def test_403_with_rate_limit_reason_is_retryable_not_auth():
    for reason in ("rateLimitExceeded", "userRateLimitExceeded", "dailyLimitExceeded"):
        translated = _StubGoogleConnector._translate_error(_RateLimitedHttpError(reason))
        assert type(translated) is ConnectorRetryableError, reason
        assert translated.status == 403


def test_plain_403_is_still_auth():
    translated = _StubGoogleConnector._translate_error(_HttpErrorLike(403))
    assert type(translated) is ConnectorAuthError


def test_is_rate_limited_is_the_one_shared_check():
    assert is_rate_limited(_RateLimitedHttpError())
    assert not is_rate_limited(_HttpErrorLike(403))  # permission denied -> not throttling
    assert not is_rate_limited(_HttpErrorLike(429))  # status handles 429 already
