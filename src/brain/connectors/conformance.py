"""Connector conformance kit: the adversarial checks every source gets for free.

:func:`assert_conforms` drives a connector through the REAL
:class:`~brain.connectors.runner.ConnectorRunner` (real repositories on the
caller's test database) and asserts the five behaviors every adapter must
have. A connector conforms when all five pass; the first failure raises
:class:`ConformanceFailure` naming the check.

1. TERMINATION — repeated small-limit syncs over a dataset LARGER than the
   limit, with the API emitting in an order that is NOT the cursor order,
   must drain within a bounded number of fetches and ingest every record
   exactly once. This catches the emit-in-API-order +
   resume-at-earliest-unemitted livelock: the same page repeats forever.
2. IDEMPOTENCY — a second full sync creates 0, updates 0, deletes 0.
3. CHANGE — one version bump -> exactly 1 update, and the item takes the text.
4. DELETE — the item is hidden via the PR B mechanism, never hard-deleted.
5. RESET — ``reset=True`` never deletes anything: a resync that returns only
   a subset of records leaves every absent record's item untouched.

Usage (adapter tests)::

    def test_gcal_conforms(runner_db):
        scenario = ConformanceScenario(records=tuple(make_record(...) ...))
        assert_conforms(lambda api: GcalConnector(service=fake_service(api)),
                        scenario, session=runner_db)

:class:`FakeSourceAPI` pages the dataset by index in the order given
(deliberately arbitrary); the adapter maps that API's cursor onto its own.
``session`` is the caller's test session — the runner writes real rows
through it; the caller owns cleaning connector_state/connector_records/
memory_items (the fixture shape is in tests/test_connectors_conformance.py).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from brain.ai.classify import FALLBACK_CLASSIFICATION
from brain.connectors.base import Connector, FetchResult
from brain.connectors.runner import ConnectorRunner, SyncSummary
from brain.connectors.state import ConnectorRecord, ConnectorRecordRepo, ConnectorStateRepo
from brain.repositories.items import ItemsRepository
from brain.services.ingest import IngestService


class ConformanceFailure(AssertionError):
    """One conformance check failed. ``check`` names which one."""

    def __init__(self, check: str, message: str) -> None:
        super().__init__(message)
        self.check = check


@dataclass(frozen=True)
class ConformanceScenario:
    """The dataset the fake API serves, in the API's OWN emission order.

    Deliberately NOT the external_id order: the termination check only
    bites when emit order and cursor order disagree.

    ``cursor_key`` (optional) switches the fake API to KEY-based paging, the
    shape real sources have: records are served in (key, external_id) order
    and the continuation cursor is ``"key|external_id"`` — resume STRICTLY
    after it. Adapters pass their own cursor key (e.g. the record's
    ``occurred_at`` / updated timestamp) so the tie-group termination pass
    exercises THEIR key semantics: more records sharing one key than fit in
    a page, followed by newer records. A connector that resumes at the bare
    key (``key >= cursor``) re-serves the same ties forever.
    """

    records: tuple[Any, ...]
    name: str = "gcal"
    cursor_key: Callable[[Any], str] | None = None
    keys: dict[str, str] | None = None  # explicit external_id -> key override

    def effective_keys(self) -> dict[str, str] | None:
        """The cursor key per external_id (None = position paging)."""
        if self.keys is not None:
            return dict(self.keys)
        if self.cursor_key is None:
            return None
        return {r.external_id: str(self.cursor_key(r)) for r in self.records}

    def with_tie_group(self, *, limit: int) -> ConformanceScenario:
        """The TIE-GROUP variant: the same records under key paging, with
        ``limit + 2`` of them sharing ONE key (a tie group bigger than a
        page) and the rest on strictly newer keys. External ids are prefixed
        with ``tie-`` so the pass ingests fresh items instead of skipping the
        already-ingested ones (a vacuous pass would prove nothing)."""
        ids = [r.external_id for r in self.records]
        by_id = {r.external_id: r for r in self.records}
        group, rest = ids[: limit + 2], ids[limit + 2 :]
        keys = {f"tie-{rid}": "TIE" for rid in group}
        keys.update({f"tie-{rid}": f"TIE-{i + 1:03d}" for i, rid in enumerate(rest)})
        records = tuple(
            by_id[rid].__class__(**{**by_id[rid].__dict__, "external_id": f"tie-{rid}"})
            for rid in ids
        )
        return ConformanceScenario(
            records=records, name=self.name, cursor_key=self.cursor_key, keys=keys
        )


class FakeSourceAPI:
    """Stands in for the real source API: pages the dataset by index.

    The cursor is the next page's start index (as a string); ``None`` means
    drained. A well-behaved connector delegates here (the reference factory
    in the kit's tests is exactly that); a broken connector may save any
    cursor it likes — the kit catches the consequence.
    """

    def __init__(self, scenario: ConformanceScenario, *, page_size: int) -> None:
        self.name = scenario.name
        self.records = list(scenario.records)
        self.page_size = page_size
        self.fetches: list[tuple[str | None, int]] = []
        self._next_only: FetchResult | None = None
        # KEY MODE (scenario.keys/cursor_key set): records are served in
        # (key, external_id) order and the continuation cursor is
        # "key|external_id" — resume STRICTLY after it. "|" is reserved.
        self._keys: dict[str, str] | None = scenario.effective_keys()

    def fetch(self, cursor: str | None, *, limit: int) -> FetchResult:
        self.fetches.append((cursor, limit))
        if self._next_only is not None:
            result, self._next_only = self._next_only, None
            return result
        if self._keys is not None:
            return self._fetch_by_key(cursor, limit)
        start = int(cursor) if cursor else 0
        page = self.records[start : start + limit]
        nxt = start + len(page)
        return FetchResult(
            records=list(page),
            next_cursor=str(nxt) if nxt < len(self.records) else None,
        )

    def _fetch_by_key(self, cursor: str | None, limit: int) -> FetchResult:
        keys = self._keys
        if keys is None:  # internal invariant: only called in key mode
            raise RuntimeError("_fetch_by_key without a key-mode scenario")
        ordered = sorted(
            self.records, key=lambda r: (keys[r.external_id], r.external_id)
        )
        start = 0
        if cursor:
            if "|" in cursor:
                key, eid = cursor.split("|", 1)
                start = next(
                    (
                        i
                        for i, r in enumerate(ordered)
                        if (keys[r.external_id], r.external_id) > (key, eid)
                    ),
                    len(ordered),
                )
            else:
                # A bare key: a naive source resumes at "key >= cursor" —
                # the start of that key's tie group (this is the trap).
                start = next(
                    (
                        i
                        for i, r in enumerate(ordered)
                        if keys[r.external_id] == cursor
                    ),
                    len(ordered),
                )
        page = ordered[start : start + limit]
        if not page or start + len(page) >= len(ordered):
            nxt = None
        else:
            last = page[-1]
            nxt = f"{keys[last.external_id]}|{last.external_id}"
        return FetchResult(records=list(page), next_cursor=nxt)

    def bump(self, external_id: str, **changes: Any) -> None:
        """Replace one record with a modified copy (CHANGE / DELETE checks)."""
        for i, record in enumerate(self.records):
            if record.external_id == external_id:
                self.records[i] = record.__class__(**{**record.__dict__, **changes})
                return
        raise KeyError(external_id)

    def add(self, record: Any) -> None:
        """Add one record upstream (IMMUTABLE-source CHANGE check).

        A new upstream record exists; adding a record whose id is ALREADY in
        the dataset models the source RE-DELIVERING that id (e.g. a label
        re-add) — the adapter must skip it, never create a second item."""
        self.records.append(record)

    def mark_deleted(self, external_id: str) -> None:
        """Flag one record deleted (the DELETE check)."""
        self.bump(external_id, version="deleted", deleted=True)

    def next_fetch_only(self, external_ids: list[str], *, reset: bool = True) -> None:
        """Make the NEXT fetch return exactly these records (the RESET check)."""
        subset = [r for r in self.records if r.external_id in external_ids]
        if len(subset) != len(external_ids):
            missing = sorted(set(external_ids) - {r.external_id for r in subset})
            raise KeyError(missing)
        self._next_only = FetchResult(records=subset, next_cursor=None, reset=reset)


class _KitClassifier:
    """No Jev, no network: every item routes to the inbox."""

    def classify(self, text: str) -> dict[str, Any]:
        return dict(FALLBACK_CLASSIFICATION)


class _KitHarness:
    """The production wiring on the caller's session (the test database)."""

    def __init__(self, session: Session) -> None:
        # Deferred: testing.py re-exports this kit, so a module-level import
        # would be circular (testing -> conformance -> testing).
        from brain.connectors.testing import (  # noqa: PLC0415
            RecordingEnrichment,
            RecordingIndexer,
        )

        self.session = session
        self.repo = ItemsRepository(session)
        self.indexer = RecordingIndexer()
        self.enrichment = RecordingEnrichment()
        self.ingest = IngestService(self.repo, _KitClassifier(), self.indexer)
        self.state = ConnectorStateRepo(session)
        self.records = ConnectorRecordRepo(session)

    def runner(self, connector: Connector) -> ConnectorRunner:
        return ConnectorRunner(
            connector,
            ingest=self.ingest,
            repo=self.repo,
            state=self.state,
            records=self.records,
            indexer=self.indexer,
            enrichment=self.enrichment,
        )

    def item(self, source: str, external_id: str) -> Any:
        """The item a record produced (None when never ingested or deleted)."""
        row = self.records.get(source, external_id)
        if row is None or row.item_id is None:
            return None
        return self.repo.get(row.item_id)

    def ingested_ids(self, source: str) -> set[str]:
        """External ids of the source that currently have a live item."""
        rows = self.session.scalars(
            select(ConnectorRecord).where(ConnectorRecord.source == source)
        ).all()
        return {
            row.external_id
            for row in rows
            if row.item_id is not None and self.repo.get(row.item_id) is not None
        }

    def reset_cursor(self, source: str) -> None:
        """Start the next sync from a fresh cursor.

        Each conformance pass drives an independent scenario generation; the
        cursor a previous pass left behind belongs to a DIFFERENT api (and,
        for the tie-group pass, a different cursor MODE), so it must not
        leak into the next pass's first fetch.
        """
        from datetime import UTC, datetime  # noqa: PLC0415

        self.state.record_success(source, cursor=None, at=datetime.now(UTC))


def assert_conforms(
    make_connector: Callable[[Any], Connector],
    scenario: ConformanceScenario,
    *,
    session: Session,
    limit: int = 3,
    max_fetches: int | None = None,
    immutable_source: bool = False,
) -> dict[str, Any]:
    """Run the five conformance checks against the REAL ConnectorRunner.

    ``make_connector(api)`` must return a FRESH connector each call (the CLI
    builds one per sync); the kit calls it once per sync. Returns a report
    dict (one entry per check) on success; raises :class:`ConformanceFailure`
    on the first violated check.

    ``immutable_source`` (declared by a donation module as
    ``IMMUTABLE_SOURCE = True``) switches the CHANGE check to the domain-true
    variant for sources whose records cannot change under the same id
    (Gmail: messages are immutable; changes are new ids + re-deliveries).
    The report names which variant ran (``change_variant``) so an immutable
    pass is never mistaken for a skipped check.
    """
    api = FakeSourceAPI(scenario, page_size=limit)
    n = len(api.records)
    if n <= limit:
        raise ValueError(
            f"conformance needs a dataset larger than the limit "
            f"({n} records vs limit {limit}) — otherwise termination is trivial"
        )
    tie_scenario = scenario.with_tie_group(limit=limit)
    tie_api = FakeSourceAPI(tie_scenario, page_size=limit)
    harness = _KitHarness(session)

    def connector_for() -> Connector:
        return make_connector(api)

    report: dict[str, Any] = {
        "termination": _check_termination(
            harness, connector_for, api, limit=limit, max_fetches=max_fetches
        ),
        # The TIE-GROUP pass: more records sharing one cursor key than fit in
        # a page, then newer records — catches the resume-at-the-bare-key
        # livelock (key >= cursor) that scrambled ORDER alone cannot.
        "termination_tie_group": _check_termination(
            harness,
            lambda: make_connector(tie_api),
            tie_api,
            limit=limit,
            max_fetches=max_fetches,
            check="termination_tie_group",
        ),
        "idempotency": _check_idempotency(harness, connector_for, api, limit=limit),
        "change": _check_change(
            harness, connector_for, api, scenario, limit=limit,
            immutable_source=immutable_source,
        ),
        "delete": _check_delete(harness, connector_for, api, scenario, limit=limit),
        "reset": _check_reset(harness, connector_for, api, scenario, limit=limit),
    }
    report["change_variant"] = (
        "immutable-new-record" if immutable_source else "version-bump"
    )
    return report


def _check_termination(
    harness: _KitHarness,
    connector_for: Callable[[], Connector],
    api: FakeSourceAPI,
    *,
    limit: int,
    max_fetches: int | None,
    check: str = "termination",
) -> dict[str, int]:
    """Repeated small-limit syncs must drain the dataset in bounded syncs.

    The bound is on SYNC COUNT, never on API fetch count: a broken connector
    may bypass the kit's API entirely (that is the livelock under test), and
    the kit itself must terminate on ANY connector.
    """
    harness.reset_cursor(api.name)
    n = len(api.records)
    cap = max_fetches if max_fetches is not None else (n // limit + 1) + 1
    used = 0
    drained = False
    for _sync_number in range(cap):
        summary = _one_sync(harness, connector_for(), api, limit=limit)
        used += 1
        if summary.errors:
            raise ConformanceFailure(
                check,
                f"{check}: {summary.errors} record(s) failed on sync "
                f"{_sync_number + 1} ({summary.error})",
            )
        if summary.fetched == 0:
            used -= 1  # the drain probe fetched nothing
            drained = True
            break
    ingested = harness.ingested_ids(api.name)
    ids = {r.external_id for r in api.records}
    missing = sorted(ids - ingested)
    if missing:
        raise ConformanceFailure(
            check,
            f"{check}: {len(ids) - len(missing)} of {len(ids)} records ingested after "
            f"{used} syncs (limit {limit}); missing: {missing}. "
            f"The sync never drained the dataset — check the connector's cursor "
            f"semantics (emitted records must be reachable from the cursors it "
            f"returns; emit-in-API-order + resume-at-earliest-unemitted livelocks; "
            f"a cursor that drops the tie-break livelocks on tie groups).",
        )
    return {"syncs": used, "drained": drained, "ingested": len(ids)}


def _check_idempotency(
    harness: _KitHarness,
    connector_for: Callable[[], Connector],
    api: FakeSourceAPI,
    *,
    limit: int,
) -> dict[str, int]:
    """Re-syncing the whole (unchanged) dataset must change nothing."""
    harness.reset_cursor(api.name)
    totals = _sync_totals(harness, connector_for, api, limit=limit, check="idempotency")
    if (totals["created"], totals["updated"], totals["deleted"]) != (0, 0, 0):
        raise ConformanceFailure(
            "idempotency",
            f"idempotency: a second full sync created {totals['created']}, updated "
            f"{totals['updated']}, deleted {totals['deleted']} — must be 0/0/0 "
            f"(re-running a sync never duplicates).",
        )
    return totals


def _check_change(
    harness: _KitHarness,
    connector_for: Callable[[], Connector],
    api: FakeSourceAPI,
    scenario: ConformanceScenario,
    *,
    limit: int,
    immutable_source: bool = False,
) -> dict[str, Any]:
    """One upstream change -> exactly one ingested change, and the item takes
    the text the ADAPTER emitted for the new state.

    Rendering adapters produce their own item text (a gtasks task is not its
    notes string), so the kit compares the item against the emitted
    SourceRecord — passed through the runner's ONE clamp site, never a
    second rule — not against the raw scenario record.

    Two variants:

    - ``version-bump`` (default): the record's version moves under the same
      id and the runner must update the item.
    - ``immutable-new-record`` (the donation module declares
      ``IMMUTABLE_SOURCE = True`` — e.g. Gmail, whose messages cannot change
      under the same id): (1) a NEW record added upstream is ingested
      exactly once; (2) the SAME id re-delivered upstream is skipped — no
      second item, no update, no delete.
    """
    if immutable_source:
        return _check_change_immutable(
            harness, connector_for, api, scenario, limit=limit
        )
    bumped = scenario.records[0].external_id
    new_text = f"body of {bumped}, SECOND VERSION"
    api.bump(bumped, version="conformance-v2", text=new_text)
    harness.reset_cursor(api.name)

    captured: dict[str, Any] = {}

    def capturing_connector() -> Connector:
        conn = connector_for()
        original_fetch = conn.fetch

        def fetch(cursor: str | None, *, limit: int) -> FetchResult:
            result = original_fetch(cursor, limit=limit)
            for record in result.records:
                if record.external_id == bumped:
                    captured["record"] = record
            return result

        conn.fetch = fetch  # type: ignore[method-assign]
        return conn

    updated = _sync_totals(
        harness, capturing_connector, api, limit=limit, check="change"
    )["updated"]
    if updated != 1:
        raise ConformanceFailure(
            "change",
            f"change: one version bump produced {updated} updates (expected exactly 1)",
        )
    if "record" not in captured:
        raise ConformanceFailure(
            "change",
            f"change: the adapter never re-emitted the changed record {bumped!r}",
        )
    # The runner's own clamp (no second rule): the item gets the clamped text.
    clamped_record, _truncated = harness.runner(
        connector_for()
    )._clamp_text(captured["record"])
    item = harness.item(api.name, bumped)
    if item is None or item.raw_text != clamped_record.text:
        got = None if item is None else item.raw_text
        raise ConformanceFailure(
            "change",
            f"change: the bumped record's item did not take the emitted text "
            f"(expected {clamped_record.text!r}, got {got!r})",
        )
    return {
        "updated_records": updated,
        "variant": "version-bump",
    }


def _check_change_immutable(
    harness: _KitHarness,
    connector_for: Callable[[], Connector],
    api: FakeSourceAPI,
    scenario: ConformanceScenario,
    *,
    limit: int,
) -> dict[str, Any]:
    """The CHANGE check for sources whose records never change under an id.

    (1) A NEW record added upstream is ingested exactly once. (2) The SAME
    id re-delivered upstream is skipped: no second item, no update, no
    delete — a re-delivery is never a change.
    """
    template = scenario.records[0]
    new_id = f"added-{template.external_id}"
    # A genuinely NEW record: distinct text AND instant, so the ingest
    # fingerprint cannot collide with the template's item (a fingerprint
    # dedupe would make the new record a dedup, not a creation).
    fresh_fields = {
        **template.__dict__,
        "external_id": new_id,
        "text": f"{template.text} (freshly arrived)",
    }
    if template.occurred_at is not None:
        from datetime import timedelta  # noqa: PLC0415

        fresh_fields["occurred_at"] = template.occurred_at + timedelta(hours=1)
    api.add(template.__class__(**fresh_fields))
    harness.reset_cursor(api.name)
    totals = _sync_totals(harness, connector_for, api, limit=limit, check="change")
    if totals["created"] != 1:
        raise ConformanceFailure(
            "change",
            f"change (immutable): a NEW upstream record produced "
            f"{totals['created']} creations (expected exactly 1)",
        )
    item = harness.item(api.name, new_id)
    if item is None:
        raise ConformanceFailure(
            "change", "change (immutable): the new record's item is missing"
        )
    # (2) The same id re-delivered upstream: skipped, never a second item.
    api.add(template.__class__(**{**template.__dict__, "external_id": new_id}))
    redelivered = _sync_totals(
        harness, connector_for, api, limit=limit, check="change"
    )
    if redelivered["created"] or redelivered["updated"] or redelivered["deleted"]:
        raise ConformanceFailure(
            "change",
            f"change (immutable): re-delivering the same id created "
            f"{redelivered['created']}, updated {redelivered['updated']}, "
            f"deleted {redelivered['deleted']} — a re-delivery is never a change",
        )
    again = harness.item(api.name, new_id)
    if again is None or again.id != item.id:
        raise ConformanceFailure(
            "change",
            "change (immutable): the re-delivery produced a second item for "
            f"{new_id!r}",
        )
    return {
        "created_records": 1,
        "redelivered_updates": 0,
        "variant": "immutable-new-record",
    }


def _check_delete(
    harness: _KitHarness,
    connector_for: Callable[[], Connector],
    api: FakeSourceAPI,
    scenario: ConformanceScenario,
    *,
    limit: int,
) -> dict[str, bool]:
    """deleted=True hides the item via the PR B mechanism and never hard-deletes."""
    doomed = scenario.records[-1].external_id
    api.mark_deleted(doomed)
    harness.reset_cursor(api.name)
    deletions = _sync_totals(
        harness, connector_for, api, limit=limit, check="delete"
    )["deleted"]
    if deletions != 1:
        raise ConformanceFailure(
            "delete",
            f"delete: marking one record deleted produced {deletions} deletions "
            f"(expected exactly 1)",
        )
    item = harness.item(api.name, doomed)
    if item is None:
        raise ConformanceFailure("delete", "delete: the item was hard-deleted from the DB")
    hidden = item.meta.get("source_deleted") is True and item.superseded_by == item.id
    visible = harness.repo.list(item_type=None, project=None, status=None, limit=50, offset=0)
    if not hidden or any(i.id == item.id for i in visible):
        raise ConformanceFailure(
            "delete",
            "delete: the item must gain source_deleted=true + superseded_by=self "
            "(PR B hiding) and vanish from the default listing",
        )
    return {"hidden": True, "still_in_db": True}


def _check_reset(
    harness: _KitHarness,
    connector_for: Callable[[], Connector],
    api: FakeSourceAPI,
    scenario: ConformanceScenario,
    *,
    limit: int,
) -> dict[str, int]:
    """A reset batch returning only a subset must delete nothing."""
    doomed = scenario.records[-1].external_id
    survivors = [r.external_id for r in scenario.records if r.external_id != doomed][:2]
    api.next_fetch_only(survivors, reset=True)
    summary = _one_sync(harness, connector_for(), api, limit=limit)
    if summary.reset is not True:
        raise ConformanceFailure("reset", "reset: the reset flag did not reach the runner")
    if summary.deleted != 0:
        raise ConformanceFailure(
            "reset",
            f"reset: reset=True must never delete (absent != deleted); "
            f"{summary.deleted} item(s) were deleted/marked",
        )
    every = {r.external_id for r in scenario.records}
    missing = sorted(rid for rid in every if harness.item(api.name, rid) is None)
    if missing:
        raise ConformanceFailure(
            "reset",
            f"reset: records absent from the resync lost their items: {missing} "
            f"(absence never deletes)",
        )
    return {"deleted": summary.deleted, "missing_after_reset": len(missing)}


def _one_sync(
    harness: _KitHarness, connector: Connector, api: FakeSourceAPI, *, limit: int
) -> SyncSummary:
    """One runner cycle with one fresh connector (CLI parity; close after)."""
    summary = harness.runner(connector).run(limit=limit)
    connector.close()
    return summary


def _sync_totals(
    harness: _KitHarness,
    connector_for: Callable[[], Connector],
    api: FakeSourceAPI,
    *,
    limit: int,
    check: str,
) -> dict[str, int]:
    """Sync until the source goes quiet; sum created/updated/deleted/fetched.

    The real sequence is 'resync, then incrementals' — one empty sync does
    NOT mean the source is done (a listing fast path over a ledger emits
    nothing on the resync, then the incremental channel delivers the
    changes). Stop when the CURSOR STOPS MOVING (a sync saved the cursor the
    previous sync saved) or after 2 consecutive empty syncs — always under a
    hard sync bound so a broken adapter still terminates.
    """
    cap = 2 * (len(api.records) // limit + 1) + 3
    totals = {"created": 0, "updated": 0, "deleted": 0, "fetched": 0}
    prev_cursor: Any = object()  # sentinel: no sync has run yet
    empty_streak = 0
    for _sync_number in range(cap):
        summary = _one_sync(harness, connector_for(), api, limit=limit)
        if summary.errors:
            raise ConformanceFailure(
                check,
                f"{check}: {summary.errors} record(s) failed mid-check ({summary.error})",
            )
        for key in totals:
            totals[key] += getattr(summary, key)
        empty = summary.fetched == 0
        empty_streak = empty_streak + 1 if empty else 0
        cursor = harness.state.get_or_create(api.name).cursor
        if cursor == prev_cursor or empty_streak >= 2:
            break
        prev_cursor = cursor
    return totals
