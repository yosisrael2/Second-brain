"""Session-level live-collection guard (PR #42 incident; refined 24 Oct).

The ORIGINAL guard compared the LIVE collection's point count before and
after the whole suite and failed on ANY change. That produced a false
positive whenever the live instance ingested a real note mid-suite
(seen 07:17Z 24 Oct: 108 -> 109, a real capture during a test run).

The guard now detects TEST-originated points, not raw count:

- every vector a TEST writes carries ``payload["test_origin"] = "pytest"``
  (tests/conftest.py patches the vector store's write chokepoint — no
  production code change);
- the session guard asserts the LIVE collection holds ZERO marker-carrying
  points (a marker there can only have come from a test);
- the raw count delta is INFORMATIONAL ONLY (external ingestion is normal).

Read-only (one REST GET + one count POST); ``None`` (qdrant unreachable)
never fails the run.
"""

import json
from urllib.request import Request, urlopen

LIVE_COLLECTION = "brain_items"
TEST_ORIGIN_KEY = "test_origin"
TEST_ORIGIN_VALUE = "pytest"


def live_point_count(qdrant_url: str, timeout_s: float = 2.0) -> int | None:
    """The LIVE collection's point count, or None when qdrant is unreachable.

    Always points at the LIVE collection name (that is the point).
    """
    try:
        with urlopen(
            f"{qdrant_url.rstrip('/')}/collections/{LIVE_COLLECTION}", timeout=timeout_s
        ) as response:
            payload = json.loads(response.read())
        return int(payload["result"]["points_count"])
    except Exception:  # noqa: BLE001 — unreachable infra must not fail the suite
        return None


def live_marker_point_count(qdrant_url: str, timeout_s: float = 2.0) -> int | None:
    """Points in the LIVE collection carrying the test-origin marker.

    Read-only POST /points/count with a payload filter; None when qdrant is
    unreachable (the guard stays silent, like the old count snapshot).
    """
    body = json.dumps(
        {
            "filter": {
                "must": [
                    {"key": TEST_ORIGIN_KEY, "match": {"value": TEST_ORIGIN_VALUE}}
                ]
            },
            "exact_count": True,
        }
    ).encode()
    request = Request(
        f"{qdrant_url.rstrip('/')}/collections/{LIVE_COLLECTION}/points/count",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urlopen(request, timeout=timeout_s) as response:
            payload = json.loads(response.read())
        return int(payload["result"]["count"])
    except Exception:  # noqa: BLE001 — unreachable infra must not fail the suite
        return None


def assert_no_test_points_in_live(marker_count: int | None) -> None:
    """Fail loudly if TEST-originated points exist in the LIVE collection."""
    if marker_count is None:
        return  # qdrant unreachable at this end — nothing to compare
    assert marker_count == 0, (
        f"LIVE collection holds {marker_count} test-originated point(s) — "
        f"a test wrote to the live store. Investigate before trusting any green run."
    )


def count_delta_note(before: int | None, after: int | None) -> str | None:
    """Informational note for a LIVE count change; None when unremarkable.

    An external write (the live instance ingesting) changes the count but
    carries no marker — that is normal operation, not test pollution.
    """
    if before is None or after is None or before == after:
        return None
    return (
        f"live collection points changed during the test session "
        f"({before} -> {after}) — no test-origin markers found; "
        f"external ingestion is expected, not a failure"
    )
