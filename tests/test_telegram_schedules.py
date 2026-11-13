"""Telegram side of scheduled tasks: /schedules + its buttons, and the runner
hooked into the poll loop's flush (real *_test Postgres, fake Telegram/LLM).

The runner contract: due tasks are claimed once, each runs the /ask
pipeline in a WORKER THREAD (the LLM client calls asyncio.run), under a
per-run timeout, and the ⏰ card goes to every allowed user; one task's
failure never touches another task or the loop.
"""

from __future__ import annotations

import asyncio
import threading
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import pytest
import sqlalchemy as sa

from brain.config import Settings
from brain.db.session import SessionLocal, engine
from brain.services.ask import AskResult
from brain.services.scheduled_tasks import ScheduledTaskStore
from brain.telegram_bot import (
    BOT_COMMANDS,
    CB_SCHED_RUN,
    CB_SCHED_TOGGLE,
    BotServices,
    TelegramBot,
    _flush_bot_jobs,
    _schedules_tick,
)
from conftest import assert_test_database

IL = ZoneInfo("Asia/Jerusalem")


@pytest.fixture(name="sched_db")
def sched_db_fixture(database: None) -> Iterator[None]:
    assert_test_database(str(engine.url))
    with engine.begin() as conn:
        conn.execute(sa.text("DELETE FROM scheduled_tasks"))
    yield
    with engine.begin() as conn:
        conn.execute(sa.text("DELETE FROM scheduled_tasks"))


@contextmanager
def _store() -> Iterator[ScheduledTaskStore]:
    session = SessionLocal()
    try:
        yield ScheduledTaskStore(session)
    finally:
        session.close()


class _Memory:
    def __init__(self, lang: str) -> None:
        self.lang = lang

    def user_language(self) -> str:
        return self.lang


class _Ask:
    """AskService stand-in: records the calling thread + loop state."""

    def __init__(self, answers: dict[str, Any] | None = None, block: threading.Event | None = None):
        self.answers = answers or {}
        self.block = block
        self.calls: list[dict[str, Any]] = []

    def ask(self, question: str) -> AskResult:
        try:
            asyncio.get_running_loop()
            on_loop = True
        except RuntimeError:
            on_loop = False
        self.calls.append(
            {"question": question, "thread": threading.current_thread(), "on_loop": on_loop}
        )
        if self.block is not None:
            self.block.wait(timeout=10)
        answer = self.answers.get(question, "nothing new")
        if isinstance(answer, Exception):
            raise answer
        return AskResult(answer=answer, cited_ids=[])


class _Tg:
    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []

    async def send_message(
        self, chat_id, text, parse_mode=None, reply_markup=None, disable_notification=None
    ):
        self.sent.append(
            {
                "chat_id": chat_id,
                "text": text,
                "markup": reply_markup,
                "silent": bool(disable_notification),
            }
        )


def _core(ask: _Ask | None = None, lang: str = "en") -> TelegramBot:
    ask = ask or _Ask()

    @contextmanager
    def factory():
        yield BotServices(ingest=None, items=None, search=None, ask=ask, memory=_Memory(lang))

    return TelegramBot(services_factory=factory, allowed_ids=frozenset({111, 222}), timezone=IL)


def _task(name: str = "Romi mail", prompt: str = "בדוק מיילים", **kw: Any) -> uuid.UUID:
    with _store() as store:
        return store.create(
            name=name, prompt=prompt, schedule=kw.pop("schedule", "0 7 * * 5"), **kw
        ).id


def _make_due(task_id: uuid.UUID, minutes_ago: int = 1) -> None:
    with engine.begin() as conn:
        conn.execute(
            sa.text("UPDATE scheduled_tasks SET next_run_at = :t WHERE id = :id"),
            {"t": datetime.now(tz=UTC) - timedelta(minutes=minutes_ago), "id": task_id},
        )


def _runs(task_id: uuid.UUID) -> list:
    with _store() as store:
        return store.runs(task_id)


async def _tick_and_wait(tg: _Tg, core: TelegramBot) -> None:
    await _schedules_tick(tg, core)
    if core._sched_tasks:  # noqa: SLF001
        await asyncio.wait_for(asyncio.gather(*list(core._sched_tasks)), timeout=20)  # noqa: SLF001


