"""Skeleton smoke tests for the connectors contract (ticket: connectors).

Only proves the contract skeleton: importability, the binding dataclass/ABC
shapes, the registry, and that the GoogleConnector stub is a signature stub.
Runner/framework behavior is covered by the framework tests of the same PR.
"""

from __future__ import annotations

import pytest

from brain.connectors import Connector, all_names, alias, get, register
from brain.connectors.base import (
    AttachmentRef,
    ConnectorAuthError,
    ConnectorError,
    ConnectorRetryableError,
    FetchResult,
    SourceRecord,
)
from brain.connectors.google.base import GoogleConnector


class _Dummy(Connector):
    name = "dummy"

    def fetch(self, cursor: str | None, *, limit: int) -> FetchResult:
        raise AssertionError("not used in this test")


def test_error_hierarchy():
    assert issubclass(ConnectorAuthError, ConnectorError)
    assert issubclass(ConnectorRetryableError, ConnectorError)


def test_source_record_defaults():
    record = SourceRecord(
        source="gcal",
        external_id="e1",
        version="v1",
        title="t",
        text="x",
        occurred_at=None,
    )
    assert record.meta == {}
    assert record.attachments == ()
    assert record.deleted is False
    assert record.url is None


def test_attachment_ref_size_optional():
    assert AttachmentRef(ref_id="a", filename="f.pdf", mime_type="application/pdf").size is None


def test_connector_is_abstract():
    with pytest.raises(TypeError):
        Connector()  # type: ignore[abstract]


def test_register_get_alias():
    with pytest.raises(ValueError):
        register(_Dummy)  # already registered via an earlier test in this module

    assert get("dummy") is _Dummy
    alias("d", "dummy")
    assert get("d") is _Dummy
    assert "dummy" in all_names()
    with pytest.raises(KeyError):
        get("no-such-connector")


def test_google_connector_stub_is_a_signature_stub():
    assert issubclass(GoogleConnector, Connector)
    conn = object.__new__(GoogleConnector)
    with pytest.raises(NotImplementedError):
        conn.fetch(None, limit=10)
    with pytest.raises(NotImplementedError):
        GoogleConnector(token_loader=lambda: "", service_factory=lambda token, scopes: None)
    with pytest.raises(NotImplementedError):
        list(GoogleConnector.iter_pages(lambda token: {}))
    with pytest.raises(NotImplementedError):
        GoogleConnector._translate_error(Exception("x"))
