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
from sqlalchemy import text
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


class TestLazyEngine:
    """The guard fires when the live DB would actually be used — not on import."""

    def test_from_import_does_not_build_the_engine(self, monkeypatch) -> None:
        import brain.db.session as db_session

        monkeypatch.setattr(db_session, "_engine", None)
        monkeypatch.setattr(db_session, "_session_factory", None)
        engine_before = db_session.engine  # from-import style: binds the proxy
        assert db_session._engine is None, "attribute access on the proxy must not build"
        assert engine_before is db_session.engine

    def test_first_use_builds_and_guards(self, monkeypatch) -> None:
        import brain.db.session as db_session

        monkeypatch.setattr(db_session, "_engine", None)
        monkeypatch.setattr(db_session, "_session_factory", None)
        with db_session.SessionLocal() as session:
            session.execute(text("SELECT 1"))
        assert db_session._engine is not None

    def test_live_url_refuses_on_first_use_without_a_flag(self, monkeypatch) -> None:
        import brain.db.session as db_session

        monkeypatch.setattr(db_session, "_engine", None)
        monkeypatch.setattr(db_session, "_session_factory", None)
        monkeypatch.setenv("DATABASE_URL", LIVE_DB_URL)
        monkeypatch.delenv("BRAIN_ALLOW_LIVE", raising=False)
        get_settings.cache_clear()
        try:
            with pytest.raises(RuntimeError, match="LIVE"):
                db_session.SessionLocal()
            assert db_session._engine is None, "nothing built when refused"
        finally:
            get_settings.cache_clear()


class TestCLIDoesNotNeedTheFlagWhenHarmless:
    """A guard firing where nothing dangerous happens trains reflexive opt-ins."""

    def test_cli_help_never_builds_the_engine(self, monkeypatch, capsys) -> None:
        import brain.db.session as db_session
        from brain import cli

        monkeypatch.setattr(db_session, "_engine", None)
        monkeypatch.setattr(db_session, "_session_factory", None)
        monkeypatch.setenv("DATABASE_URL", LIVE_DB_URL)
        monkeypatch.delenv("BRAIN_ALLOW_LIVE", raising=False)
        get_settings.cache_clear()
        try:
            with pytest.raises(SystemExit) as exc_info:
                cli.main(["--help"])
            assert exc_info.value.code in (None, 0)
        finally:
            get_settings.cache_clear()
            capsys.readouterr()
        assert db_session._engine is None

    def test_eval_extraction_aggregate_only_runs_without_the_flag(
        self, monkeypatch, tmp_path, capsys
    ) -> None:
        import brain.db.session as db_session
        from brain import cli

        gold = tmp_path / "gold.jsonl"
        pred = tmp_path / "pred.jsonl"
        gold.write_text("")
        pred.write_text("")
        monkeypatch.setattr(db_session, "_engine", None)
        monkeypatch.setattr(db_session, "_session_factory", None)
        monkeypatch.setenv("DATABASE_URL", LIVE_DB_URL)
        monkeypatch.delenv("BRAIN_ALLOW_LIVE", raising=False)
        get_settings.cache_clear()
        try:
            assert cli.main([
                "eval-extraction", "--gold", str(gold), "--pred", str(pred),
                "--aggregate-only",
            ]) == 0
        finally:
            get_settings.cache_clear()
            capsys.readouterr()
        assert db_session._engine is None, "eval-extraction must never touch the DB"

    def test_db_using_command_refuses_without_the_flag(self, monkeypatch) -> None:
        import brain.db.session as db_session
        from brain import cli

        monkeypatch.setattr(db_session, "_engine", None)
        monkeypatch.setattr(db_session, "_session_factory", None)
        monkeypatch.setenv("DATABASE_URL", LIVE_DB_URL)
        monkeypatch.delenv("BRAIN_ALLOW_LIVE", raising=False)
        get_settings.cache_clear()
        try:
            with pytest.raises(RuntimeError, match="LIVE"):
                cli.main([
                    "memory", "seed-kid", "--name", "x", "--gender", "girl",
                    "--school", "s", "--grade", "1", "--class", "c", "--teacher", "t",
                ])
        finally:
            get_settings.cache_clear()

    def test_alembic_env_does_not_import_db_session(self) -> None:
        # migrations build their own engine from the URL; they must keep
        # needing only the explicit flag, not this module's shared engine
        env_py = open("src/brain/db/migrations/env.py").read()
        assert "db.session" not in env_py and "SessionLocal" not in env_py


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
