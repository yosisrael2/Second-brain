"""F2 email source: the Gmail client seam and list params.

The rest client is tested offline: the google-api-python-client service is
injected (``service_factory``), so no test ever touches Google. Error
mapping is duck-typed (``exc.resp.status``) so the mapping is testable
without google libs installed.
"""

from typing import Any

import pytest

from brain.ai.gmail import (
    GMAIL_SCOPE,
    GmailAuthError,
    GmailError,
    GmailLabelError,
    RestGmailClient,
    default_service_factory,
    gmail_list_params,
)

# --- the one list-params builder ----------------------------------------------


class TestListParams:
    def test_label_ids_always_present(self):
        params = gmail_list_params(query="", label_ids=["LABEL_123"])
        assert params["labelIds"] == ["LABEL_123"]

    def test_label_ids_present_even_with_query_and_multiple_labels(self):
        params = gmail_list_params(query="newer_than:1d", label_ids=["A", "B"])
        assert params["labelIds"] == ["A", "B"]
        assert params["q"] == "newer_than:1d"

    def test_empty_query_omitted_but_label_ids_kept(self):
        params = gmail_list_params(query="", label_ids=["A"])
        assert "q" not in params
        assert params["labelIds"] == ["A"]

    def test_no_label_ids_raises(self):
        # By construction: a list call without labelIds would read the whole
        # mailbox. The builder refuses to produce such params.
        with pytest.raises(ValueError):
            gmail_list_params(query="", label_ids=[])


# --- rest client over an injected fake service --------------------------------


class _FakeRequest:
    def __init__(self, result: Any = None, error: Exception | None = None) -> None:
        self._result = result
        self._error = error

    def execute(self) -> Any:
        if self._error is not None:
            raise self._error
        return self._result


class _FakeHttpError(Exception):
    """Duck-typed stand-in for googleapiclient.errors.HttpError."""

    def __init__(self, status: int, content: bytes = b"") -> None:
        super().__init__(f"http {status}")
        self.resp = type("Resp", (), {"status": status})()
        self.content = content


class _FakeMessages:
    def __init__(self, calls: list[dict[str, Any]], result: Any, error: Exception | None) -> None:
        self._calls = calls
        self._result = result
        self._error = error

    def list(self, **kwargs: Any) -> _FakeRequest:
        self._calls.append(kwargs)
        return _FakeRequest(result=self._result, error=error_if(self._error))

    def get(self, userId: str, id: str, format: str = "full") -> _FakeRequest:  # noqa: N803
        self._calls.append({"get": id, "format": format})
        return _FakeRequest(result=self._result, error=error_if(self._error))


def error_if(error: Exception | None) -> Exception | None:
    return error


class _FakeUsers:
    def __init__(self, messages: _FakeMessages) -> None:
        self._messages = messages

    def messages(self) -> _FakeMessages:
        return self._messages


class _FakeService:
    def __init__(self, users: _FakeUsers) -> None:
        self._users = users

    def users(self) -> _FakeUsers:
        return self._users


def _client(result: Any, error: Exception | None = None):
    calls: list[dict[str, Any]] = []
    service = _FakeService(_FakeUsers(_FakeMessages(calls, result, error)))
    client = RestGmailClient(
        token_loader=lambda: "refresh-token",
        service_factory=lambda token, user_id: service,
    )
    return client, calls


class TestRestGmailClient:
    def test_list_passes_label_ids_through(self):
        result = {"messages": [{"id": "m1", "threadId": "t1"}]}
        client, calls = _client(result)
        stubs = client.list_messages(query="", label_ids=["LABEL_1"])
        assert [s.id for s in stubs] == ["m1"]
        assert calls[0]["labelIds"] == ["LABEL_1"]
        assert calls[0]["userId"] == "me"

    def test_auth_error_on_401(self):
        client, _ = _client(None, _FakeHttpError(401))
        with pytest.raises(GmailAuthError):
            client.list_messages(query="", label_ids=["L"])

    def test_label_error_on_404(self):
        client, _ = _client(None, _FakeHttpError(404))
        with pytest.raises(GmailLabelError):
            client.list_messages(query="", label_ids=["GONE"])

    def test_transient_error_on_503(self):
        from brain.ai.gmail import GmailError

        client, _ = _client(None, _FakeHttpError(503))
        with pytest.raises(GmailError) as excinfo:
            client.list_messages(query="", label_ids=["L"])
        assert not isinstance(excinfo.value, (GmailAuthError, GmailLabelError))

    def test_refresh_error_maps_to_auth_error(self):
        # google-auth raises RefreshError when the refresh token was revoked.
        client, _ = _client(None, type("RefreshError", (Exception,), {})("invalid_grant"))
        with pytest.raises(GmailAuthError):
            client.list_messages(query="", label_ids=["L"])


