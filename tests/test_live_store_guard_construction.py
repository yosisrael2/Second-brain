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
        assert lg.live_point_count("http://127.0.0.1:27333") == 50

    def test_unreachable_qdrant_returns_none(self, monkeypatch) -> None:
        import tests.live_guard as lg

        def _boom(*a, **k):
            raise URLError("refused")

        monkeypatch.setattr(lg, "urlopen", _boom)
        assert lg.live_point_count("http://127.0.0.1:1") is None

    def test_external_live_write_is_not_a_failure(self) -> None:
        """RED (false positive, 07:17Z 24 Oct): the LIVE instance ingested a
        real note mid-suite (108 -> 109) and the count-delta guard failed the
        whole session. An external write is not test pollution — the guard
        must pass it (delta informational only)."""
        import tests.live_guard as lg

        lg.assert_no_test_points_in_live(marker_count=0)  # no markers: no failure
        note = lg.count_delta_note(before=108, after=109)
        assert note is not None and "108" in note  # delta stays visible, informational
        assert lg.count_delta_note(before=50, after=50) is None
        assert lg.count_delta_note(before=None, after=51) is None  # qdrant was down

    def test_test_origin_points_in_live_are_a_failure(self, monkeypatch) -> None:
        """The goal that must survive: a TEST writing into the live
        collection is caught — via the test-origin marker its payload
        carries, independent of the count."""
        import tests.live_guard as lg

        with pytest.raises(AssertionError, match="test-origin"):
            lg.assert_no_test_points_in_live(marker_count=1)
        lg.assert_no_test_points_in_live(marker_count=0)
        lg.assert_no_test_points_in_live(marker_count=None)  # qdrant unreachable

    def test_marker_count_parses_qdrant_count_rest(self, monkeypatch) -> None:
        """The marker count queries the LIVE collection with a payload filter
        (read-only POST /points/count) and parses result.count."""
        import tests.live_guard as lg

        captured = {}

        class _FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def read(self):
                return json.dumps({"result": {"count": 3}}).encode()

        def _fake_urlopen(request, *a, **k):
            captured["url"] = request.full_url
            captured["body"] = request.data
            return _FakeResponse()

        monkeypatch.setattr(lg, "urlopen", _fake_urlopen)
        assert lg.live_marker_point_count("http://127.0.0.1:27333") == 3
        assert captured["url"].endswith("/collections/brain_items/points/count")
        filter_body = json.loads(captured["body"])
        must = filter_body["filter"]["must"][0]
        assert must["key"] == lg.TEST_ORIGIN_KEY
        assert must["match"]["value"] == lg.TEST_ORIGIN_VALUE

    def test_marker_count_unreachable_qdrant_returns_none(self, monkeypatch) -> None:
        import tests.live_guard as lg

        def _boom(*a, **k):
            raise URLError("refused")

        monkeypatch.setattr(lg, "urlopen", _boom)
        assert lg.live_marker_point_count("http://127.0.0.1:1") is None

    def test_test_writes_carry_the_origin_marker(self) -> None:
        """Every vector a TEST writes through the real store carries the
        test-origin marker in its payload — that is what the live guard
        counts. A production write (no fixture) must stay marker-free."""
        from qdrant_client import QdrantClient

        from brain.ai.vectorstore import QdrantVectorStore

        store = QdrantVectorStore(
            client=QdrantClient(":memory:"), collection="brain_items_marker_test"
        )
        store.ensure_collection(dim=4)
        store.upsert("item-1", [0.1, 0.2, 0.3, 0.4], {"text": "hello"})
        points, _ = store._client.scroll(
            collection_name="brain_items_marker_test", with_payload=True
        )
        assert points, "the point must exist"
        import tests.live_guard as lg

        assert points[0].payload[lg.TEST_ORIGIN_KEY] == lg.TEST_ORIGIN_VALUE


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
