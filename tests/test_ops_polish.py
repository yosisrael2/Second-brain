"""Ops polish (telegram flap, cockpit cards, fake-LLM diagnostics).

1. Telegram heartbeat flap: the bot long-polls up to poll_timeout_s (50s
   default) and only heartbeats on SUCCESS, so a quiet chat or a retry loop
   legitimately pushes the heartbeat age past the old fixed 60s threshold.
   The threshold now follows the bot's actual getUpdates timeout
   (settings.telegram_poll_timeout_s + margin), and the bot heartbeats on
   the retry paths too (honest liveness, tested in test_telegram_bot.py).
2. /ops/health gains read-only 'attachments' and 'enrichment' components
   (the cockpit renders every component automatically).
3. AskService.ask(record_ops=False) runs the whole ask path WITHOUT recording
   ops spans/events — for diagnostics runs with a fake LLM, so /ops/health's
   llm 'last call failed' is not poisoned until the next real call.
"""

import pytest
from sqlalchemy import text as sa_text

from brain import ops
from brain.ops import health as ops_health


@pytest.fixture(name="ops_tables")
def ops_tables_fixture(database: None):
    """ops tables exist; clean them around the test (same as test_ops_health)."""
    ops_health.reset_cache()
    with ops._get_engine().begin() as conn:
        conn.execute(sa_text("DELETE FROM ops_events"))
        conn.execute(sa_text("DELETE FROM ops_heartbeats"))
    yield
    with ops._get_engine().begin() as conn:
        conn.execute(sa_text("DELETE FROM ops_events"))
        conn.execute(sa_text("DELETE FROM ops_heartbeats"))


def _telegram_raw(age: float, *, poll_timeout_s: int = 50) -> dict:
    return {
        "token_set": True,
        "allowlist_set": True,
        "heartbeat_age_s": age,
        "poll_timeout_s": poll_timeout_s,
        "degraded_after_s": poll_timeout_s + 30,
    }


class TestTelegramThresholdFollowsPollTimeout:
    def test_quiet_poll_within_timeout_is_ok(self):
        entry = ops_health._derive_status("telegram", _telegram_raw(49.0))
        assert entry["status"] == "ok"

    def test_age_just_under_threshold_is_ok(self):
        entry = ops_health._derive_status("telegram", _telegram_raw(79.0))
        assert entry["status"] == "ok"

    def test_age_just_over_threshold_is_degraded(self):
        entry = ops_health._derive_status("telegram", _telegram_raw(81.0))
        assert entry["status"] == "degraded"

    def test_age_over_1800_is_down(self):
        entry = ops_health._derive_status("telegram", _telegram_raw(1801.0))
        assert entry["status"] == "down"

    def test_check_derives_threshold_from_settings(self, monkeypatch):
        """_telegram_check reads settings.telegram_poll_timeout_s (50 -> 80)."""
        from brain.config import Settings

        monkeypatch.setattr(
            ops_health,
            "get_settings",
            lambda: Settings(
                telegram_bot_token="1234:abc",
                telegram_allowed_user_ids="1",
                telegram_poll_timeout_s=50,
                _env_file=None,
            ),
        )
        raw = ops_health._telegram_check()
        assert raw["poll_timeout_s"] == 50
        assert raw["degraded_after_s"] == 80


class TestAttachmentsComponent:
    def test_present_with_queue_budget_and_read_today(self, client, ops_tables):
        body = client.get("/ops/health").json()
        att = body["components"]["attachments"]
        assert att["status"] == "ok"
        assert att["details"]["queue_depth"] == 0
        assert att["details"]["read_today"] == 0
        assert att["details"]["daily_budget"] == 50

    def test_last_error_surfaced_in_details(self, client, ops_tables):
        ops.record_event(
            "attachments",
            "read_error",
            ok=False,
            detail={"error": "claude sdk timed out after 90s"},
        )
        body = client.get("/ops/health").json()
        details = body["components"]["attachments"]["details"]
        assert details["last_error"]["kind"] == "read_error"


class TestEnrichmentComponent:
    def test_worker_off_is_ok_but_stated(self, client, ops_tables):
        body = client.get("/ops/health").json()
        enr = body["components"]["enrichment"]
        assert enr["status"] == "ok"
        assert enr["details"]["worker_enabled"] is False
        assert enr["details"]["queue_depth"] == 0
        assert enr["details"]["cap_used_today"] == 0
        assert enr["details"]["cap_limit"] == 100

    def test_worker_on_when_enabled(self, client, ops_tables, monkeypatch):
        from brain.config import Settings

        monkeypatch.setattr(
            ops_health,
            "get_settings",
            lambda: Settings(enrichment_worker_enabled=True, _env_file=None),
        )
        body = client.get("/ops/health").json()
        assert body["components"]["enrichment"]["details"]["worker_enabled"] is True


class TestAskRecordOpsFlag:
    def _ask(self, record_ops: bool):
        from tests.fakes import FakeRepository
        from tests.test_ask_service import StubSearch, cite_every_source, make_hits

        from brain.ai.ask import FakeLLMClient
        from brain.services.ask import AskService

        repo = FakeRepository()
        hits = make_hits(repo, "The server runs on port 27800.")
        service = AskService(StubSearch(hits), FakeLLMClient(reply=cite_every_source))  # type: ignore[arg-type]
        return service, service.ask("what port?", record_ops=record_ops)

    def test_record_ops_false_records_nothing(self, ops_tables):
        result = self._ask(record_ops=False)[1]
        assert result.answer  # the answer still works
        with ops._get_engine().connect() as conn:
            rows = conn.execute(
                sa_text(
                    "SELECT count(*) FROM ops_events WHERE component IN"
                    " ('ask', 'search', 'llm')"
                )
            ).scalar_one()
        assert rows == 0, "diagnostics must not poison /ops/health's llm view"

    def test_default_still_records(self, ops_tables):
        self._ask(record_ops=True)[1]
        with ops._get_engine().connect() as conn:
            rows = conn.execute(
                sa_text("SELECT count(*) FROM ops_events WHERE component = 'ask'")
            ).scalar_one()
        assert rows >= 1  # the ask->search and ask->llm spans
