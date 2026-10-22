"""The flow graph — ONE declarative definition shared by /ops/flow and the page.

The page (src/brain/cockpit/app.js) renders this as an inline SVG; desktop
layout uses the ``column``/``row`` hints, phone width re-lays-out the same
nodes/edges vertically. Edge health comes from ops_events rows carrying the
hop name (see the instrumentation in services/).
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import text

from brain.db.session import engine
from brain.ops import health as ops_health

_WINDOW_MINUTES = 15
_ACTIVE_SECONDS = 60

# Component → node mapping: a node's status mirrors its component's health.
NODES: list[dict[str, Any]] = [
    # Inputs (left)
    {"id": "telegram", "label": "Telegram", "column": 0, "row": 0, "component": "telegram"},
    {"id": "api", "label": "API /items,/capture", "column": 0, "row": 1, "component": "api"},
    {"id": "search", "label": "Search", "column": 0, "row": 2, "component": "api"},
    {"id": "ask", "label": "Ask", "column": 0, "row": 3, "component": "llm"},
    # Processing (middle)
    {"id": "telegram_bot", "label": "Telegram bot", "column": 1, "row": 0, "component": "telegram"},
    {"id": "ingest", "label": "Ingest", "column": 1, "row": 1, "component": "api"},
    {"id": "parser", "label": "Paste parser", "column": 2, "row": 0, "component": "api"},
    {"id": "embedder", "label": "Embedder", "column": 2, "row": 1, "component": "embedder"},
    {"id": "jev", "label": "Jev classify", "column": 2, "row": 2, "component": "jev"},
    # Stores / AI (right)
    {"id": "postgres", "label": "Postgres", "column": 4, "row": 0, "component": "postgres"},
    {"id": "qdrant", "label": "Qdrant", "column": 4, "row": 1, "component": "qdrant"},
    {"id": "rerank", "label": "Jev rerank", "column": 4, "row": 2, "component": "jev"},
    {"id": "llm", "label": "LLM", "column": 2, "row": 3, "component": "llm"},
    # Infrastructure row (underneath)
    {"id": "docker", "label": "Docker", "column": 0, "row": 4, "component": "docker"},
    {
        "id": "background", "label": "Background services",
        "column": 1, "row": 4, "component": "background",
    },
    {"id": "disk", "label": "Disk", "column": 2, "row": 4, "component": "disk"},
]

EDGES: list[dict[str, str]] = [
    {"from": "telegram", "to": "telegram_bot", "hop": "telegram->bot"},
    {"from": "telegram_bot", "to": "parser", "hop": "telegram->parser"},
    {"from": "parser", "to": "ingest", "hop": "parser->ingest"},
    {"from": "api", "to": "ingest", "hop": "api->ingest"},
    {"from": "ingest", "to": "jev", "hop": "ingest->jev"},
    {"from": "ingest", "to": "postgres", "hop": "ingest->postgres"},
    {"from": "ingest", "to": "embedder", "hop": "ingest->embedder"},
    {"from": "embedder", "to": "qdrant", "hop": "embedder->qdrant"},
    {"from": "search", "to": "embedder", "hop": "search->embedder"},
    {"from": "search", "to": "qdrant", "hop": "search->qdrant"},
    {"from": "qdrant", "to": "rerank", "hop": "search->rerank"},
    {"from": "ask", "to": "search", "hop": "ask->search"},
    {"from": "ask", "to": "llm", "hop": "ask->llm"},
]

def flow_payload() -> dict[str, Any]:
    """Nodes + edges with live status, summaries and per-hop stats."""
    health = ops_health.run_health_checks()
    nodes = [_node_payload(node, health) for node in NODES]
    status_by_id = {node["id"]: node["status"] for node in nodes}
    edges = [_edge_payload(edge, status_by_id) for edge in EDGES]
    degraded = sorted(
        node["id"] for node in nodes if node["status"] in {"degraded", "down"}
    )
    return {
        "nodes": nodes,
        "edges": edges,
        "overall": health.get("overall", "ok"),
        "degraded": degraded,
    }


def _node_status(node: dict[str, Any], health: dict[str, Any]) -> str:
    entry = health.get("components", {}).get(node["component"], {})
    return str(entry.get("status", "down"))


def _node_payload(node: dict[str, Any], health: dict[str, Any]) -> dict[str, Any]:
    component_health = health.get("components", {}).get(node["component"], {})
    hop_p50s = [
        stats["p50"]
        for edge in EDGES
        if node["id"] in (edge["from"], edge["to"])
        and (stats := _edge_stats(edge["hop"])) and isinstance(stats.get("p50"), (int, float))
    ]
    return {
        "id": node["id"],
        "label": node["label"],
        "column": node["column"],
        "row": node["row"],
        "component": node["component"],
        "status": _node_status(node, health),
        "summary": component_health.get("summary", ""),
        "latency_ms": round(min(hop_p50s), 1) if hop_p50s else None,
    }


def _edge_payload(edge: dict[str, str], status_by_id: dict[str, str]) -> dict[str, Any]:
    stats = _edge_stats(edge["hop"])
    target_status = status_by_id.get(edge["to"])
    if target_status == "down":
        status = "red"
    elif stats["count"] == 0:
        status = "grey"
    elif stats["error_rate"] >= 0.2:
        status = "red"
    elif stats["error_rate"] > 0:
        status = "amber"
    else:
        status = "green"
    return {
        "from": edge["from"],
        "to": edge["to"],
        "hop": edge["hop"],
        "status": status,
        "count": stats["count"],
        "p50": stats["p50"],
        "p95": stats["p95"],
        "error_rate": stats["error_rate"],
        "last_error": stats["last_error"],
        "active": _edge_active(edge["hop"]),
    }


def edge_status(hop: str, health: dict[str, Any] | None = None) -> tuple[str, int]:
    """(status, count) for one hop — the spec's edge-color math."""
    stats = _edge_stats(hop)
    count = stats["count"]
    # Accept both {"components": {...}} (flow_payload shape) and flat
    # {component: {"status": ...}} maps.
    components = health.get("components", health) if isinstance(health, dict) else {}
    target = hop.rsplit("->", 1)[-1]
    component = next(
        (node["component"] for node in NODES if node["id"] == target), None
    )
    if component and components.get(component, {}).get("status") == "down":
        return "red", count
    if count == 0:
        return "grey", 0
    if stats["error_rate"] >= 0.2:
        return "red", count
    if stats["error_rate"] > 0:
        return "amber", count
    return "green", count


