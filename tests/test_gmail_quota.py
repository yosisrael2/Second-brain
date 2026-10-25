"""Gmail quota hygiene (fix/gmail-quota-403): the live 403 rateLimitExceeded.

Diagnosis (unit math for gmail.googleapis.com 'Total Query Cost', the
'Units per minute per user' budget):
- adopt email --dry-run (limit=200) listed the WHOLE label eagerly and then
  issued one messages.get(format='full') per listed message (~201 messages
  in the school label) — a ~1000-unit burst inside one run.
- sync email --dry-run --limit 20 listed the whole label (~3 pages) and
  then GETted 20 messages — a ~115-unit burst in seconds.
Both blow the 100-units/minute/user budget immediately, and the client had
NO pacing and NO intra-run retry: the transient 403 killed the run.

Fixes under test here:
- bounded listing: --limit fetches only the pages it needs (maxResults);
- client-side pacing: a minimum interval between consecutive API calls;
- intra-run retry with exponential backoff (honoring Retry-After) on
  rateLimitExceeded / 429 — transient, never a hard failure; any OTHER 403
  stays hard.

Every test here is offline: the google service is a fake (the same
duck-typed harness as tests/test_connectors_gmail.py). No test touches the
live Gmail API and no test reads the keychain.
"""

from __future__ import annotations

import base64
import json
from typing import Any

import pytest

from brain.ai.gmail import RestGmailClient
from brain.config import Settings
from brain.connectors.google.gmail import GmailConnector

LABEL = "Label_school"


# --- a fake google service with page-aware list + scripted errors ------------


class _Request:
    """Duck-typed googleapiclient request: each execute() is a FRESH attempt
    (the real client re-sends the HTTP request), so scripted outcomes are
    consumed at execute time via ``fn``, never baked into the object."""

    def __init__(self, fn=None, result=None, error=None):
        self._fn = fn
        self._result = result
        self._error = error

    def execute(self):
        if self._fn is not None:
            return self._fn()
        if self._error is not None:
            raise self._error
        return self._result


class _HttpError(Exception):
    """Duck-typed googleapiclient.errors.HttpError (resp.status/headers/content)."""

    def __init__(self, status: int, content: bytes = b"", headers: dict | None = None):
        super().__init__(f"http {status}")
        self.resp = type("Resp", (), {"status": status, "headers": headers or {}})()
        self.content = content


def _rate_limited_content() -> bytes:
    return json.dumps(
        {"error": {"code": 403, "errors": [{"reason": "rateLimitExceeded"}],
         "message": "Quota exceeded for quota metric 'Total Query Cost'"}}
    ).encode()


def _full_message(mid: str) -> dict:
    """A minimal users.messages.get('format=full') response."""
    body = base64.urlsafe_b64encode(b"hello").decode().rstrip("=")
    return {
        "id": mid,
        "threadId": "t1",
        "internalDate": "1761127200000",
        "payload": {
            "mimeType": "text/plain",
            "headers": [
                {"name": "Subject", "value": f"msg {mid}"},
                {"name": "From", "value": "teacher@example.com"},
                {"name": "Date", "value": "Thu, 22 Oct 2026 10:00:00 +0000"},
                {"name": "Message-ID", "value": f"<{mid}@mail.gmail.com>"},
            ],
            "body": {"data": body},
        },
    }


class _State:
    """Scripted fake state: page-aware list, per-call error scripts."""

    def __init__(self, *, page_size_respects_max_results: bool = True):
        self.calls: list[tuple[str, dict]] = []
        self.messages: dict[str, dict] = {}
        # the full label, one flat list — the fake pages it like Gmail does
        self.all_ids: list[str] = []
        self.list_errors: list[Exception] = []  # popped once per list call
        self.get_errors: dict[str, list[Exception]] = {}
        self.profile_history_id = "9002"
        self.page_size_respects_max_results = page_size_respects_max_results

    def _page(self, kwargs: dict) -> dict:
        ids = list(self.all_ids)
        if self.page_size_respects_max_results:
            size = int(kwargs.get("maxResults", 500))
        else:
            size = len(ids)
        token = int(kwargs.get("pageToken") or 0)
        chunk = ids[token * size:(token + 1) * size]
        nxt = token + 1
        result: dict[str, Any] = {"messages": [{"id": m, "threadId": "t"} for m in chunk]}
        if nxt * size < len(ids):
            result["nextPageToken"] = str(nxt)
        return result


