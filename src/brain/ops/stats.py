"""Event/item statistics for the Stats tab (24h/7d, percentiles, histogram)."""

from __future__ import annotations

from typing import Any

from sqlalchemy import text

from brain.db.session import engine


def event_stats() -> dict[str, Any]:
    """Per-component 24h/7d counts, error rates, p50/p95 + item stats."""
    components_24h = _component_rows(hours=24)
    components_7d = _component_rows(hours=24 * 7)
    merged = []
    for row in components_7d:
        day = next((c for c in components_24h if c["component"] == row["component"]), None)
        merged.append({**row, "count_24h": day["count"] if day else 0,
                       "error_rate_24h": day["error_rate"] if day else 0.0})
    return {
        "components": merged,
        "histogram": _confidence_histogram(),
        "status_shares": _status_shares(),
    }


def _component_rows(hours: float) -> list[dict[str, Any]]:
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT component, count(*) AS count, "
                "avg(CASE WHEN ok THEN 0 ELSE 1 END)::float AS error_rate, "
                "percentile_cont(0.5) WITHIN GROUP (ORDER BY latency_ms) AS p50, "
                "percentile_cont(0.95) WITHIN GROUP (ORDER BY latency_ms) AS p95 "
                "FROM ops_events WHERE ts > now() - make_interval(hours => :h) "
                "GROUP BY component ORDER BY count DESC"
            ),
            {"h": hours},
        ).mappings().all()
    return [
        {
            "component": row["component"],
            "count": int(row["count"]),
            "error_rate": round(float(row["error_rate"] or 0), 4),
            "p50": _round(row["p50"]),
            "p95": _round(row["p95"]),
        }
        for row in rows
    ]


def _round(value: Any) -> float | None:
    return round(float(value), 1) if value is not None else None


def _confidence_histogram() -> list[dict[str, Any]]:
    """Classification confidence distribution (10 buckets over [0, 1])."""
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT width_bucket(confidence, 0, 1, 10) AS bucket, count(*) AS count "
                "FROM memory_items WHERE confidence IS NOT NULL GROUP BY 1 ORDER BY 1"
            )
        ).all()
    buckets = [{"bucket": i, "range": f"{i / 10:.1f}-{(i + 1) / 10:.1f}", "count": 0}
               for i in range(10)]
    for bucket, count in ((int(r[0]), int(r[1])) for r in rows):
        if 1 <= bucket <= 10:
            buckets[bucket - 1]["count"] = count
    return buckets


def _status_shares() -> dict[str, int]:
    with engine.connect() as conn:
        rows = conn.execute(
            text("SELECT status, count(*) AS count FROM memory_items GROUP BY status")
        ).all()
    return {str(row[0]): int(row[1]) for row in rows}
