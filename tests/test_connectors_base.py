"""Skeleton smoke tests for the connectors contract (ticket: connectors).

Only proves the contract skeleton: importability, the binding dataclass/ABC
shapes, the registry, and that the GoogleConnector stub is a signature stub.
Runner/framework behavior is covered by the framework tests of the same PR.
"""

from __future__ import annotations

import pytest

from brain.connectors import Connector, alias, all_names, get, register
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


# Registered once at import: the duplicate-registration guard needs it.
register(_Dummy)


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
    assert get("dummy") is _Dummy  # registered at import (see _Dummy above)
    with pytest.raises(ValueError):
        register(_Dummy)  # duplicate name refused

    alias("d", "dummy")
    assert get("d") is _Dummy
    assert "dummy" in all_names()
    with pytest.raises(KeyError):
        get("no-such-connector")


def test_google_connector_is_abstract():
    """GoogleConnector carries the shared machinery; fetch() is the adapter's."""
    assert issubclass(GoogleConnector, Connector)
    with pytest.raises(TypeError):
        GoogleConnector(  # type: ignore[abstract]
            token_loader=lambda: "", service_factory=lambda token, scopes: None
        )