def _callbacks(reply) -> list[str]:
    return [data for row in reply.keyboard or [] for _, data in row]


# --- commands --------------------------------------------------------------------------


def test_schedules_is_a_registered_command():
    assert "schedules" in [name for name, _ in BOT_COMMANDS]


def test_schedules_empty(sched_db):
    reply = _core().route("/schedules", 111)
    assert "No scheduled tasks yet" in reply.text and not reply.keyboard


def test_schedules_lists_state_next_run_and_buttons(sched_db):
    active = _task("Romi <mail>")
    paused = _task("Daily digest", schedule="0 6 * * *")
    with _store() as store:
        store.set_enabled(paused, False)
        next_run = store.get(active).next_run_at.astimezone(IL)
    reply = _core().route("/schedules", 111)
    assert "1. ▶ <b>Romi &lt;mail&gt;</b> — Every Friday at 07:00" in reply.text
    assert f"Active · next: Fri {next_run.strftime('%d.%m')} 07:00" in reply.text
    assert "2. ⏸ <b>Daily digest</b> — Every day at 06:00" in reply.text
    assert "Paused" in reply.text
    labels = [label for row in reply.keyboard for label, _ in row]
    assert labels == ["1 ▶️ Run now", "1 ⏸ Pause", "2 ▶️ Run now", "2 ▶ Resume"]
    data = _callbacks(reply)
    assert data[0].startswith(f"{CB_SCHED_RUN}:") and data[1].startswith(f"{CB_SCHED_TOGGLE}:")
    assert all(len(item.encode()) < 64 for item in data)


def test_schedules_in_hebrew(sched_db):
    _task()
    reply = _core(lang="he").route("/schedules", 111)
    assert "משימות מתוזמנות" in reply.text and "כל יום שישי ב-07:00" in reply.text
    assert "פעילה" in reply.text
    assert reply.keyboard[0][0][0] == "1 ▶️ הרץ עכשיו"


def test_pause_and_resume_buttons(sched_db):
    task_id = _task()
    core = _core()
    reply = core.route("/schedules", 111)
    paused = core.handle_callback(_callbacks(reply)[1], 111)
    assert paused.edit and "⏸ Paused: Romi mail" in paused.text
    with _store() as store:
        task = store.get(task_id)
        assert not task.enabled and task.next_run_at is None
    resumed = core.handle_callback(_callbacks(paused)[1], 111)
    assert "▶ Resumed: Romi mail" in resumed.text
    with _store() as store:
        assert store.get(task_id).next_run_at is not None


def test_button_for_a_deleted_task(sched_db):
    task_id = _task()
    core = _core()
    reply = core.route("/schedules", 111)
    with _store() as store:
        store.delete(task_id)
    after = core.handle_callback(_callbacks(reply)[0], 111)
    assert "no longer exists" in after.text


def test_stale_index_is_rejected(sched_db):
    _task()
    core = _core()
    data = _callbacks(core.route("/schedules", 111))[0]
    token = data.split(":")[1]
    assert "stale" in core.handle_callback(f"{CB_SCHED_RUN}:{token}:9", 111).toast


# --- runner -----------------------------------------------------------------------------


def test_run_now_button_runs_off_the_loop_and_messages_every_user(sched_db):
    task_id = _task(prompt="מה חדש מבית הספר?")
    ask = _Ask({"מה חדש מבית הספר?": "שני מיילים חדשים"})
    core = _core(ask)
    queued = core.handle_callback(_callbacks(core.route("/schedules", 111))[0], 111)
    assert "Queued: Romi mail" in queued.text
    tg = _Tg()
    main = threading.current_thread()

    asyncio.run(_tick_and_wait(tg, core))

    assert [call["question"] for call in ask.calls] == ["מה חדש מבית הספר?"]
    assert ask.calls[0]["thread"] is not main and ask.calls[0]["on_loop"] is False
    assert [msg["chat_id"] for msg in tg.sent] == [111, 222]
    assert all(msg["silent"] for msg in tg.sent)  # a digest arrives without a sound
    text = tg.sent[0]["text"]
    assert text.startswith("⏰ <b>Romi mail</b>\n🧠 <b>Answer</b>")
    assert "שני מיילים חדשים" in text
    (run,) = _runs(task_id)
    assert run.status == "ok" and run.trigger == "manual" and run.output == "שני מיילים חדשים"
    assert core._sched_inflight == set()  # noqa: SLF001


