"""Persistence for connector sync state and the external-id -> item ledger.

Tables (one migration, owned by the connectors ticket):
- ``connector_state(name PK, cursor, last_run_at, last_ok_at, last_error, counters)``
- ``connector_records(source, external_id, version, item_id, PK(source, external_id))``

``connector_records`` is the runner's idempotency memory: it maps every
synced external id to the item it produced and the version it saw, so
re-running a sync never duplicates and version bumps update in place. The
FK to memory_items is ON DELETE SET NULL: a user who deletes the item has
acted on it — the record stays (item_id NULL) and the runner never
re-captures it.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from brain.db.models import ConnectorRecord, ConnectorState

_LAST_ERROR_MAX = 2000  # keep the column small; errors are truncated


# Consecutive record-level failures before a record is quarantined: the
# cursor must never be wedged by one poison record forever.
QUARANTINE_AFTER = 3


class ConnectorStateRepo:
    """Cursor + health per connector (the ``connector_state`` table)."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def get(self, name: str) -> ConnectorState | None:
        return self._session.get(ConnectorState, name)

    def get_or_create(self, name: str) -> ConnectorState:
        row = self._session.get(ConnectorState, name)
        if row is None:
            row = ConnectorState(name=name)
            self._session.add(row)
            self._session.flush()
        return row

    def all(self) -> list[ConnectorState]:
        """Every connector's state row, alphabetical (CLI listing)."""
        return list(self._session.scalars(select(ConnectorState).order_by(ConnectorState.name)))

    def record_success(
        self, name: str, *, cursor: str | None, at: datetime
    ) -> None:
        """Persist the cursor + heartbeat fields after a fully successful batch."""
        row = self.get_or_create(name)
        row.cursor = cursor
        row.last_run_at = at
        row.last_ok_at = at
        row.last_error = None
        row.counters = {**(row.counters or {}), "attempt": 0}  # backoff resets
        self._session.commit()

    def record_error(
        self, name: str, *, error: str, at: datetime, retryable: bool
    ) -> None:
        """Record a failure; only retryable failures grow the backoff counter.

        The cursor is deliberately untouched: the next run re-fetches the
        same window (at-least-once; the runner is idempotent by
        external_id/version).
        """
        row = self.get_or_create(name)
        row.last_run_at = at
        row.last_error = error[:_LAST_ERROR_MAX]
        if retryable:
            row.counters = {**(row.counters or {}), "attempt": self.attempt(name) + 1}
        self._session.commit()

    def attempt(self, name: str) -> int:
        """The current consecutive-failure counter (backoff exponent)."""
        row = self.get(name)
        return int((row.counters or {}).get("attempt", 0)) if row else 0


class ConnectorRecordRepo:
    """The ``connector_records`` ledger: external id -> item + seen version."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def get(self, source: str, external_id: str) -> ConnectorRecord | None:
        stmt = select(ConnectorRecord).where(
            ConnectorRecord.source == source, ConnectorRecord.external_id == external_id
        )
        return self._session.scalars(stmt).first()

    def put(self, source: str, external_id: str, version: str | None, item_id: object) -> None:
        """Insert or update the ledger row for one synced record.

        A successful sync also resets the poison-record bookkeeping: the
        record is back in the runner's good graces.
        """
        row = self.get(source, external_id)
        if row is None:
            row = ConnectorRecord(source=source, external_id=external_id)
            self._session.add(row)
        row.version = version
        row.item_id = item_id  # type: ignore[assignment]
        row.status = "synced"
        row.fail_count = 0
        row.last_error = None
        self._session.commit()

    def mark_failure(
        self, source: str, external_id: str, version: str | None, error: str
    ) -> bool:
        """Count one more consecutive failure for this record.

        The failing record may have poisoned the SHARED session (a real DB
        error — truncation, FK violation, bad JSONB — aborts the
        transaction), so roll back FIRST: bookkeeping on a poisoned session
        would raise PendingRollbackError inside the runner's except block
        and abort run() with no quarantine and no cursor release. The
        rollback only discards the failed record's partial writes — every
        earlier record of the batch was already committed per record (each
        _handle path commits before returning).

        After QUARANTINE_AFTER failures in a row the record is quarantined:
        status 'failed' + the failing version recorded — the runner stops
        retrying it, so the batch's cursor can advance. Returns True iff the
        record is NOW quarantined. A later version bump clears it (the
        runner calls :meth:`clear_failure`). The version is only written on
        quarantining — an in-flight failing record keeps its old version so
        the runner's version-equality skip never bypasses the retry.
        """
        self._session.rollback()
        row = self.get(source, external_id)
        if row is None:
            row = ConnectorRecord(source=source, external_id=external_id)
            self._session.add(row)
        row.fail_count = (row.fail_count or 0) + 1
        row.last_error = error
        quarantined = row.fail_count >= QUARANTINE_AFTER
        if quarantined:
            row.status = "failed"
            row.version = version
        self._session.commit()
        return quarantined

    def clear_failure(self, source: str, external_id: str) -> None:
        """Release a quarantined record (the source bumped its version)."""
        row = self.get(source, external_id)
        if row is not None and row.status == "failed":
            row.status = "synced"
            row.fail_count = 0
            row.last_error = None
            self._session.commit()

    def quarantined_counts(self) -> dict[str, int]:
        """Quarantined (status 'failed') rows per source (CLI listing)."""
        rows = self._session.execute(
            select(ConnectorRecord.source, func.count())
            .where(ConnectorRecord.status == "failed")
            .group_by(ConnectorRecord.source)
        ).all()
        return {source: n for source, n in rows}

    def counts(self) -> dict[str, int]:
        """Rows per source (CLI listing)."""
        rows = self._session.execute(
            select(ConnectorRecord.source, ConnectorRecord.external_id)
        ).all()
        counts: dict[str, int] = {}
        for source, _external_id in rows:
            counts[source] = counts.get(source, 0) + 1
        return counts