# --- production refresh needs the OAuth client fields --------------------------


class TestDefaultServiceFactory:
    """google-auth cannot refresh with refresh_token alone: Credentials also
    needs client_id, client_secret and token_uri (from the Desktop client
    JSON). The factory loads that file (outside the repo)."""

    @staticmethod
    def _inject_fake_google(monkeypatch, captured):
        import sys
        import types

        creds_mod = types.ModuleType("google.oauth2.credentials")

        class FakeCredentials:
            def __init__(self, **kwargs):
                captured.update(kwargs)

        creds_mod.Credentials = FakeCredentials
        disco_mod = types.ModuleType("googleapiclient.discovery")
        disco_mod.build = lambda *args, **kwargs: "fake-service"
        monkeypatch.setitem(sys.modules, "google.oauth2.credentials", creds_mod)
        monkeypatch.setitem(sys.modules, "googleapiclient.discovery", disco_mod)

    @staticmethod
    def _write_client_secret(tmp_path):
        import json

        path = tmp_path / "client_secret.json"
        path.write_text(
            json.dumps(
                {
                    "installed": {
                        "client_id": "cid.apps.googleusercontent.com",
                        "client_secret": "sekret",
                        "token_uri": "https://oauth2.googleapis.com/token",
                        "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                    }
                }
            )
        )
        return path

    def test_credentials_receive_client_fields_and_scope(self, monkeypatch, tmp_path):
        from brain.config import get_settings

        captured: dict = {}
        self._inject_fake_google(monkeypatch, captured)
        path = self._write_client_secret(tmp_path)
        monkeypatch.setenv("GMAIL_CREDENTIALS_PATH", str(path))
        get_settings.cache_clear()
        try:
            service = default_service_factory("1//tok", "me")
        finally:
            get_settings.cache_clear()
        assert service == "fake-service"
        assert captured["client_id"] == "cid.apps.googleusercontent.com"
        assert captured["client_secret"] == "sekret"
        assert captured["refresh_token"] == "1//tok"
        assert captured["token_uri"] == "https://oauth2.googleapis.com/token"
        assert captured["scopes"] == [GMAIL_SCOPE]

    def test_missing_client_secret_path_is_a_clear_auth_error(self, monkeypatch):
        from brain.config import get_settings

        captured: dict = {}
        self._inject_fake_google(monkeypatch, captured)
        monkeypatch.setenv("GMAIL_CREDENTIALS_PATH", "")
        get_settings.cache_clear()
        try:
            with pytest.raises(GmailAuthError, match="client secret"):
                default_service_factory("1//tok", "me")
        finally:
            get_settings.cache_clear()
        assert captured == {}  # nothing constructed without the client file

    def test_invalid_client_secret_json_is_a_clear_auth_error(self, monkeypatch, tmp_path):
        from brain.config import get_settings

        captured: dict = {}
        self._inject_fake_google(monkeypatch, captured)
        path = tmp_path / "broken.json"
        path.write_text("{not json")
        monkeypatch.setenv("GMAIL_CREDENTIALS_PATH", str(path))
        get_settings.cache_clear()
        try:
            with pytest.raises(GmailAuthError, match="client secret"):
                default_service_factory("1//tok", "me")
        finally:
            get_settings.cache_clear()


class TestListPagination:
    def test_follows_pagetoken_up_to_a_small_cap(self):
        pages = [
            {"messages": [{"id": "m1", "threadId": "t1"}], "nextPageToken": "p2"},
            {"messages": [{"id": "m2", "threadId": "t1"}]},
        ]

        class PagingMessages:
            def __init__(self):
                self.calls = []

            def list(self, **kwargs):
                self.calls.append(kwargs)
                result = pages[min(len(self.calls) - 1, len(pages) - 1)]
                return _FakeRequest(result=result)

        paging = PagingMessages()
        client = RestGmailClient(
            token_loader=lambda: "refresh-token",
            service_factory=lambda token, user_id: _FakeService(_FakeUsers(paging)),
        )
        stubs = client.list_messages(query="", label_ids=["L"])
        assert [s.id for s in stubs] == ["m1", "m2"]
        assert paging.calls[1].get("pageToken") == "p2"

    def test_cap_stops_the_walk(self):
        class EndlessMessages:
            def __init__(self):
                self.calls = 0

            def list(self, **kwargs):
                self.calls += 1
                return _FakeRequest(
                    result={
                        "messages": [{"id": f"m{self.calls}", "threadId": "t"}],
                        "nextPageToken": f"p{self.calls}",
                    }
                )

        endless = EndlessMessages()
        client = RestGmailClient(
            token_loader=lambda: "refresh-token",
            service_factory=lambda token, user_id: _FakeService(_FakeUsers(endless)),
        )
        stubs = client.list_messages(query="", label_ids=["L"])
        assert endless.calls <= 5  # small cap: bounded walk per poll
        assert len(stubs) == endless.calls


# --- C5 additions: history walk, profile cursor, injected service --------------


class _FakeHistoryResource:
    def __init__(self, calls, pages, error=None):
        self._calls = calls
        self._pages = pages
        self._error = error

    def list(self, **kwargs):
        self._calls.append({"history.list": kwargs})
        page_token = kwargs.get("pageToken")
        if self._error is not None:
            return _FakeRequest(error=self._error)
        page = self._pages.get(page_token or "first", {"history": [], "historyId": "1"})
        return _FakeRequest(result=page)


class _FakeHistoryUsers:
    def __init__(self, calls, pages, profile, error=None):
        self._calls = calls
        self._pages = pages
        self._profile = profile
        self._error = error

    def history(self):
        return _FakeHistoryResource(self._calls, self._pages, self._error)

    def getProfile(self, userId):  # noqa: N803
        self._calls.append({"getProfile": userId})
        return _FakeRequest(result={"historyId": self._profile})


class TestHistoryWalk:
    def test_list_history_yields_pages_until_no_next_token(self):
        calls: list = []
        pages = {
            "first": {"history": [{"id": "1"}], "historyId": "2", "nextPageToken": "p2"},
            "p2": {"history": [{"id": "2"}], "historyId": "3"},
        }
        client = RestGmailClient(
            token_loader=lambda: "tok",
            service_factory=lambda token, user_id: _FakeService(
                _FakeUsers(_FakeMessages(calls, None, None))
            )
            if False
            else type(
                "S",
                (),
                {
                    "users": lambda self: _FakeHistoryUsers(
                        calls, pages, profile="3"
                    )
                },
            )(),
        )
        pages_out = list(
            client.list_history(start_history_id="9000", label_id="LABEL_1")
        )
        assert [p["historyId"] for p in pages_out] == ["2", "3"]
        assert calls[0]["history.list"]["startHistoryId"] == "9000"
        assert calls[0]["history.list"]["labelId"] == "LABEL_1"
        assert calls[1]["history.list"]["pageToken"] == "p2"

    def test_list_history_translates_http_errors(self):
        calls: list = []
        client = RestGmailClient(
            token_loader=lambda: "tok",
            service_factory=lambda token, user_id: type(
                "S", (), {"users": lambda self: _FakeHistoryUsers(
                    calls, pages={}, profile=None, error=_FakeHttpError(503)
                )}
            )(),
        )
        with pytest.raises(GmailError) as excinfo:
            list(client.list_history(start_history_id="1", label_id="L"))
        assert excinfo.value.status == 503

    def test_profile_history_id_returns_the_current_cursor(self):
        calls: list = []
        client = RestGmailClient(
            token_loader=lambda: "tok",
            service_factory=lambda token, user_id: type(
                "S", (), {"users": lambda self: _FakeHistoryUsers(calls, pages={}, profile="42")}
            )(),
        )
        assert client.profile_history_id() == "42"


class TestInjectedService:
    def test_prebuilt_service_skips_the_token_loader(self):
        """The connector passes the ONE shared service; no token read, no
        factory call."""
        calls: list = []

        class S:
            def users(self):
                calls.append("users")
                return _FakeUsers(_FakeMessages(calls, {"messages": []}, None))

        client = RestGmailClient(service=S())  # no token_loader at all
        stubs = client.list_messages(query="", label_ids=["L"])
        assert stubs == []
        assert calls  # the service was used directly


class TestDecodeAttachmentPayload:
    def test_decodes_b64url_and_refuses_empty_data(self):
        import base64

        from brain.ai.gmail import decode_attachment_payload

        blob = base64.urlsafe_b64encode(b"bytes!").decode().rstrip("=")
        raw = {"data": blob, "size": 6}
        assert decode_attachment_payload(raw, "a1", "m1") == b"bytes!"
        with pytest.raises(GmailError):
            decode_attachment_payload({}, "a1", "m1")
