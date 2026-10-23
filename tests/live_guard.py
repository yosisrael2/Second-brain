"""Session-level tripwire against live-collection pollution (see PR #42 incident).

``conftest.py`` runs :func:`live_point_count` before and after the whole
suite; :func:`assert_points_stable` turns any change in the LIVE collection's
point count into a loud failure. Read-only (a single Qdrant REST GET);
``None`` (qdrant unreachable) never fails the run.
"""

import json
from urllib.request import urlopen


def live_point_count(qdrant_url: str, timeout_s: float = 2.0) -> int | None:
    """The LIVE collection's point count, or None when qdrant is unreachable.

    Always points at the LIVE collection name (that is the point: the suite
    must notice if the live store changed at all, even accidentally).
    """
    try:
        with urlopen(
            f"{qdrant_url.rstrip('/')}/collections/brain_items", timeout=timeout_s
        ) as response:
            payload = json.loads(response.read())
        return int(payload["result"]["points_count"])
    except Exception:  # noqa: BLE001 — unreachable infra must not fail the suite
        return None


def assert_points_stable(before: int | None, after: int | None) -> None:
    """Fail loudly if the LIVE collection's point count changed mid-suite."""
    if before is None or after is None:
        return  # qdrant unreachable at one end — nothing to compare
    assert before == after, (
        f"LIVE collection points changed during the test session "
        f"({before} -> {after}) — a test wrote to the live store. "
        f"Investigate before trusting any green run."
    )
