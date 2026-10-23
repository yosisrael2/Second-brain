"""Connector conformance kit (brain.connectors.testing.assert_conforms).

The kit drives a connector through the REAL ConnectorRunner (real repos on
the caller's test database) and asserts the five behaviors every adapter
must have. Red-first: these tests were written before the kit existed.

The adversarial core is TERMINATION: the fake API emits its dataset in an
order that is NOT the cursor/external_id order, and the sync runs with a
small limit (3) over a 7-record dataset. A connector that emits in API order
while resuming at the earliest unemitted record in cursor order re-serves
the same page forever — exactly the livelock found in gtasks.
"""

from __future__ import annotations

import pytest
from sqlalchemy import text

from brain.connectors.base import Connector, FetchResult
from brain.connectors.conformance import (
    ConformanceFailure,
    ConformanceScenario,
    FakeSourceAPI,
    assert_conforms,
)
from brain.connectors.testing import FakeConnector, make_record

SCRAMBLED_IDS = ["r5", "r1", "r6", "r2", "r7", "r3", "r4"]


def _scenario() -> ConformanceScenario:
    """7 records; the API emits them in a scrambled (non-id) order."""
    records = tuple(
        make_record(external_id=rid, text=f"body of {rid}") for rid in SCRAMBLED_IDS
    )
    return ConformanceScenario(records=tuple(records))


@pytest.fixture(name="kit_db")
def kit_db_fixture(database: None):
    """Fresh connector tables + memory_items, cleaned after (runner_db shape)."""
    from brain.db.session import SessionLocal, engine

    with engine.begin() as conn:
        for table in ("connector_records", "connector_state"):
            conn.execute(text(f"DELETE FROM {table}"))
        conn.execute(text("DELETE FROM memory_items"))
    session = SessionLocal()
    yield session
    session.rollback()
    session.close()
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM connector_records"))
        conn.execute(text("DELETE FROM connector_state"))
        conn.execute(text("DELETE FROM memory_items"))


# --- the reference connector (what a well-behaved adapter looks like) --------


class _PageThroughFake(FakeConnector):
    """FakeConnector wired to the kit's fake API: fetch delegates to the API's
    paging (cursor in, page out), so the connector itself is stateless and the
    cursor the API returns is what the runner persists — the well-behaved
    strategy every adapter must reproduce with its real source."""

    def __init__(self, api: FakeSourceAPI) -> None:
        super().__init__(name=api.name)
        self._api = api

    def fetch(self, cursor: str | None, *, limit: int) -> FetchResult:
        self.cursors.append(cursor)
        self.limits.append(limit)
        return self._api.fetch(cursor, limit=limit)


def _reference_factory(api: FakeSourceAPI) -> Connector:
    return _PageThroughFake(api)


# --- the deliberately broken connector (the red evidence) --------------------


class LivelockConnector(Connector):
    """The gtasks bug, minimized: emits in API order from the TOP of the
    dataset on every fetch and persists a cursor that points back INTO the
    page it just emitted — so the same page repeats forever and a dataset
    larger than the limit never drains."""

    name = "gcal"

    def __init__(self, api: FakeSourceAPI) -> None:
        self._api = api

    def fetch(self, cursor: str | None, *, limit: int) -> FetchResult:
        page = self._api.records[:limit]
        return FetchResult(records=list(page), next_cursor="0")


# --- the five checks ---------------------------------------------------------


def test_kit_passes_a_well_behaved_connector(kit_db):
    report = assert_conforms(_reference_factory, _scenario(), session=kit_db)

    assert set(report) == {"termination", "idempotency", "change", "delete", "reset"}
    assert report["termination"]["syncs"] <= 8  # 3 pages + drain, not N-syncs
    assert report["termination"]["ingested"] == 7


def test_kit_fails_a_livelocked_connector_on_termination(kit_db):
    """The deliberately broken connector must FAIL — with a message that
    names the check and shows the never-draining page."""
    try:
        assert_conforms(
            lambda api: LivelockConnector(api),
            _scenario(),
            session=kit_db,
        )
    except ConformanceFailure as failure:
        assert failure.check == "termination"
        assert "3 of 7" in str(failure)  # the same first page, forever
    else:
        raise AssertionError("the livelock connector passed the conformance kit")


def test_kit_idempotency_change_delete_reset(kit_db):
    """The remaining four checks run to completion on a good connector and
    are reported individually (one line per check in the report)."""
    report = assert_conforms(_reference_factory, _scenario(), session=kit_db)

    assert report["idempotency"]["created"] == 0
    assert report["idempotency"]["updated"] == 0
    assert report["change"]["updated_records"] == 1  # the CHANGE check
    assert report["delete"]["hidden"] is True
    assert report["delete"]["still_in_db"] is True
    assert report["reset"]["deleted"] == 0
    assert report["reset"]["missing_after_reset"] == 0
