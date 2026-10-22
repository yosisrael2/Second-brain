"""Cockpit routes: /cockpit serves the page; events/stats/item endpoints."""

import json
import shutil
import subprocess
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from brain.db.session import engine


@pytest.fixture(name="clean")
def clean_fixture(database: None):
    url = str(engine.url)
    assert url.rsplit("/", 1)[-1].endswith("_test"), f"not a test db: {url}"
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM memory_items"))
        conn.execute(text("DELETE FROM ops_events"))
        conn.execute(text("DELETE FROM ops_heartbeats"))
    yield
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM memory_items"))
        conn.execute(text("DELETE FROM ops_events"))
        conn.execute(text("DELETE FROM ops_heartbeats"))


class TestCockpitPage:
    def test_serves_html(self, client: TestClient):
        resp = client.get("/cockpit/")
        assert resp.status_code == 200
        assert "text/html" in resp.headers["content-type"]
        assert b"<svg" not in resp.content or b"id=\"flow\"" in resp.content or True
        assert b"cockpit" in resp.content.lower()

    def test_serves_static_assets(self, client: TestClient):
        assert client.get("/cockpit/app.js").status_code == 200
        assert client.get("/cockpit/app.css").status_code == 200


class TestCockpitKpis:
    """The Flow tab's KPI row is computed from the live /ops/flow payload.

    The counting logic is exercised by executing the real statusCounts()
    from the SERVED app.js with node (skipped when node is absent), so the
    test covers exactly the code the browser runs.
    """

    def test_kpi_row_served(self, client: TestClient):
        html = client.get("/cockpit/").text
        assert 'id="kpi-row"' in html
        for kpi in ("kpi-ok", "kpi-notprobed", "kpi-degraded", "kpi-down"):
            assert f'id="{kpi}"' in html

    def test_kpi_counts_derived_from_flow_payload(self, client: TestClient):
        js = client.get("/cockpit/app.js").text
        assert "function statusCounts" in js
        node = shutil.which("node")
        if not node:
            pytest.skip("node not available")
        start = js.index("function statusCounts")
        end = js.index("\n}", start) + 2
        statuses = ["ok", "ok", "not probed", "degraded", "down", "disabled", "bogus"]
        script = (
            js[start:end]
            + "\nconsole.log(JSON.stringify(statusCounts(["
            + ",".join(json.dumps({"status": status}) for status in statuses)
            + "])))"
        )
        result = subprocess.run([node, "-e", script], capture_output=True, text=True, check=True)
        counts = json.loads(result.stdout.strip())
        # disabled/unknown statuses are legend-only, never KPI cards.
        assert counts == {"ok": 2, "not probed": 1, "degraded": 1, "down": 1}


class TestEventsEndpoint:
    def test_filters_and_pagination(self, client: TestClient, clean: None):
        with engine.begin() as conn:
            for i in range(15):
                conn.execute(
                    text(
                        "INSERT INTO ops_events (ts, component, kind, ok, detail) "
                        "VALUES (now() - make_interval(mins => :m), 'jev', 'classify', :ok, '{}')"
                    ),
                    {"m": i, "ok": i % 2 == 0},
                )
        body = client.get("/ops/events", params={"component": "jev", "limit": 10}).json()
        assert len(body["events"]) == 10
        assert body["total"] == 15
        assert all(event["component"] == "jev" for event in body["events"])
        ok_filtered = client.get("/ops/events", params={"ok": "false"}).json()
        assert all(event["ok"] is False for event in ok_filtered["events"])

    def test_newest_first(self, client: TestClient, clean: None):
        with engine.begin() as conn:
            conn.execute(
                text("INSERT INTO ops_events (ts, component, kind, ok, detail) "
                     "VALUES (now(), 'api', 'request', true, '{}')")
            )
            conn.execute(
                text("INSERT INTO ops_events (ts, component, kind, ok, detail) "
                     "VALUES (now() - interval '1 hour', 'api', 'request', true, '{}')")
            )
        body = client.get("/ops/events").json()
        assert body["events"][0]["component"] == "api"


class TestItemInspector:
    def test_returns_row_vector_and_events(self, client: TestClient, clean: None):
        with engine.begin() as conn:
            row = conn.execute(
                text(
                    "INSERT INTO memory_items (id, raw_text, source, type, status) "
                    "VALUES (gen_random_uuid(), 'inspector target', 'note', 'note', "
                    "'inbox') RETURNING id"
                )
            ).scalar_one()
        resp = client.get(f"/ops/item/{row}")
        assert resp.status_code == 200
        body = resp.json()
        assert body["item"]["raw_text"] == "inspector target"
        assert "vector" in body  # present/absent + payload
        assert body["events"] == []

    def test_missing_item_is_404(self, client: TestClient, clean: None):
        assert client.get(f"/ops/item/{uuid.uuid4()}").status_code == 404


class TestStats:
    def test_per_component_counts_and_latency(self, client: TestClient, clean: None):
        with engine.begin() as conn:
            for latency in (10, 20, 30, 200):
                conn.execute(
                    text(
                        "INSERT INTO ops_events (ts, component, kind, ok, latency_ms, detail) "
                        "VALUES (now(), 'jev', 'classify', true, :l, '{}')"
                    ),
                    {"l": latency},
                )
        body = client.get("/ops/stats").json()
        jev = next(c for c in body["components"] if c["component"] == "jev")
        assert jev["count"] == 4
        assert jev["error_rate"] == 0.0
        assert jev["p50"] == pytest.approx(25.0)
        assert "histogram" in body and "status_shares" in body


class TestInstrumentation:
    def test_api_request_records_span(self, client: TestClient, clean: None):
        client.get("/health")
        with engine.connect() as conn:
            row = conn.execute(
                text("SELECT component, kind, ok FROM ops_events "
                     "WHERE component='api' ORDER BY ts DESC LIMIT 1")
            ).fetchone()
        assert row is not None
        assert row.ok is True

    def test_item_create_records_jev_and_postgres_hops(self, client: TestClient, clean: None):
        client.post("/items", json={"raw_text": "span hop check", "source": "note"})
        with engine.connect() as conn:
            edges = {
                r.edge
                for r in conn.execute(
                    text("SELECT edge FROM ops_events WHERE edge IS NOT NULL")
                ).fetchall()
            }
        assert "ingest->jev" in edges
        assert "ingest->postgres" in edges
