"""Health endpoint tests: all components, fakes for degraded scenarios.

Health check adapters are injectable (`HealthContext`); these tests drive
/ops/health and /ops/probe with fakes so no real Qdrant/LLM is needed.
"""

import pytest
from sqlalchemy import text

from brain.db.session import engine
from brain.ops import health as ops_health


def _healthy_ctx() -> ops_health.HealthContext:
    return ops_health.HealthContext(
        db=lambda: {"reachable": True, "latency_ms": 1.0, "drift": False,
                    "items_by_status": {"inbox": 3}, "size": "8103 kB"},
        qdrant=lambda: {"reachable": True, "collection": "brain_items",
                        "vectors": 3, "items": 3, "dims_ok": True},
        embedder=lambda: {"provider": "fastembed", "model": "BAAI/bge-small-en-v1.5",
                          "loaded": True},
        jev=lambda: {"configured": False},
        llm=lambda: {"provider": "claude_sdk", "model": None},
        telegram=lambda: {"token_set": True, "allowlist_set": True, "heartbeat_age_s": 4.0},
        docker=lambda: {"available": True, "services": {"qdrant": "healthy"}},
        disk=lambda: {"free_gb": 42.0, "fastembed_free_gb": 0.2},
    )


@pytest.fixture(name="ops_tables")
def ops_tables_fixture(database: None):
    ops_health.reset_cache()
    """ops tables exist (migrated); clean them around the test."""
    url = str(engine.url)
    assert url.rsplit("/", 1)[-1].endswith("_test"), f"not a test db: {url}"
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM ops_events"))
        conn.execute(text("DELETE FROM ops_heartbeats"))
    yield
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM ops_events"))
        conn.execute(text("DELETE FROM ops_heartbeats"))


class TestHealthEndpoint:
    def test_returns_every_component_with_status(self, client, ops_tables):
        monkeypatch_ctx = _healthy_ctx()
        ops_health._default_context = lambda: monkeypatch_ctx  # type: ignore[assignment]
        body = client.get("/ops/health").json()
        expected = {"api", "postgres", "qdrant", "embedder", "jev",
                    "llm", "telegram", "docker", "background", "disk"}
        assert expected <= set(body["components"].keys())
        for component in expected:
            entry = body["components"][component]
            assert entry["status"] in {"ok", "degraded", "down", "disabled"}
            assert "summary" in entry and "checked_at" in entry

    def test_qdrant_down_marks_overall_degraded(self, client, ops_tables):
        ctx = _healthy_ctx()
        ctx.qdrant = lambda: {"reachable": False}
        ops_health._default_context = lambda: ctx  # type: ignore[assignment]
        body = client.get("/ops/health").json()
        assert body["components"]["qdrant"]["status"] == "down"
        assert body["overall"] in {"degraded", "down"}

    def test_alembic_drift_is_degraded(self, client, ops_tables):
        ctx = _healthy_ctx()
        ctx.db = lambda: {"reachable": True, "latency_ms": 1.0, "drift": True,
                          "items_by_status": {}, "size": "1 kB"}
        ops_health._default_context = lambda: ctx  # type: ignore[assignment]
        body = client.get("/ops/health").json()
        assert body["components"]["postgres"]["status"] == "degraded"

    def test_stale_telegram_heartbeat_is_degraded(self, client, ops_tables):
        ctx = _healthy_ctx()
        ctx.telegram = lambda: {
            "token_set": True, "allowlist_set": True, "heartbeat_age_s": 400.0,
        }
        ops_health._default_context = lambda: ctx  # type: ignore[assignment]
        body = client.get("/ops/health").json()
        assert body["components"]["telegram"]["status"] == "degraded"

    def test_unconfigured_jev_is_disabled_not_down(self, client, ops_tables):
        ctx = _healthy_ctx()
        ctx.jev = lambda: {"configured": False}
        ops_health._default_context = lambda: ctx  # type: ignore[assignment]
        body = client.get("/ops/health").json()
        assert body["components"]["jev"]["status"] == "disabled"


class TestProbes:
    def test_probe_not_triggered_by_get_health(self, client, ops_tables, monkeypatch):
        calls: list[str] = []
        ctx = _healthy_ctx()
        ops_health._default_context = lambda: ctx  # type: ignore[assignment]
        monkeypatch.setattr(
            ops_health, "run_probe", lambda component, ctx=None: calls.append(component) or {}
        )
        client.get("/ops/health")
        assert calls == []  # expensive probes never run on auto-refresh

    def test_llm_probe_runs_on_demand(self, client, ops_tables, monkeypatch):
        monkeypatch.setattr(
            ops_health, "run_probe",
            lambda component, ctx=None: {
                "ok": True, "model": "claude-haiku-5-5", "latency_ms": 120.0,
            },
        )
        resp = client.post("/ops/probe/llm")
        assert resp.status_code == 200
        assert resp.json()["model"] == "claude-haiku-5-5"

    def test_unknown_component_probe_is_404(self, client, ops_tables):
        assert client.post("/ops/probe/nope").status_code == 404


class TestSecrets:
    def test_no_secret_values_in_any_ops_response(self, client, ops_tables, monkeypatch):
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "super-secret-token-value")
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-full-key")
        from brain.config import get_settings

        get_settings.cache_clear()
        ctx = _healthy_ctx()
        ops_health._default_context = lambda: ctx  # type: ignore[assignment]
        try:
            for path in ("/ops/health", "/ops/flow", "/ops/events"):
                body = client.get(path).text
                assert "super-secret-token-value" not in body
                assert "sk-ant-full-key" not in body
            probe = client.post("/ops/probe/llm")
            assert "super-secret-token-value" not in probe.text
        finally:
            get_settings.cache_clear()