def test_result_message_is_in_the_user_language(sched_db):
    task_id = _task()
    _make_due(task_id)
    tg = _Tg()
    asyncio.run(_tick_and_wait(tg, _core(lang="he")))
    assert tg.sent and "🧠 <b>תשובה</b>" in tg.sent[0]["text"]


def test_due_task_runs_once_across_ticks(sched_db):
    task_id = _task()
    _make_due(task_id)
    ask = _Ask()
    core = _core(ask)
    tg = _Tg()

    async def two_ticks():
        await _tick_and_wait(tg, core)
        await _tick_and_wait(tg, core)

    asyncio.run(two_ticks())
    assert len(ask.calls) == 1 and len(tg.sent) == 2  # one run, two users
    with _store() as store:
        assert store.get(task_id).next_run_at > datetime.now(tz=UTC)


def test_one_failing_task_does_not_affect_another(sched_db):
    bad = _task("Broken", prompt="boom")
    good = _task("Fine", prompt="ok?")
    _make_due(bad)
    _make_due(good)
    ask = _Ask({"boom": RuntimeError("kaput"), "ok?": "all good"})
    tg = _Tg()
    asyncio.run(_tick_and_wait(tg, _core(ask)))
    texts = [msg["text"] for msg in tg.sent if msg["chat_id"] == 111]
    failed = "failed: RuntimeError: kaput"
    assert any(t.startswith("⏰ <b>Broken</b>") and failed in t for t in texts)
    assert any(t.startswith("⏰ <b>Fine</b>") and "all good" in t for t in texts)
    rings = {msg["text"].split("\n")[0]: not msg["silent"] for msg in tg.sent}
    assert rings == {"⏰ <b>Broken</b>": True, "⏰ <b>Fine</b>": False}  # failures ring
    assert _runs(bad)[0].status == "error" and _runs(good)[0].status == "ok"


def test_flush_survives_a_broken_runner_and_still_flushes(sched_db, monkeypatch):
    core = _core()
    core._last_ping_check = time.monotonic()  # noqa: SLF001 — skip the 48h pings

    def explode(**_: Any):
        raise RuntimeError("db down")

    monkeypatch.setattr(core, "claim_scheduled", explode)
    sent_outbound: list = []
    monkeypatch.setattr(core, "drain_outbound", lambda: sent_outbound.append(1) or [])

    asyncio.run(_flush_bot_jobs(_Tg(), core))  # must not raise
    assert sent_outbound == [1]  # the rest of the flush still ran


def test_run_timeout_records_error_drops_the_late_result_and_blocks_reclaim(sched_db, monkeypatch):
    monkeypatch.setattr(
        "brain.config.get_settings",
        lambda: Settings(_env_file=None, scheduled_task_timeout_s=1),
    )
    release = threading.Event()
    ask = _Ask(block=release)
    core = _core(ask)
    task_id = _task("Slow")
    _make_due(task_id)
    tg = _Tg()

    async def scenario():
        await _tick_and_wait(tg, core)  # returns after the 1s timeout
        assert core._sched_inflight == {task_id}  # noqa: SLF001 — thread still alive
        with _store() as store:
            store.request_run(task_id)
        await _schedules_tick(tg, core)  # in-flight: not claimed again
        assert len(ask.calls) == 1
        release.set()
        for _ in range(100):
            if not core._sched_inflight:  # noqa: SLF001
                break
            await asyncio.sleep(0.05)

    asyncio.run(scenario())
    assert [msg["chat_id"] for msg in tg.sent] == [111, 222]
    assert "took longer than 1s" in tg.sent[0]["text"]
    assert not tg.sent[0]["silent"]  # a timeout rings
    runs = _runs(task_id)
    assert len(runs) == 1 and runs[0].status == "error"
    assert runs[0].error == "timed out after 1s" and runs[0].output is None
    with _store() as store:
        assert store.get(task_id).run_requested_at is not None  # still queued for later
