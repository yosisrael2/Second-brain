"""Conformance sweep (manager decision, 24 Oct): EVERY connector registered
on main must pass the kit.

- The registry is read LIVE at RUNTIME (``ensure_registered()`` +
  ``all_names()``) — never a hardcoded list — so an adapter merged later is
  covered automatically.
- Each adapter donates its OFFLINE conformance connector as
  ``tests/conformance/<name>.py`` exposing ``build(api) -> Connector`` (the
  adapter on a fake service that delegates to the kit's paging API). Fakes
  stay in tests; production code stays fake-free.
- The file is loaded BY PATH (``importlib.util.spec_from_file_location``):
  tests/ is not a package and the sweep must not depend on other test
  modules having been imported.
- The loop (not ``pytest.mark.parametrize``) is deliberate: parametrize
  arguments are computed at sweep-module COLLECTION time, so a donor module
  imported later in the session would be invisible — the exact trap the
  manager called out.

An adapter registered without its file FAILS the sweep naming the adapter —
that is the enforcement for every future adapter PR.
"""

from __future__ import annotations

import importlib.util
import sys
import warnings
from pathlib import Path

import pytest
from sqlalchemy import text

from brain.connectors import ensure_registered
from brain.connectors.conformance import (
    ConformanceScenario,
    assert_conforms,
)
from brain.connectors.testing import make_record

CONFORMANCE_DIR = Path(__file__).parent / "conformance"


@pytest.fixture(autouse=True)
def _pin_connector_settings(monkeypatch: pytest.MonkeyPatch):
    """Pin the settings the adapters read lazily per fetch (the gcal
    allowlist + zone) — the same pinning the adapter unit tests use, with
    the settings cache cleared so the sweep's env wins."""
    from brain.config import get_settings

    monkeypatch.setenv("TIMEZONE", "Asia/Jerusalem")
    monkeypatch.setenv("BRAIN_GCAL_CALENDARS", "primary")
    # The donor dataset carries fixed 2026 dates: pin BOTH window bounds wide
    # (the adapter tests' _OLD_ENOUGH pattern) so the sweep is independent of
    # the BRAIN_GCAL_PAST_DAYS / BRAIN_GCAL_FUTURE_DAYS defaults and of when
    # the suite runs.
    monkeypatch.setenv("BRAIN_GCAL_PAST_DAYS", "36500")
    monkeypatch.setenv("BRAIN_GCAL_FUTURE_DAYS", "36500")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture(name="kit_db")
def kit_db_fixture(database: None):
    """Fresh connector tables + memory_items, cleaned after — the same
    caller-owned shape the kit's own tests use (documented in
    conformance.py): the runner writes real rows on the caller's session."""
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

# A dataset larger than the kit's limit (3) with RFC 3339 versions: real
# sources watermark on timestamps, so the kit's key-mode pages must look
# like them (the tie-group variant overrides keys per record anyway).
_DATASET_IDS = ["r1", "r2", "r3", "r4", "r5", "r6", "r7"]


def _scenario(name: str) -> ConformanceScenario:
    records = tuple(
        make_record(
            external_id=rid,
            text=f"body of {rid}",
            version=f"2026-03-0{int(rid[1])}T09:00:00.000Z",
            source=name,
        )
        for rid in _DATASET_IDS
    )
    # cursor_key = the record's version: real sources page by a timestamp
    # key, and the tie-group pass must exercise the adapter's key semantics.
    return ConformanceScenario(
        records=records, name=name, cursor_key=lambda r: str(r.version)
    )


def _load_builder(name: str):
    """Load ``tests/conformance/<name>.py`` by PATH; None when absent.

    Returns ``(build, immutable)`` — the module's ``build(api)`` and its
    optional ``IMMUTABLE_SOURCE`` declaration (sources whose records never
    change under an id run the kit's domain-true change variant).
    """
    path = CONFORMANCE_DIR / f"{name}.py"
    if not path.is_file():
        return None
    spec = importlib.util.spec_from_file_location(f"conformance_{name}", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.build, bool(getattr(module, "IMMUTABLE_SOURCE", False))


def _product_names() -> list[str]:
    """The registered ADAPTER names — the live registry, minus test fixtures.

    The registry is process-global and pytest imports every test module at
    collection, so test files that register scratch connectors (e.g.
    test_connectors_base's ``dummy``, registered at import for its
    duplicate-guard test) land in it too. The invariant this encodes:
    PRODUCT ADAPTERS LIVE UNDER ``brain.`` (their classes come from
    ``brain.connectors.*`` modules); a class registered from anywhere else
    is a test fixture by construction and MUST NOT fail the sweep. Real
    adapters merged later register from ``brain.connectors.*`` and are
    covered automatically.
    """
    from brain.connectors import _REGISTRY

    names = sorted(
        name
        for name, cls in _REGISTRY.items()
        if str(getattr(cls, "__module__", "")).startswith("brain.")
    )
    # The invariant, asserted: everything the sweep will demand a donation
    # file for is a product module — no test fixture leaks into the list.
    assert all(_REGISTRY[name].__module__.startswith("brain.") for name in names)
    return names


def test_product_names_ignores_test_only_registrations(monkeypatch):
    """A registry entry whose class lives OUTSIDE brain. is a test fixture —
    ignored ON PURPOSE (the sweep never demands a donation file for it)."""
    from brain.connectors import _REGISTRY, ensure_registered
    from brain.connectors.base import Connector, FetchResult

    ensure_registered()
    product = next(
        name
        for name, cls in _REGISTRY.items()
        if cls.__module__.startswith("brain.")
    )

    class _FixtureConnector(Connector):
        name = "fixture"

        def fetch(self, cursor: str | None, *, limit: int) -> FetchResult:
            raise AssertionError("not used")

    _FixtureConnector.__module__ = "tests.test_somewhere_else"
    fake_registry = {product: _REGISTRY[product], "fixture": _FixtureConnector}
    monkeypatch.setattr(
        sys.modules["brain.connectors"], "_REGISTRY", fake_registry
    )
    assert _product_names() == [product]  # 'fixture' ignored: not a brain. module


def test_sweep_every_registered_connector_passes_the_kit(kit_db):
    """One kit pass per registered connector; missing donor file = failure
    naming the adapter (the enforcement for future adapter PRs)."""
    ensure_registered()
    names = _product_names()
    assert names, (
        "the registry is empty — the sweep proves nothing; this test exists "
        "to fail when an adapter merges without tests/conformance/<name>.py"
    )

    missing = [name for name in names if _load_builder(name) is None]
    assert not missing, "\n".join(
        f"adapter {name!r} is registered but tests/conformance/{name}.py is "
        "missing — every connector on main must pass the conformance kit "
        "(add build(api) -> Connector delegating to the kit's paging API)"
        for name in missing
    )

    for name in names:
        loaded = _load_builder(name)
        assert loaded is not None  # narrowed above; for the type checker
        build, immutable = loaded
        report = assert_conforms(
            build, _scenario(name), session=kit_db, immutable_source=immutable
        )
        variant = report.pop("change_variant")
        assert set(report) == {
            "termination",
            "termination_tie_group",
            "idempotency",
            "change",
            "delete",
            "reset",
        }, f"{name}: the kit did not run every check: {sorted(report)}"
        # Visible in normal pytest output (no -s, no new dependency): the
        # summary warnings section names the change variant per adapter, so
        # an immutable pass is never mistaken for a skipped check.
        warnings.warn(
            f"conformance[{name}]: change variant = {variant} "
            f"({'IMMUTABLE_SOURCE declared' if immutable else 'version-bump'})",
            stacklevel=1,
        )
