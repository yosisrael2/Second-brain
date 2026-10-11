"""Route band tests for brain.ai.classify.route."""

import pytest

from brain.ai.classify import route
from brain.ai.enums import ItemStatus


@pytest.mark.parametrize(
    ("confidence", "expected"),
    [
        (1.0, ItemStatus.FILED),
        (0.95, ItemStatus.FILED),
        (0.9, ItemStatus.FILED),  # boundary: exactly 0.9 is filed
        (0.899, ItemStatus.NEEDS_REVIEW),
        (0.7, ItemStatus.NEEDS_REVIEW),
        (0.5, ItemStatus.NEEDS_REVIEW),  # boundary: exactly 0.5 needs review
        (0.499, ItemStatus.INBOX),
        (0.0, ItemStatus.INBOX),
    ],
)
def test_route_bands(confidence: float, expected: ItemStatus) -> None:
    result = route(confidence)
    assert result is expected