class _Messages:
    def __init__(self, state: _State):
        self._state = state

    def list(self, **kwargs):
        state = self._state

        def _outcomes():
            state.calls.append(("messages.list", dict(kwargs)))  # one per ATTEMPT
            if state.list_errors:
                raise state.list_errors.pop(0)
            return state._page(kwargs)

        return _Request(fn=_outcomes)

    def get(self, userId, id, format="full"):  # noqa: N803, A002
        self._state.calls.append(("messages.get", {"id": id, "format": format}))
        script = self._state.get_errors.get(id)
        if script:
            return _Request(error=script.pop(0))
        msg = self._state.messages.get(id)
        if msg is None:
            return _Request(error=_HttpError(404, b"Requested entity was not found"))
        return _Request(result=msg)


class _Users:
    def __init__(self, state: _State):
        self._state = state

    def messages(self):
        return _Messages(self._state)

    def getProfile(self, userId):  # noqa: N803
        self._state.calls.append(("getProfile", {}))
        return _Request(result={"historyId": self._state.profile_history_id})


class _Service:
    def __init__(self, state: _State):
        self._state = state

    def users(self):
        return _Users(self._state)


def _client(state: _State, **over) -> RestGmailClient:
    return RestGmailClient(service=_Service(state), **over)


def _connector(state: _State, **over) -> GmailConnector:
    min_interval = over.pop("min_interval_s", 0.0)
    settings_kwargs: dict[str, Any] = dict(
        gmail_label_id=LABEL,
        gmail_query="",
        gmail_credentials_path="",
        _env_file=None,
    )
    fields = getattr(Settings, "model_fields", {}) or {}
    if "gmail_min_call_interval_s" in fields:
        settings_kwargs["gmail_min_call_interval_s"] = min_interval
    return GmailConnector(
        token_loader=lambda: "refresh-token",
        service_factory=lambda token, scopes, api_name, api_version: _Service(state),
        settings=Settings(**settings_kwargs),
        client_overrides={
            key: value
            for key, value in over.items()
            if key in ("sleep", "clock", "max_retries")
        },
    )


def _label_of(n: int) -> _State:
    state = _State()
    state.all_ids = [f"m{i:03d}" for i in range(n)]
    state.messages = {mid: _full_message(mid) for mid in state.all_ids}
    return state


class TestBoundedListing:
    def test_sync_limit_20_issues_one_list_call_capped_by_max_results(self):
        """--limit 20 over a 201-message label: ONE list call carrying
        maxResults (the old code listed the whole label, 3 pages, no cap)."""
        state = _label_of(201)
        connector = _connector(state)
        result = connector.fetch(None, limit=20)

        list_calls = [c for c in state.calls if c[0] == "messages.list"]
        assert len(list_calls) == 1, list_calls
        assert int(list_calls[0][1]["maxResults"]) <= 100
        assert len(result.records) == 20

    def test_query_batch_pages_lazily_until_the_limit_is_met(self):
        """A page of 50 with only 10 unknown: the walk fetches the NEXT page
        only because it still needs records — and stops the moment it has
        enough (the fast-path ledger skips cost no GET)."""
        state = _label_of(201)
        connector = _connector(state, is_known=lambda mid: mid < "m150")
        result = connector.fetch(None, limit=20)

        gets = [c for c in state.calls if c[0] == "messages.get"]
        assert len(gets) == 20
        list_calls = [c for c in state.calls if c[0] == "messages.list"]
        assert 1 <= len(list_calls) <= 4  # bounded by the label's pages, not unbounded
        assert len(result.records) == 20
        assert result.next_cursor is None  # truncated -> no cursor

    def test_full_resync_of_a_small_label_still_advances_the_cursor(self):
        """Exactly-limit records consumed the whole listing: NOT truncated —
        the historyId is a safe resume point (old semantics preserved)."""
        state = _label_of(20)
        connector = _connector(state)
        result = connector.fetch(None, limit=20)

        assert len(result.records) == 20
        assert result.next_cursor == "9002"
        list_calls = [c for c in state.calls if c[0] == "messages.list"]
        assert len(list_calls) == 1


class TestPacing:
    def test_consecutive_calls_are_paced_by_the_min_interval(self):
        state = _label_of(3)
        sleeps: list[float] = []
        client = _client(
            state,
            min_interval_s=0.25,
            sleep=sleeps.append,
            clock=lambda: 0.0,  # frozen clock: every pace fires
        )
        client.list_messages(query="", label_ids=[LABEL])
        client.get_message("m000")
        client.get_message("m001")

        # the 2nd and 3rd calls each waited one interval; the 1st never waits
        assert len(sleeps) == 2
        assert all(s == pytest.approx(0.25) for s in sleeps)

    def test_pacing_never_sleeps_below_a_zero_interval(self):
        state = _label_of(2)
        sleeps: list[float] = []
        client = _client(state, min_interval_s=0.0, sleep=sleeps.append, clock=lambda: 0.0)
        client.list_messages(query="", label_ids=[LABEL])
        client.get_message("m000")
        assert sleeps == []

    def test_settings_default_keeps_the_client_well_under_the_budget(self):
        """Gmail's per-user budget is 100 units/minute; ~5 units per call.
        The default interval must keep a single client at <= 12 calls/minute."""
        assert Settings(gmail_credentials_path="", _env_file=None).gmail_min_call_interval_s >= 5.0

    def test_connector_wires_the_configured_interval_into_its_client(self):
        state = _label_of(2)
        connector = _connector(state, min_interval_s=7.5)
        connector.fetch(None, limit=1)
        assert connector._gmail()._min_interval_s == 7.5


