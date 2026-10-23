"""PR D1b tests: the WhatsApp poller as a managed launchd service.

Covers the four pieces of the live-start plan:
- `brain whatsapp` CLI preflight: the live-store guard REUSES
  brain.config.assert_not_live_store (PR #31, same as the API lifespan and
  'brain enrich'); permanent conditions (unconfigured store, missing store
  file, live-store refusal) exit 78 — never 1 — and `--once` runs exactly
  one poll cycle (testable seam, also useful operationally). All CLI cases
  run as SUBPROCESSES with a timeout: a bug that leaves the loop running
  fails the test red instead of hanging the suite (the process is killed;
  a pre-guard run never outlives the test).
- scripts/run-whatsapp.sh: the run-bot.sh KeepAlive exit-code contract; it
  is the ONLY place that exports BRAIN_ALLOW_LIVE=1 / WHATSAPP_BRIDGE_DB.
- scripts/service.sh: com.secondbrain.whatsapp as the third managed agent
  (install/status/restart/logs), with takeover of any manual poller
  (ownership-checked) before install.
- ops health: a 'whatsapp' component — healthy = last poll within 3x the
  poll interval AND a fresh heartbeat; BACKGROUND_LABELS covers the agent.
"""

from __future__ import annotations

import os
import sqlite3
import subprocess
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import text

from brain.ops import health as ops_health

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
REPO_ROOT = SCRIPTS.parent
VENV_BRAIN = REPO_ROOT / ".venv/bin/brain"

LIVE_URL = "postgresql+psycopg://brain:brain@localhost:27432/brain"

CLI_TIMEOUT_S = 45

need_venv = pytest.mark.skipif(not VENV_BRAIN.exists(), reason="REPO_ROOT/.venv is not provisioned")

BRIDGE_SCHEMA = """
CREATE TABLE chats (
    jid TEXT PRIMARY KEY,
    name TEXT,
    last_message_time TIMESTAMP
);
CREATE TABLE messages (
    id TEXT, chat_jid TEXT, sender TEXT, content TEXT, timestamp TIMESTAMP,
    is_from_me BOOLEAN, media_type TEXT, filename TEXT, url TEXT,
    media_key BLOB, file_sha256 BLOB, file_enc_sha256 BLOB, file_length INTEGER,
    PRIMARY KEY (id, chat_jid),
    FOREIGN KEY (chat_jid) REFERENCES chats(jid)
);
"""


