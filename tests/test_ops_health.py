"""Health endpoint tests: all components, fakes for degraded scenarios.

Health check adapters are injectable (`HealthContext`); these tests drive
/ops/health and /ops/probe with fakes so no real Qdrant/LLM is needed.
"""

import pytest
from sqlalchemy import text

from brain import ops
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
        whatsapp=lambda: {"configured": True, "interval_s": 30.0,
                          "heartbeat_age_s": 5.0, "last_poll_age_s": 5.0,
                          "channels": 2},
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
                    "llm", "telegram", "docker", "background", "disk", "whatsapp"}
        assert expected <= set(body["components"].keys())
        for component in expected:
            entry = body["components"][component]
            assert entry["status"] in {"ok", "degraded", "down", "disabled", "not probed"}
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

    def test_probe_records_an_ops_event(self, client, ops_tables, monkeypatch):
        """A probe is evidence: it must land in ops_events (proves the component)."""
        from brain.ai.ask import Completion
        from brain.api import deps

        class FakeLLM:
            def complete(self, prompt: str) -> Completion:
                return Completion(text="OK", model="fake")

        monkeypatch.setattr(deps, "get_llm_client", lambda: FakeLLM())
        client.post("/ops/probe/llm")
        recent = ops.recent_event("llm", ok=True)
        assert recent is not None and recent["kind"] == "probe"


class TestNotProbed:
    """Config-only components show "not probed" until a probe or real traffic."""

    def test_llm_without_traffic_or_probe_is_not_probed(self, client, ops_tables, monkeypatch):
        ops_health.reset_cache()
        ops_health._default_context = lambda: _healthy_ctx()  # type: ignore[assignment]
        body = client.get("/ops/health").json()
        entry = body["components"]["llm"]
        assert entry["status"] == "not probed"
        assert "probe" in entry["summary"].lower()

    def test_llm_with_recent_failed_call_is_degraded(self, client, ops_tables, monkeypatch):
        with engine.begin() as conn:
            conn.execute(text(
                "INSERT INTO ops_events (component, kind, ok, edge, detail) "
                "VALUES ('ask', 'llm', false, 'ask->llm', '{}')"
            ))
        ops_health.reset_cache()
        ops_health._default_context = lambda: _healthy_ctx()  # type: ignore[assignment]
        body = client.get("/ops/health").json()
        assert body["components"]["llm"]["status"] == "degraded"

    def test_llm_with_recent_successful_traffic_is_ok(self, client, ops_tables, monkeypatch):
        with engine.begin() as conn:
            conn.execute(text(
                "INSERT INTO ops_events (component, kind, ok, edge, detail) "
                "VALUES ('ask', 'llm', true, 'ask->llm', '{}')"
            ))
        ops_health.reset_cache()
        ops_health._default_context = lambda: _healthy_ctx()  # type: ignore[assignment]
        body = client.get("/ops/health").json()
        assert body["components"]["llm"]["status"] == "ok"

    def test_embedder_not_loaded_is_not_probed(self, client, ops_tables, monkeypatch):
        ctx = _healthy_ctx()
        ctx.embedder = lambda: {"provider": "fastembed", "model": "BAAI/bge-small-en-v1.5",
                                "loaded": False}
        ops_health._default_context = lambda: ctx  # type: ignore[assignment]
        ops_health.reset_cache()
        body = client.get("/ops/health").json()
        assert body["components"]["embedder"]["status"] == "not probed"

    def test_embedder_with_recent_traffic_is_ok(self, client, ops_tables, monkeypatch):
        ctx = _healthy_ctx()
        ctx.embedder = lambda: {"provider": "fastembed", "model": "BAAI/bge-small-en-v1.5",
                                "loaded": False}
        with engine.begin() as conn:
            conn.execute(text(
                "INSERT INTO ops_events (component, kind, ok, edge, detail) "
                "VALUES ('search', 'embed', true, 'search->embedder', '{}')"
            ))
        ops_health._default_context = lambda: ctx  # type: ignore[assignment]
        ops_health.reset_cache()
        body = client.get("/ops/health").json()
        assert body["components"]["embedder"]["status"] == "ok"


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


