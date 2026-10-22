"""The API must refuse to start against the LIVE store unless explicitly allowed.

Incident: a worker started ``uvicorn brain.main:app`` from a worktree without
.env, and Settings' defaults — database ``brain`` on localhost:27432, qdrant
collection ``brain_items`` — ARE the live service, so the instance silently
wrote to the live DB. The guard (``brain.config.assert_not_live_store``, wired
into the app lifespan) makes that startup fail fast. Only the launchd path
(scripts/run-server.sh) exports BRAIN_ALLOW_LIVE=1 to opt in.
"""

import pytest

from brain.config import (
    LIVE_DATABASE_URL,
    LIVE_QDRANT_COLLECTION,
    assert_not_live_store,
)

_MSG = "refusing to start against the LIVE store"


class TestGuardFunction:
    def test_raises_for_the_exact_live_url(self):
        with pytest.raises(RuntimeError, match="LIVE store"):
            assert_not_live_store(LIVE_DATABASE_URL, "brain_items_test")

    def test_raises_for_any_scheme_pointing_at_the_live_db(self):
        # a different driver spelling of the SAME live store must also be caught
        with pytest.raises(RuntimeError, match="LIVE store"):
            assert_not_live_store(
                "postgresql://brain:brain@127.0.0.1:27432/brain", "brain_items_test"
            )

    def test_raises_for_the_live_collection(self):
        with pytest.raises(RuntimeError, match="LIVE store"):
            assert_not_live_store(
                "postgresql+psycopg://brain:brain@localhost:27432/brain_w3_test",
                LIVE_QDRANT_COLLECTION,
            )

    def test_allow_live_flag_passes_the_live_store(self):
        # the launchd path: the real service IS allowed to run against live
        assert_not_live_store(
            LIVE_DATABASE_URL, LIVE_QDRANT_COLLECTION, allow_live="1"
        )

    def test_passes_for_a_test_url_and_collection(self):
        assert_not_live_store(
            "postgresql+psycopg://brain:brain@localhost:27432/brain_w3_test",
            "brain_items_test",
        )

    def test_passes_for_another_database_on_the_live_port(self):
        # the live marker is the DB NAME 'brain' on port 27432, not the port alone
        assert_not_live_store(
            "postgresql+psycopg://brain:brain@localhost:27432/brain_test",
            "brain_items_test",
        )

    def test_message_names_both_remedies(self):
        with pytest.raises(RuntimeError) as exc:
            assert_not_live_store(LIVE_DATABASE_URL, LIVE_QDRANT_COLLECTION)
        msg = str(exc.value)
        assert "set DATABASE_URL/QDRANT_COLLECTION" in msg
        assert "BRAIN_ALLOW_LIVE=1" in msg


class TestAppLifespan:
    def _drive(self):
        import asyncio

        from brain.main import lifespan

        async def run():
            async with lifespan(None):
                pass

        asyncio.run(run())

    def test_lifespan_refuses_live_store(self, monkeypatch):
        monkeypatch.setenv("DATABASE_URL", LIVE_DATABASE_URL)
        monkeypatch.setenv("QDRANT_COLLECTION", LIVE_QDRANT_COLLECTION)
        monkeypatch.delenv("BRAIN_ALLOW_LIVE", raising=False)
        from brain.config import get_settings

        get_settings.cache_clear()
        try:
            with pytest.raises(RuntimeError, match="LIVE store"):
                self._drive()
        finally:
            get_settings.cache_clear()

    def test_lifespan_starts_with_test_settings(self, monkeypatch):
        monkeypatch.setenv(
            "DATABASE_URL",
            "postgresql+psycopg://brain:brain@localhost:27432/brain_w3_test",
        )
        monkeypatch.setenv("QDRANT_COLLECTION", "brain_items_test")
        from brain.config import get_settings

        get_settings.cache_clear()
        try:
            self._drive()  # must not raise
        finally:
            get_settings.cache_clear()