def _bridge(tmp_path: Path) -> Path:
    """A one-chat synthetic bridge store (schema of the real messages.db)."""
    path = tmp_path / "messages.db"
    conn = sqlite3.connect(path)
    try:
        conn.executescript(BRIDGE_SCHEMA)
        conn.execute("INSERT INTO chats VALUES ('j1@g.us', 'Chat A', NULL)")
        conn.execute(
            "INSERT INTO messages (id, chat_jid, sender, content, timestamp, is_from_me)"
            " VALUES ('m1', 'j1@g.us', 'Rinat', 'hello', ?, 0)",
            (datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S+03:00"),),
        )
        conn.commit()
    finally:
        conn.close()
    return path


def _brain_cli(
    tmp_path: Path,
    *,
    database_url: str,
    allow_live: bool,
    bridge_db: str | None,
    extra_args: list[str] | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run `.venv/bin/brain whatsapp ...` as a bounded subprocess.

    The timeout is part of the contract: a CLI that loops when it should
    have refused fails the test red instead of hanging the suite.
    """
    env = dict(os.environ)
    env["DATABASE_URL"] = database_url
    if allow_live:
        env["BRAIN_ALLOW_LIVE"] = "1"
    else:
        env.pop("BRAIN_ALLOW_LIVE", None)
    if bridge_db is None:
        env.pop("WHATSAPP_BRIDGE_DB", None)
    else:
        env["WHATSAPP_BRIDGE_DB"] = bridge_db
    return subprocess.run(
        [str(VENV_BRAIN), "whatsapp", *(extra_args or [])],
        capture_output=True,
        text=True,
        timeout=CLI_TIMEOUT_S,
        env=env,
        cwd=str(REPO_ROOT),
    )


# --- CLI preflight (guard + permanent exit codes) -------------------------------


@need_venv
class TestWhatsappCliPreflight:
    def test_live_store_refused_without_flag(self, tmp_path):
        """Same ONE definition of 'live store' as the API and 'brain enrich'."""
        result = _brain_cli(
            tmp_path,
            database_url=LIVE_URL,
            allow_live=False,
            bridge_db=str(tmp_path / "messages.db"),
            extra_args=["--once"],
        )
        assert result.returncode == 78, result.stdout + result.stderr
        assert "live" in (result.stdout + result.stderr).lower()

    def test_unconfigured_bridge_db_is_78_never_1(self, tmp_path):
        """Unconfigured is PERMANENT (launchd must not restart-loop on it)."""
        result = _brain_cli(
            tmp_path,
            database_url=LIVE_URL,
            allow_live=True,
            bridge_db=None,
        )
        assert result.returncode == 78, result.stdout + result.stderr

    def test_missing_bridge_store_file_is_78(self, tmp_path, database):
        result = _brain_cli(
            tmp_path,
            database_url=os.environ["DATABASE_URL"],
            allow_live=True,
            bridge_db=str(tmp_path / "no-such-messages.db"),
            extra_args=["--once"],
        )
        assert result.returncode == 78, result.stdout + result.stderr

    def test_once_runs_a_single_poll(self, tmp_path, database):
        """--once: one poll cycle, then exit 0 (no loop; the testable seam).

        Live-edge semantics (PR D1): the first poll SKIPS everything at/below
        the newest existing message, so the test polls once, adds a message,
        and polls again — the second cycle must ingest exactly it.
        """
        # The allowlist lives in memory: seed the channel the bridge fixture
        # has (and clear any state a previous run of this test left behind).
        from brain.db.models import Entity
        from brain.db.session import SessionLocal

        with SessionLocal() as session:
            session.execute(text("DELETE FROM whatsapp_poll_state"))
            session.execute(text("DELETE FROM memory_items"))
            session.execute(text("DELETE FROM entities"))
            session.add(Entity(kind="channel", name="Chat A"))
            session.commit()
        bridge = _bridge(tmp_path)
        common = dict(
            database_url=os.environ["DATABASE_URL"],
            allow_live=True,
            bridge_db=str(bridge),
        )
        assert _brain_cli(tmp_path, extra_args=["--once"], **common).returncode == 0
        # A new message lands in the bridge after the first poll.
        conn = sqlite3.connect(bridge)
        try:
            conn.execute(
                "INSERT INTO messages (id, chat_jid, sender, content, timestamp, is_from_me)"
                " VALUES ('m2', 'j1@g.us', 'Dana', 'fresh', ?, 0)",
                (datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S+03:00"),),
            )
            conn.commit()
        finally:
            conn.close()
        assert _brain_cli(tmp_path, extra_args=["--once"], **common).returncode == 0
        from brain.db.session import engine

        with engine.begin() as conn:
            seen = conn.execute(
                text("SELECT messages_seen FROM whatsapp_poll_state WHERE chat_jid = 'j1@g.us'")
            ).scalar_one()
        assert seen == 1

    def test_once_still_refused_for_live_store_without_flag(self, tmp_path, database):
        """The flag is the ONLY opt-in; without it the guard wins even for --once."""
        result = _brain_cli(
            tmp_path,
            database_url=LIVE_URL,
            allow_live=False,
            bridge_db=str(_bridge(tmp_path)),
            extra_args=["--once"],
        )
        assert result.returncode == 78, result.stdout + result.stderr


# --- run-whatsapp.sh (KeepAlive exit-code contract + env exports) ----------------


@need_venv
class TestRunWhatsappWrapper:
    def _run_wrapper(self, tmp_path: Path, brain_rc: int) -> subprocess.CompletedProcess[str]:
        fake_bin = tmp_path / f"bin-{brain_rc}"
        fake_bin.mkdir(exist_ok=True)
        fake_brain = fake_bin / "brain"
        fake_brain.write_text(
            f"#!/usr/bin/env bash\necho fake-poller rc={brain_rc}\nexit {brain_rc}\n"
        )
        fake_brain.chmod(0o755)
        return subprocess.run(
            [str(SCRIPTS / "run-whatsapp.sh")],
            capture_output=True,
            text=True,
            timeout=60,
            env={"BRAIN_BIN": str(fake_brain), "PATH": os.environ["PATH"]},
        )

    @pytest.mark.parametrize("brain_rc,expected", [(0, 0), (78, 0), (1, 1), (70, 70)])
    def test_exit_code_mapping(self, tmp_path, brain_rc, expected):
        result = self._run_wrapper(tmp_path, brain_rc)
        assert result.returncode == expected, result.stdout + result.stderr

    def test_wrapper_preserves_the_real_code_in_its_log_line(self, tmp_path):
        result = self._run_wrapper(tmp_path, 78)
        assert "exited 78" in result.stdout

    def test_wrapper_exports_allow_live_and_bridge_db(self, tmp_path):
        """BRAIN_ALLOW_LIVE=1 and WHATSAPP_BRIDGE_DB come from THIS script only."""
        fake_bin = tmp_path / "bin-env"
        fake_bin.mkdir()
        fake_brain = fake_bin / "brain"
        fake_brain.write_text(
            "#!/usr/bin/env bash\n"
            'echo "ALLOW: ${BRAIN_ALLOW_LIVE-unset}"; echo "DB: ${WHATSAPP_BRIDGE_DB-unset}"\n'
        )
        fake_brain.chmod(0o755)
        result = subprocess.run(
            [str(SCRIPTS / "run-whatsapp.sh")],
            capture_output=True,
            text=True,
            timeout=60,
            env={
                "BRAIN_BIN": str(fake_brain),
                "PATH": os.environ["PATH"],
                "WHATSAPP_BRIDGE_DB": "/some/bridge/messages.db",
            },
        )
        assert "ALLOW: 1" in result.stdout
        assert "DB: /some/bridge/messages.db" in result.stdout


# --- service.sh: third managed agent --------------------------------------------


class _LaunchctlStub:
    def __init__(self, tmp_path: Path, print_state: str = "running") -> None:
        self.bin = tmp_path / "stubbin"
        self.bin.mkdir(exist_ok=True)
        self.log = tmp_path / "launchctl-calls.log"
        stub = self.bin / "launchctl"
        stub.write_text(
            "#!/usr/bin/env bash\n"
            f'echo "$@" >> {self.log}\n'
            'case "$1" in\n'
            f'  print) printf "  state = {print_state}\\n  pid = 4242\\n'
            f'  last exit code = 0\\n";;\n'
            "  *) exit 0;;\n"
            "esac\n"
        )
        stub.chmod(0o755)

    def env(self) -> dict[str, str]:
        return {"PATH": f"{self.bin}:{os.environ['PATH']}"}


def _service_env(tmp_path: Path, extra: dict[str, str] | None = None) -> dict[str, str]:
    env = _LaunchctlStub(tmp_path).env()
    env.update(
        {
            "PLIST_DIR": str(tmp_path / "LaunchAgents"),
            "LOG_DIR": str(tmp_path / "Logs"),
        }
    )
    if extra:
        env.update(extra)
    return env


def _service(
    tmp_path: Path, *args: str, env_extra: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, **_service_env(tmp_path, env_extra)}
    return subprocess.run(
        [str(SCRIPTS / "service.sh"), *args],
        capture_output=True,
        text=True,
        timeout=120,
        env=env,
    )


@need_venv
class TestServiceWhatsappAgent:
    def test_install_writes_the_whatsapp_plist(self, tmp_path):
        result = _service(tmp_path, "install")
        assert result.returncode == 0, result.stdout + result.stderr
        wa = tmp_path / "LaunchAgents" / "com.secondbrain.whatsapp.plist"
        assert wa.exists(), result.stdout
        body = wa.read_text()
        assert "<string>com.secondbrain.whatsapp</string>" in body
        assert str(SCRIPTS / "run-whatsapp.sh") in body
        assert "<key>SuccessfulExit</key>" in body and "<false/>" in body
        assert "<integer>30</integer>" in body  # ThrottleInterval 30
        # Secrets stay out of the plist (values AND variable names).
        assert "TELEGRAM_BOT_TOKEN" not in body
        assert "WHATSAPP_BRIDGE_DB" not in body
        calls = (tmp_path / "launchctl-calls.log").read_text()
        assert any("bootstrap" in c and "com.secondbrain.whatsapp" in c for c in calls.splitlines())

    def test_status_restart_and_logs_cover_the_whatsapp_agent(self, tmp_path):
        assert "com.secondbrain.whatsapp" in _service(tmp_path, "status").stdout
        result = _service(tmp_path, "restart")
        assert result.returncode == 0
        calls = (tmp_path / "launchctl-calls.log").read_text()
        assert "com.secondbrain.whatsapp" in calls
        assert "whatsapp" in _service(tmp_path, "logs", "whatsapp").stdout

    def test_install_takes_over_a_manual_poller(self, tmp_path):
        """A manual poller of OURS (command + cwd owned) is stopped before install.

        The fake process fakes argv[0] to 'brain whatsapp' (exec -a) and runs
        with cwd = this repo, exactly what is_our_poller requires; the
        ownership check is what keeps other clones' processes safe.
        """
        proc = subprocess.Popen(
            ["/bin/bash", "-c", 'exec -a "brain whatsapp" /bin/sleep 60'],
            cwd=str(REPO_ROOT),
        )
        try:
            time.sleep(0.4)
            assert proc.poll() is None  # the fake poller is alive
            result = _service(tmp_path, "install")
            assert result.returncode == 0, result.stdout + result.stderr
            assert "takeover" in result.stdout
            proc.wait(timeout=10)  # stopped by the takeover
            assert proc.returncode != 0  # SIGTERM, not a clean exit
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait()


# --- ops health: the 'whatsapp' component ----------------------------------------


def _healthy_whatsapp(**overrides: object) -> dict[str, object]:
    raw: dict[str, object] = {
        "configured": True,
        "interval_s": 30.0,
        "heartbeat_age_s": 5.0,
        "last_poll_age_s": 5.0,
        "channels": 2,
    }
    raw.update(overrides)
    return raw


class TestWhatsappHealthComponent:
    def _payload(self, whatsapp_raw: dict[str, object]) -> dict[str, object]:
        ops_health.reset_cache()
        ctx = ops_health.HealthContext(
            db=lambda: {
                "reachable": True,
                "latency_ms": 1.0,
                "drift": False,
                "items_by_status": {},
                "size": "1 kB",
            },
            qdrant=lambda: {
                "reachable": True,
                "collection_exists": True,
                "vectors": 0,
                "items": 0,
                "dims_ok": True,
            },
            embedder=lambda: {"provider": "fastembed", "model": None, "loaded": False},
            jev=lambda: {"configured": False},
            llm=lambda: {"provider": "claude_sdk", "model": None},
            telegram=lambda: {"token_set": False, "allowlist_set": False, "heartbeat_age_s": None},
            docker=lambda: {"available": False},
            disk=lambda: {"free_gb": 42.0},
            whatsapp=lambda: whatsapp_raw,
        )
        return ops_health.run_health_checks(ctx)

    def test_component_present_and_ok_when_fresh(self):
        body = self._payload(_healthy_whatsapp())
        assert "whatsapp" in body["components"]
        entry = body["components"]["whatsapp"]
        assert entry["status"] == "ok"

    def test_unconfigured_is_disabled_not_down(self):
        entry = self._payload({"configured": False})["components"]["whatsapp"]
        assert entry["status"] == "disabled"

    def test_within_three_intervals_is_ok(self):
        """The manager's rule: healthy = last poll within 3x the interval."""
        entry = self._payload(_healthy_whatsapp(last_poll_age_s=75.0, heartbeat_age_s=75.0))
        assert entry["components"]["whatsapp"]["status"] == "ok"

    def test_stale_poll_is_down(self):
        entry = self._payload(_healthy_whatsapp(last_poll_age_s=300.0, heartbeat_age_s=300.0))[
            "components"
        ]["whatsapp"]
        assert entry["status"] == "down"

    def test_no_heartbeat_is_down(self):
        entry = self._payload(_healthy_whatsapp(heartbeat_age_s=None, last_poll_age_s=None))[
            "components"
        ]["whatsapp"]
        assert entry["status"] == "down"

    def test_background_labels_cover_the_whatsapp_agent(self):
        assert "com.secondbrain.whatsapp" in ops_health.BACKGROUND_LABELS


class TestSideProcessIndexerWiring:
    """'brain whatsapp' is a side process: it must poll with the REAL vector
    indexer, not indexer=None (the D1b design bug — polled conversations were
    invisible to semantic search until a manual 'brain reindex')."""

    def test_side_process_indexer_returns_the_deps_indexer(self, monkeypatch):
        import brain.cli as cli
        from brain.api import deps

        sentinel = object()
        monkeypatch.setattr(deps, "get_item_indexer", lambda: sentinel)
        assert cli._side_process_indexer() is sentinel

    def test_side_process_indexer_returns_none_when_deps_unavailable(self, monkeypatch):
        import brain.cli as cli

        def _boom():
            raise RuntimeError("no embedder here")

        monkeypatch.setattr("brain.api.deps.get_item_indexer", _boom)
        assert cli._side_process_indexer() is None

    def test_cli_attachment_service_forwards_the_indexer(self, monkeypatch):
        """Attachment items captured by the poller path are indexed by the
        attachments service itself — but only if the CLI hands it the indexer."""
        import brain.cli as cli

        captured: dict = {}

        class _RecordingService:
            def __init__(self, **kwargs):
                captured.update(kwargs)

        monkeypatch.setattr("brain.services.attachments.AttachmentService", _RecordingService)
        monkeypatch.setattr("brain.services.attachments.AttachmentFileStore", lambda root: None)
        monkeypatch.setattr("brain.services.attachments.PdfTextLayerReader", lambda: None)
        monkeypatch.setattr("brain.services.attachments.ClaudeFileReader", lambda: None)
        cli._cli_attachment_service(
            SimpleNamespace(files_dir=None, attachment_daily_budget=5), indexer="IDX"
        )
        assert captured["indexer"] == "IDX"

    def test_whatsapp_cli_indexes_through_the_seam(self, tmp_path, database, monkeypatch):
        """End to end, bounded: two --once cycles through cli.main with the
        indexer seam stubbed — the created conversation reaches the indexer."""
        import brain.cli as cli
        from brain.config import get_settings
        from brain.db.models import Entity
        from brain.db.session import SessionLocal

        class _RecordingIndexer:
            def __init__(self):
                self.indexed = []

            def index(self, item):
                self.indexed.append(item)
                return True

        fake = _RecordingIndexer()
        monkeypatch.setattr(cli, "_side_process_indexer", lambda: fake)
        with SessionLocal() as session:
            session.execute(text("DELETE FROM whatsapp_poll_state"))
            session.execute(text("DELETE FROM memory_items"))
            session.execute(text("DELETE FROM entities"))
            session.add(Entity(kind="channel", name="Chat A"))
            session.commit()
        bridge = _bridge(tmp_path)
        monkeypatch.setenv("BRAIN_ALLOW_LIVE", "1")
        monkeypatch.setenv("WHATSAPP_BRIDGE_DB", str(bridge))
        get_settings.cache_clear()
        try:
            assert cli.main(["whatsapp", "--once"]) == 0  # init cycle
            conn = sqlite3.connect(bridge)
            try:
                conn.execute(
                    "INSERT INTO messages (id, chat_jid, sender, content, timestamp, is_from_me)"
                    " VALUES ('m2', 'j1@g.us', 'Dana', 'fresh', ?, 0)",
                    # strictly later than m1 (the fixture label drops microseconds)
                    (
                        (datetime.now(UTC) + timedelta(seconds=70)).strftime(
                            "%Y-%m-%d %H:%M:%S+03:00"
                        ),
                    ),
                )
                conn.commit()
            finally:
                conn.close()
            assert cli.main(["whatsapp", "--once"]) == 0  # ingest cycle
        finally:
            get_settings.cache_clear()
        assert len(fake.indexed) == 1
        assert fake.indexed[0].source == "whatsapp"