class TestBackgroundComponent:
    """The cockpit's Background-services node: launchd agent states from
    `launchctl print` (scripts/service.sh install)."""

    @staticmethod
    def _ctx(background) -> ops_health.HealthContext:
        return ops_health.HealthContext(
            db=lambda: {"reachable": True, "latency_ms": 1.0, "drift": False,
                        "items_by_status": {}, "size": "1 kB"},
            qdrant=lambda: {"reachable": True, "collection_exists": True,
                            "vectors": 0, "items": 0, "dims_ok": True},
            embedder=lambda: {"provider": "fastembed", "model": None, "loaded": False},
            jev=lambda: {"configured": False},
            llm=lambda: {"provider": "claude_sdk", "model": None},
            telegram=lambda: {"token_set": False, "allowlist_set": False,
                              "heartbeat_age_s": None},
            docker=lambda: {"available": False},
            disk=lambda: {"free_gb": 42.0},
            background=background,
        )

    def test_disabled_when_launchd_unavailable(self):
        ops_health.reset_cache()
        body = ops_health.run_health_checks(self._ctx(lambda: {"available": False, "agents": {}}))
        node = body["components"]["background"]
        assert node["status"] == "disabled"
        assert "launchd unavailable" in node["summary"]

    def test_disabled_when_no_agents_installed(self):
        ops_health.reset_cache()
        body = ops_health.run_health_checks(self._ctx(lambda: {"available": True, "agents": {}}))
        node = body["components"]["background"]
        assert node["status"] == "disabled"
        assert "service.sh install" in node["summary"]

    def test_ok_when_both_agents_running(self):
        ops_health.reset_cache()
        agents = {
            "com.secondbrain.api": {"state": "running", "pid": 100},
            "com.secondbrain.telegram": {"state": "running", "pid": 101},
        }
        raw = lambda: {"available": True, "agents": agents}  # noqa: E731
        body = ops_health.run_health_checks(self._ctx(raw))
        node = body["components"]["background"]
        assert node["status"] == "ok"
        assert node["summary"] == "2/2 agents running"

    def test_degraded_when_one_agent_dead_with_exit_code(self):
        ops_health.reset_cache()
        agents = {
            "com.secondbrain.api": {"state": "running", "pid": 100},
            "com.secondbrain.telegram": {"state": "exited", "last_exit": 78},
        }
        raw = lambda: {"available": True, "agents": agents}  # noqa: E731
        body = ops_health.run_health_checks(self._ctx(raw))
        node = body["components"]["background"]
        assert node["status"] == "degraded"
        assert "1/2 agents running" in node["summary"]
        assert "telegram: exited (exit 78)" in node["summary"]

    def test_down_when_installed_but_none_running(self):
        ops_health.reset_cache()
        agents = {
            "com.secondbrain.api": {"state": "exited", "last_exit": 1},
            "com.secondbrain.telegram": {"state": "not installed"},
        }
        raw = lambda: {"available": True, "agents": agents}  # noqa: E731
        body = ops_health.run_health_checks(self._ctx(raw))
        node = body["components"]["background"]
        assert node["status"] == "down"
        assert "0/2 agents running" in node["summary"]

    def test_default_adapter_parses_launchctl_print(self, monkeypatch, tmp_path):
        """The default adapter shells out to `launchctl print gui/<uid>/<label>`;
        here a PATH stub keeps the test hermetic."""
        import os

        stub = tmp_path / "stubbin"
        stub.mkdir()
        (stub / "launchctl").write_text(
            "#!/usr/bin/env bash\n"
            'case "$2" in\n'
            "  *com.secondbrain.api) printf '\tstate = running\\n\tpid = 4242\\n"
            "\tlast exit code = 0\\n';;\n"
            '  *) echo "Could not find service" >&2; exit 3;;\n'
            "esac\n"
        )
        (stub / "launchctl").chmod(0o755)
        monkeypatch.setenv("PATH", f"{stub}:{os.environ['PATH']}")
        ops_health.reset_cache()
        body = ops_health.run_health_checks(ops_health.HealthContext())
        raw = body["components"]["background"]
        assert raw["status"] == "ok"  # api running; telegram not installed => not counted
        assert raw["summary"] == "1/1 agents running"
        assert raw["details"]["agents"]["com.secondbrain.api"]["pid"] == 4242
        assert raw["details"]["agents"]["com.secondbrain.telegram"] == {}

    def test_adapter_uses_top_level_state_not_nested_entries(self, monkeypatch, tmp_path):
        """Real `launchctl print` output contains NESTED 'state = active' lines
        (events/sockets sub-dicts) after the top-level 'state = running'. The
        parser must read the top-level (one-tab) keys only — the last nested
        line previously overwrote the state and a healthy agent read as
        'active' (=> background 'down 0/2' while both agents run)."""
        import os

        stub = tmp_path / "stubbin2"
        stub.mkdir()
        real_like = (
            "gui/501/com.secondbrain.api = {\n"
            "\tactive count = 1\n"
            "\tpath = /Users/x/Library/LaunchAgents/com.secondbrain.api.plist\n"
            "\ttype = LaunchAgent\n"
            "\tstate = running\n"
            "\tprogram = /Users/x/git/Second-brain/scripts/run-server.sh\n"
            "\tevents = {\n"
            "\t\tstate = active\n"
            "\t}\n"
            "\tsockets = {\n"
            "\t\tstate = active\n"
            "\t}\n"
            "\tpid = 93202\n"
            "\tlast exit code = (never exited)\n"
            "}\n"
        )
        (stub / "launchctl").write_text(
            "#!/usr/bin/env bash\n"
            'case "$2" in\n'
            f"  *com.secondbrain.api) printf '{real_like}';;\n"
            "  *) exit 3;;\n"
            "esac\n"
        )
        (stub / "launchctl").chmod(0o755)
        monkeypatch.setenv("PATH", f"{stub}:{os.environ['PATH']}")
        ops_health.reset_cache()
        body = ops_health.run_health_checks(ops_health.HealthContext())
        raw = body["components"]["background"]
        assert raw["status"] == "ok"
        agent = raw["details"]["agents"]["com.secondbrain.api"]
        assert agent["state"] == "running"
        assert agent["pid"] == 93202