def _edge_stats(hop: str) -> dict[str, Any]:
    """Per-hop stats over the window; one query, never raises."""
    try:
        with engine.connect() as conn:
            row = conn.execute(
                text(
                    "SELECT count(*) AS count, "
                    "avg(CASE WHEN ok THEN 0 ELSE 1 END)::float AS error_rate, "
                    "percentile_cont(0.5) WITHIN GROUP (ORDER BY latency_ms) AS p50, "
                    "percentile_cont(0.95) WITHIN GROUP (ORDER BY latency_ms) AS p95 "
                    "FROM ops_events WHERE edge = :e "
                    "AND ts > now() - make_interval(mins => :m)"
                ),
                {"e": hop, "m": _WINDOW_MINUTES},
            ).mappings().one()
        last_error = None
        if row["error_rate"]:
            last_error = _edge_last_error(hop)
        return {
            "count": int(row["count"]),
            "error_rate": round(float(row["error_rate"] or 0), 4),
            "p50": _round(row["p50"]),
            "p95": _round(row["p95"]),
            "last_error": last_error,
        }
    except Exception:  # noqa: BLE001  (no DB → idle edges; never raise)
        return {"count": 0, "error_rate": 0.0, "p50": None, "p95": None,
                "last_error": None}


def _edge_last_error(hop: str) -> str | None:
    try:
        with engine.connect() as conn:
            return conn.execute(
                text(
                    "SELECT detail->>'error' FROM ops_events "
                    "WHERE edge = :e AND ok = false AND ts > now() - make_interval(mins => :m) "
                    "ORDER BY ts DESC LIMIT 1"
                ),
                {"e": hop, "m": _WINDOW_MINUTES},
            ).scalar_one_or_none()
    except Exception:  # noqa: BLE001
        return None


def _edge_active(hop: str) -> bool:
    """Traffic in the last 60 seconds (drives the animated dash)."""
    try:
        with engine.connect() as conn:
            row = conn.execute(
                text(
                    "SELECT count(*) FROM ops_events WHERE edge = :e "
                    "AND ts > now() - make_interval(secs => :s)"
                ),
                {"e": hop, "s": _ACTIVE_SECONDS},
            ).scalar_one()
        return int(row) > 0
    except Exception:  # noqa: BLE001
        return False


def _round(value: Any) -> float | None:
    return round(float(value), 1) if value is not None else None
