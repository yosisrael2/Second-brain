"""Shared pytest fixtures."""

import pytest
from fastapi.testclient import TestClient

from brain.main import create_app


@pytest.fixture(name="client")
def client_fixture() -> TestClient:
    """TestClient wired to a fresh app instance."""
    return TestClient(create_app())
