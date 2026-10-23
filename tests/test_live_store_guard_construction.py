"""The live-store guard must live at CONSTRUCTION, not only in the lifespan.

The #31 guard runs in the FastAPI lifespan — which TestClient(create_app())
never triggers without a ``with`` block — so a store/engine built outside a
real app start (a test, an ad-hoc script, a lru_cache surviving an env
change) could silently touch the LIVE Qdrant collection / database. These
tests pin the construction-time guards: building a REAL vector store or the
app engine against the live store refuses unless BRAIN_ALLOW_LIVE=1.
"""

import json
from urllib.error import URLError

import pytest
from tests.live_guard import assert_points_stable, live_point_count

from brain.ai.vectorstore import QdrantVectorStore
from brain.config import (
    assert_not_live_collection,
    assert_not_live_database,
    get_settings,
)

LIVE_COLLECTION = "brain_items"
LIVE_DB_URL = "postgresql+psycopg://brain:brain@localhost:27432/brain"


class TestVectorStoreConstruction:
    def test_refuses_the_live_collection(self) -> None:
        with pytest.raises(RuntimeError, match="LIVE"):
            QdrantVectorStore(collection=LIVE_COLLECTION)

    def test_allows_the_live_collection_with_the_flag(self, monkeypatch) -> None:
        monkeypatch.setenv("BRAIN_ALLOW_LIVE", "1")
        store = QdrantVectorStore(
            url="http://localhost:27333", collection=LIVE_COLLECTION
        )
        assert store._collection == LIVE_COLLECTION

    def test_allows_test_collections_without_a_flag(self) -> None:
        store = QdrantVectorStore(
            url="http://localhost:27333", collection="brain_items_w5_test"
        )
        assert store._collection == "brain_items_w5_test"

    def test_injected_client_is_never_guarded(self) -> None:
        # an injected client is by definition NOT the live qdrant (tests use
        # in-memory clients); the collection name alone must not block it
        class _FakeClient:
            pass

        store = QdrantVectorStore(collection=LIVE_COLLECTION, client=_FakeClient())
        assert store._collection == LIVE_COLLECTION


class TestEngineConstruction:
    def test_refuses_the_live_database(self, monkeypatch) -> None:
        monkeypatch.setenv("DATABASE_URL", LIVE_DB_URL)
        get_settings.cache_clear()
        try:
            from brain.db.session import create_engine_from_settings

            with pytest.raises(RuntimeError, match="LIVE"):
                create_engine_from_settings()
        finally:
            get_settings.cache_clear()

    def test_allows_the_live_database_with_the_flag(self, monkeypatch) -> None:
        monkeypatch.setenv("DATABASE_URL", LIVE_DB_URL)
        monkeypatch.setenv("BRAIN_ALLOW_LIVE", "1")
        get_settings.cache_clear()
        try:
            from brain.db.session import create_engine_from_settings

            engine = create_engine_from_settings()
            engine.dispose()
        finally:
            get_settings.cache_clear()

    def test_allows_test_urls_without_a_flag(self) -> None:
        from brain.db.session import create_engine_from_settings

        engine = create_engine_from_settings()
        engine.dispose()


class TestRunBotExportsTheFlag:
    """The bot builds a vector store and DB sessions: it must opt in."""

    def test_run_bot_exports_allow_live(self) -> None:
        script = open("scripts/run-bot.sh").read()
        assert "BRAIN_ALLOW_LIVE" in script, (
            "run-bot.sh must export BRAIN_ALLOW_LIVE=1 — without it the bot "
            "refuses to start once the construction guard exists"
        )


class TestSessionTripwire:
    def test_live_point_count_parses_qdrant_rest(self, monkeypatch) -> None:
        class _FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def read(self):
                return json.dumps({"result": {"points_count": 50}}).encode()

        import tests.live_guard as lg

        monkeypatch.setattr(lg, "urlopen", lambda *a, **k: _FakeResponse())
        assert live_point_count("http://127.0.0.1:27333") == 50

    def test_unreachable_qdrant_returns_none(self, monkeypatch) -> None:
        import tests.live_guard as lg

        def _boom(*a, **k):
            raise URLError("refused")

        monkeypatch.setattr(lg, "urlopen", _boom)
        assert live_point_count("http://127.0.0.1:1") is None

    def test_tripwire_fails_when_the_live_count_changed(self) -> None:
        with pytest.raises(AssertionError, match="LIVE"):
            assert_points_stable(before=50, after=51)

    def test_tripwire_passes_when_stable_or_unknown(self) -> None:
        assert_points_stable(before=50, after=50)
        assert_points_stable(before=None, after=51)  # qdrant was unreachable


class TestNarrowGuards:
    def test_collection_guard_refuses_only_the_live_name(self) -> None:
        with pytest.raises(RuntimeError, match="LIVE"):
            assert_not_live_collection("brain_items")
        assert_not_live_collection("brain_items_test")

    def test_database_guard_refuses_only_the_live_url(self) -> None:
        with pytest.raises(RuntimeError, match="LIVE"):
            assert_not_live_database(LIVE_DB_URL)
        assert_not_live_database(
            "postgresql+psycopg://brain:brain@localhost:27432/brain_w5_test"
        )

    def test_flag_opts_in(self) -> None:
        assert_not_live_collection(LIVE_COLLECTION, allow_live="1")
        assert_not_live_database(LIVE_DB_URL, allow_live="1")
