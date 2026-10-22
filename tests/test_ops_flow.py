"""Flow endpoint tests: graph definition, edge status math, node-down edges."""

import pytest
from sqlalchemy import text

from brain.db.session import engine
from brain.ops import flow as ops_flow
from brain.ops.flow import EDGES, NODES


@pytest.fixture(name="ops_clean")
def ops_clean_fixture(database: None):
    url = str(engine.url)
    assert url.rsplit("/", 1)[-1].endswith("_test"), f"not a test db: {url}"
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM ops_events"))
        conn.execute(text("DELETE FROM ops_heartbeats"))
    yield
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM ops_events"))
        conn.execute(text("DELETE FROM ops_heartbeats"))


def _seed_edge_events(edge: str, ok_count: int, fail_count: int, minutes_ago: int = 1) -> None:
    for _ in range(ok_count):
        with engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO ops_events (ts, component, kind, ok, latency_ms, edge, detail) "
                    "VALUES (now() - make_interval(mins => :m), 'x', 'call', true, 100, :e, '{}')"
                ),
                {"m": minutes_ago, "e": edge},
            )
    for _ in range(fail_count):
        with engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO ops_events (ts, component, kind, ok, latency_ms, edge, detail) "
                    "VALUES (now() - make_interval(mins => :m), 'x', 'call', false, 100, :e, "
                    "'{\"error\": \"VectorStoreError: down\"}')"
                ),
                {"m": minutes_ago, "e": edge},
            )


class TestGraphDefinition:
    def test_nodes_carry_layout_and_component(self):
        assert NODES, "graph must define nodes"
        ids = {node["id"] for node in NODES}
        assert {"telegram", "telegram_bot", "parser", "ingest", "api", "jev",
                "embedder", "qdrant", "postgres", "search", "ask", "rerank",
                "llm"} <= ids
        for node in NODES:
            assert "column" in node and "row" in node and "component" in node

    def test_edges_carry_hop_names(self):
        hops = {edge["hop"] for edge in EDGES}
        assert {"telegram->parser", "ingest->jev", "ingest->postgres",
                "ingest->embedder", "embedder->qdrant", "search->rerank",
                "ask->llm"} <= hops
        for edge in EDGES:
            assert edge["from"] in {n["id"] for n in NODES}
            assert edge["to"] in {n["id"] for n in NODES}


class TestEdgeStatusMath:
    def test_green_when_no_errors(self, ops_clean: None):
        _seed_edge_events("ingest->jev", ok_count=10, fail_count=0)
        assert ops_flow.edge_status("ingest->jev") == ("green", 10)

    def test_amber_when_some_errors(self, ops_clean: None):
        _seed_edge_events("ingest->jev", ok_count=9, fail_count=1)  # 10% errors
        status, _count = ops_flow.edge_status("ingest->jev")
        assert status == "amber"

    def test_red_when_mostly_failing(self, ops_clean: None):
        _seed_edge_events("ingest->jev", ok_count=1, fail_count=4)  # 80% errors
        status, _count = ops_flow.edge_status("ingest->jev")
        assert status == "red"

    def test_grey_idle_when_no_traffic(self, ops_clean: None):
        status, count = ops_flow.edge_status("ingest->jev")
        assert status == "grey" and count == 0

    def test_red_when_target_node_down(self, ops_clean: None, monkeypatch):
        _seed_edge_events("ingest->qdrant", ok_count=10, fail_count=0)
        health = {"qdrant": {"status": "down"}}
        status, _count = ops_flow.edge_status("ingest->qdrant", health=health)
        assert status == "red"


class TestFlowEndpoint:
    def test_flow_returns_every_node_and_edge(self, client, ops_clean):
        body = client.get("/ops/flow").json()
        node_ids = {node["id"] for node in body["nodes"]}
        assert {"ingest", "qdrant", "postgres"} <= node_ids
        returned_edges = {(edge["from"], edge["to"]) for edge in body["edges"]}
        assert {(e["from"], e["to"]) for e in EDGES} <= returned_edges
        for node in body["nodes"]:
            assert node["status"] in {"ok", "degraded", "down", "disabled"}
            assert "summary" in node
        for edge in body["edges"]:
            assert edge["status"] in {"green", "amber", "red", "grey"}
            assert "count" in edge and "p50" in edge and "error_rate" in edge
