"""The bot and the API must build their AskService from ONE builder.

Drift incident: ``deps.get_ask_service`` passes ``top_k=settings.ask_top_k``,
``recall_pool=settings.ask_recall_pool`` and ``tz=ZoneInfo(settings.timezone)``,
but the bot factory built a bare ``AskService(search, llm, memory_block=...)``,
so bot answers ran on UTC ("Current date/time" 3h behind Jerusalem), rendered
wrong When-lines (09:00 shown as 06:00) and used default retrieval sizes.
``deps.build_ask_service`` is that single builder; both call sites must use it.
"""

from zoneinfo import ZoneInfo

from brain.config import get_settings


def _jerusalem_settings(monkeypatch):
    """Small generator-as-context-manager pinning TIMEZONE for the test."""
    monkeypatch.setenv("TIMEZONE", "Asia/Jerusalem")
    get_settings.cache_clear()
    try:
        yield get_settings()
    finally:
        get_settings.cache_clear()


class TestBuilder:
    def test_bot_factory_uses_the_shared_builder(self, monkeypatch, database):
        """The live bot services factory must get its AskService from deps."""
        from brain.api import deps
        from brain.telegram_bot import _live_services_factory

        sentinel = object()
        calls: list[dict] = []

        def spy(*args, **kwargs):
            calls.append({"args": args, "kwargs": kwargs})
            return sentinel

        # stub the shared AI clients so the factory stays offline
        monkeypatch.setattr(deps, "build_ask_service", spy)
        monkeypatch.setattr(deps, "get_embedder", lambda: object())
        monkeypatch.setattr(deps, "get_vector_store", lambda: object())
        monkeypatch.setattr(deps, "get_reranker", lambda: object())
        monkeypatch.setattr(deps, "get_llm_client", lambda: object())
        monkeypatch.setattr(deps, "get_classifier", lambda: object())
        monkeypatch.setattr(deps, "get_item_indexer", lambda: object())

        factory = _live_services_factory()
        with factory() as services:
            assert services.ask is sentinel
            assert len(calls) == 1
            # search wired positionally, memory block carried through
            assert calls[0]["kwargs"].get("memory_block") is not None

    def test_builder_uses_the_configured_timezone(self, monkeypatch):
        from brain.ai.ask import FakeLLMClient
        from brain.api.deps import build_ask_service

        for _ in _jerusalem_settings(monkeypatch):
            search = object()
            service = build_ask_service(search, FakeLLMClient(reply="x"))
            # the drift: a bare AskService defaults to UTC and renders 06:00
            assert service._tz == ZoneInfo("Asia/Jerusalem")
            assert service._top_k == get_settings().ask_top_k
            assert service._recall_pool == get_settings().ask_recall_pool

    def test_when_lines_render_in_local_time(self, monkeypatch):
        """An item at 09:00 Israel time must render '... 09:00', not 06:00."""
        from datetime import datetime
        from zoneinfo import ZoneInfo

        from brain.ai.ask import FakeLLMClient
        from brain.api.deps import build_ask_service
        from brain.db.models import MemoryItem

        for _ in _jerusalem_settings(monkeypatch):
            service = build_ask_service(object(), FakeLLMClient(reply="x"))
            item = MemoryItem(raw_text="x", status="filed")
            # aware datetime: _when_of must render it in the SERVICE's tz on
            # any machine (a naive datetime would be read in the system zone)
            item.occurred_at = datetime(2026, 10, 23, 9, 0, tzinfo=ZoneInfo("Asia/Jerusalem"))
            item.meta = {}
            when = service._when_of(item)
            assert when is not None and when.endswith("09:00"), when

    def test_api_path_delegates_to_the_builder(self, monkeypatch):
        from brain.ai.ask import FakeLLMClient
        from brain.api import deps

        sentinel = object()

        def spy(*args, **kwargs):
            assert kwargs.get("memory_block") == "BLOCK"
            return sentinel

        monkeypatch.setattr(deps, "build_ask_service", spy)
        result = deps.get_ask_service(
            search=object(), llm=FakeLLMClient(reply="x"), memory=type(
                "M", (), {"compact_block": "BLOCK"}
            )()
        )
        assert result is sentinel