class TestRateLimitRetry:
    def test_rate_limited_403_is_retried_with_backoff_and_succeeds(self):
        state = _label_of(2)
        state.list_errors = [
            _HttpError(403, _rate_limited_content()),
            _HttpError(403, _rate_limited_content()),
        ]
        sleeps: list[float] = []
        client = _client(state, sleep=sleeps.append, clock=lambda: 0.0, min_interval_s=0.0)

        stubs = client.list_messages(query="", label_ids=[LABEL])
        assert [s.id for s in stubs] == ["m000", "m001"]
        list_calls = [c for c in state.calls if c[0] == "messages.list"]
        assert len(list_calls) == 3  # 2 throttled + 1 success
        assert sleeps == [2.0, 4.0]  # exponential backoff

    def test_retry_after_header_wins_over_the_backoff(self):
        state = _label_of(2)
        state.list_errors = [
            _HttpError(403, _rate_limited_content(), headers={"Retry-After": "7"}),
        ]
        sleeps: list[float] = []
        client = _client(state, sleep=sleeps.append, clock=lambda: 0.0, min_interval_s=0.0)

        client.list_messages(query="", label_ids=[LABEL])
        assert sleeps == [7.0]

    def test_429_is_retried_too(self):
        state = _label_of(2)
        state.list_errors = [_HttpError(429, b"rate limit")]
        sleeps: list[float] = []
        client = _client(state, sleep=sleeps.append, clock=lambda: 0.0, min_interval_s=0.0)
        client.list_messages(query="", label_ids=[LABEL])
        assert len([c for c in state.calls if c[0] == "messages.list"]) == 2

    def test_a_plain_403_is_never_retried(self):
        state = _label_of(2)
        state.list_errors = [_HttpError(403, b'{"error": {"errors": [{"reason": "forbidden"}]}}')]
        sleeps: list[float] = []
        client = _client(state, sleep=sleeps.append, clock=lambda: 0.0, min_interval_s=0.0)

        with pytest.raises(Exception) as excinfo:
            client.list_messages(query="", label_ids=[LABEL])
        assert len(state.calls) == 1  # no retry attempts
        assert sleeps == []
        assert getattr(excinfo.value, "status", None) == 403

    def test_retry_exhaustion_raises_after_the_last_attempt(self):
        state = _label_of(2)
        state.list_errors = [
            _HttpError(403, _rate_limited_content()) for _ in range(10)
        ]
        sleeps: list[float] = []
        client = _client(
            state, sleep=sleeps.append, clock=lambda: 0.0,
            min_interval_s=0.0, max_retries=3,
        )

        with pytest.raises(Exception) as excinfo:
            client.list_messages(query="", label_ids=[LABEL])
        list_calls = [c for c in state.calls if c[0] == "messages.list"]
        assert len(list_calls) == 4  # 1 + max_retries
        assert len(sleeps) == 3
        assert getattr(excinfo.value, "status", None) == 403


class TestDryRunBudget:
    def test_adopt_dry_run_gets_are_throttled_not_burst(self):
        """The adopt path drains with limit=200: with pacing, its GETs
        spread over >= (n-1) * interval seconds instead of bursting, and
        the fetch respects the limit (the tail is the next run's work)."""
        state = _label_of(201)
        sleeps: list[float] = []
        connector = _connector(
            state, min_interval_s=0.5, sleep=sleeps.append,
        )
        connector.fetch(None, limit=200)

        gets = [c for c in state.calls if c[0] == "messages.get"]
        assert len(gets) == 200  # the limit, not the whole label
        assert len(sleeps) >= 199  # one pace per call after the first

    def test_pacing_failure_never_breaks_the_fetch(self):
        """A pacing clock that misbehaves must not kill a sync (the throttle
        is hygiene, not correctness)."""
        state = _label_of(1)
        client = _client(
            state,
            min_interval_s=0.25,
            sleep=lambda _s: (_ for _ in ()).throw(RuntimeError("timer broke")),
            clock=lambda: 0.0,
        )
        stubs = client.list_messages(query="", label_ids=[LABEL])
        assert [s.id for s in stubs] == ["m000"]
