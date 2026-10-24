"""A missing python-dateutil must fail LOUDLY, not degrade recurring pastes.

worker_10 hit the silent variant locally: expand_occurrences imported
dateutil INSIDE its catch-all try, so an env that skipped the editable
install returned None for EVERY rule — identical to a malformed rule — and
each recurring paste quietly became a single DTSTART item. The import now
lives at module level: blocking it fails the import of this module (loud,
at the first recurring paste) while the broad except keeps handling
genuinely malformed RRULE text.
"""

import builtins
import importlib
import sys

import pytest


def test_missing_dateutil_fails_the_import_loudly(monkeypatch):

    real_import = builtins.__import__

    def _blocked(name, *args, **kwargs):
        if name.split(".")[0] == "dateutil":
            raise ImportError(f"No module named {name!r} (blocked by test)")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _blocked)
    monkeypatch.delitem(sys.modules, "brain.services.recurrence", raising=False)
    with pytest.raises(ImportError):
        importlib.import_module("brain.services.recurrence")

    # leave a WORKING module in sys.modules for the other tests
    monkeypatch.undo()
    importlib.import_module("brain.services.recurrence")
