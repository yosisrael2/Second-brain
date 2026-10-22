"""F2 email source: the Gmail client seam and list params.

The rest client is tested offline: the google-api-python-client service is
injected (``service_factory``), so no test ever touches Google. Error
mapping is duck-typed (``exc.resp.status``) so the mapping is testable
without google libs installed.
"""

from typing import Any

import pytest

from brain.ai.gmail import (
    GmailAuthError,
    GmailLabelError,
    RestGmailClient,
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
