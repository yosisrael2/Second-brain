"""schemas guards: the memory_items.source CHECK domain, API side.

The migration adds ``ck_memory_items_source`` NOT VALID on live: every NEW
insert/update is checked by Postgres. The capture paths all write fixed
in-domain values (see the runner test file + the audit in PR #53), but the
API layer let a CLIENT send any source (POST /items, PATCH /items/{id}) —
after the migration that write would 500 on live. These tests pin the
schema-level guard: only ITEM_SOURCE_DOMAIN values pass validation.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from brain.db.enums import ITEM_SOURCE_DOMAIN
from brain.schemas import ItemCreate, ItemUpdate


def test_item_create_rejects_sources_outside_the_domain() -> None:
    with pytest.raises(ValidationError):
        ItemCreate(raw_text="x", source="bogus")
    # 'gmail' is deliberately NOT a source: the gmail adapter captures as
    # 'email' (compatibility with the existing email data).
    with pytest.raises(ValidationError):
        ItemCreate(raw_text="x", source="gmail")


def test_item_create_accepts_every_domain_value() -> None:
    for source in ITEM_SOURCE_DOMAIN:
        assert ItemCreate(raw_text="x", source=source).source == source


def test_item_create_default_source_is_manual() -> None:
    assert ItemCreate(raw_text="x").source == "manual"


def test_item_update_rejects_sources_outside_the_domain() -> None:
    with pytest.raises(ValidationError):
        ItemUpdate(source="bogus")


def test_item_update_source_unset_or_valid() -> None:
    assert ItemUpdate().source is None
    assert ItemUpdate(source="gcal").source == "gcal"
