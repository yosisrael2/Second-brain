"""Repro tests for the 4 manager-review MAJORS on the Gmail SB/ labeler.

Each test first FAILED on origin/main (26f77ea) for the stated reason:

  M1  ``--max-apply -1`` applied past the cap — the ``plans[:max_apply] if
      max_apply >= 0 else list(plans)`` branch made -1 (and any negative)
      UNLIMITED, and argparse accepted anything integer;
  M2  ``--auth`` wrote the labeler token into the SYNC token's keychain item
      (service ``second-brain-gmail``) and stored a consent without ANY
      account check, so consenting the wrong Google account silently
      relabeled the wrong mailbox; ``--apply`` fell back to the sync token;
  M3  the 7-day ops prune deleted the labeler's audit rows — the undo
      ledger — silently breaking ``--undo`` for batches older than 7 days;
  M4  the audit row was written AFTER the Gmail modify call, so a failed
      audit write still let the label through (unaudited writes) and a
      crash mid-batch left rows that could not be told apart.

Fakes only: no Google (fake service/flow/profile seam), no real keychain (a
recording fake), no real ``security``. The DB is this worker's
brain_w21_labeler_test via the ``database`` fixture; the Qdrant collection is
brain_items_w21_labeler_test (derived by conftest).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy import text
from tests.test_connectors_gmail import LABEL, _ServiceState

# The SYNC token's keychain item (services.keychain.SERVICE). The labeler
# must never read or write it.
SYNC_SERVICE = "second-brain-gmail"
# The labeler's OWN item (the fix's contract: a DISTINCT service name).
LABELER_SERVICE = "second-brain-gmail-labeler"

TEST_DB_URL = "postgresql+psycopg://brain:brain@localhost:27432/brain_w21_labeler_test"
TEST_COLLECTION = "brain_items_w21_labeler_test"


def _client(state: _ServiceState) -> Any:
    from brain.ai.gmail import RestGmailClient

    return RestGmailClient(
        token_loader=lambda: "refresh-token",
        service_factory=lambda token, user_id: _FakeServiceShim(state),
        min_interval_s=0.0,
        max_retries=0,
    )


class _FakeServiceShim:
    """Adapts the test_connectors_gmail fake to RestGmailClient's factory
    signature (token, user_id)."""

    def __init__(self, state: _ServiceState) -> None:
        self._state = state

    def users(self) -> Any:
        from tests.test_connectors_gmail import _FakeUsers

        return _FakeUsers(self._state)


class _ModifySpy:
    """Delegating client wrapper that snapshots the labeler's audit rows for
    a message AT THE MOMENT its Gmail modify call fires (write-ahead proof)."""

    def __init__(self, inner: Any) -> None:
        self._inner = inner
        self.states_at_modify: dict[str, list[str]] = {}
        self.modify_calls: list[tuple[str, dict]] = []

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    def modify_message_labels(self, message_id: str, *, add=(), remove=()) -> None:
        self.states_at_modify[message_id] = _audit_states(message_id)
        self.modify_calls.append((message_id, {"add": list(add), "remove": list(remove)}))
        return self._inner.modify_message_labels(message_id, add=add, remove=remove)


class _FailOnModify:
    """Delegating client wrapper whose modify raises for chosen message ids
    (per-message failure scripting the shared fake does not support)."""

    def __init__(self, inner: Any, fail_ids: set[str], exc: Exception) -> None:
        self._inner = inner
        self._fail_ids = fail_ids
        self._exc = exc

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    def modify_message_labels(self, message_id: str, *, add=(), remove=()) -> None:
        if message_id in self._fail_ids:
            raise self._exc
        return self._inner.modify_message_labels(message_id, add=add, remove=remove)


def _audit_states(gmail_id: str, batch: str | None = None) -> list[str]:
    """The state values of the labeler's apply rows for one message."""
    from brain.db.session import engine

    sql = (
        "SELECT detail->>'state' FROM ops_events WHERE component = 'gmail-label' "
        "AND kind = 'apply' AND detail->>'gmail_id' = :g"
    )
    params: dict[str, Any] = {"g": gmail_id}
    if batch is not None:
        sql += " AND detail->>'batch' = :b"
        params["b"] = batch
    with engine.connect() as conn:
        return [row[0] for row in conn.execute(text(sql), params).all()]


def _insert_apply_row(batch: str, gmail_id: str, state: str, *, label_id: str = "L_1") -> None:
    """One labeler apply audit row with an explicit detail state."""
    from brain.db.session import engine

    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO ops_events (component, kind, ok, detail) "
                "VALUES ('gmail-label', 'apply', true, CAST(:d AS jsonb))"
            ),
            {"d": json.dumps(
                {"batch": batch, "gmail_id": gmail_id, "label_path": "SB/School",
                 "label_id": label_id, "source": "rules", "state": state}
            )},
        )


def _clear_labeler_rows() -> None:
    from brain.db.session import engine

    with engine.begin() as conn:
        conn.execute(text("DELETE FROM ops_events WHERE component = 'gmail-label'"))


def _msg_with_headers(mid: str, *, sender: str, subject: str) -> dict:
    from tests.test_connectors_gmail import _msg_fixture

    raw = _msg_fixture(mid)
    raw["labelIds"] = [LABEL]
    headers = raw["payload"]["headers"]
    headers[:] = [h for h in headers if h["name"] not in ("From", "Subject")]
    headers.append({"name": "From", "value": sender})
    headers.append({"name": "Subject", "value": subject})
    return raw


def _seed_state(count: int) -> _ServiceState:
    state = _ServiceState()
    state.profile_history_id = "9002"
    for i in range(count):
        mid = f"m{i:02d}"
        state.messages[mid] = _msg_with_headers(
            mid, sender="office@school.example", subject=f"trip {i}"
        )
    state.list_result = {"messages": [{"id": f"m{i:02d}"} for i in range(count)]}
    return state


def _rules_file(tmp_path: Any) -> str:
    path = tmp_path / "rules.json"
    path.write_text(json.dumps({"School": {"domains": ["school.example"], "list_ids": []}}))
    return str(path)


class _MajorsSettings:
    """Just the fields the gmail-label paths read (this worker's test store)."""

    def __init__(self, rules_path: str = "", gmail_account: str | None = None,
                 credentials_path: str = "/tmp/client-secret.json") -> None:
        self.gmail_labeler_rules_path = rules_path
        self.gmail_label_id = LABEL
        self.gmail_query = ""
        self.gmail_min_call_interval_s = 0.0
        self.gmail_account = gmail_account
        self.gmail_credentials_path = credentials_path
        self.database_url = TEST_DB_URL
        self.qdrant_collection = TEST_COLLECTION


class _RecordingKeychain:
    """Signature-compatible Keychain stand-in: records every store per
    (service, account), serves preset reads — the real `security` never
    runs. Class-level ledger so asserts see every instance the CLI makes."""

    ledger: list[tuple[str, str, str]] = []
    items: dict[tuple[str, str], str] = {}

    def __init__(self, runner: Any = None, service: str = "second-brain-gmail") -> None:
        self._service = service

    def store(self, account: str, secret: str) -> None:
        _RecordingKeychain.ledger.append((self._service, account, secret))
        _RecordingKeychain.items[(self._service, account)] = secret

    def read(self, account: str) -> str | None:
        return _RecordingKeychain.items.get((self._service, account))

    def delete(self, account: str) -> None:
        _RecordingKeychain.items.pop((self._service, account), None)

    @classmethod
    def reset(cls, items: dict[tuple[str, str], str] | None = None) -> None:
        cls.ledger = []
        cls.items = dict(items or {})


def _patch_keychain(monkeypatch: pytest.MonkeyPatch,
                    items: dict[tuple[str, str], str] | None = None) -> None:
    import brain.services.keychain as keychain_mod

    _RecordingKeychain.reset(items)
    monkeypatch.setattr(keychain_mod, "Keychain", _RecordingKeychain)


def _patch_profile_email(monkeypatch: pytest.MonkeyPatch,
                         result: Any) -> None:
    """Patch the labeler's post-consent profile seam when the checkout has
    one (the fix). On origin/main there is NO profile call at all — the
    storage asserts below still go red for the stated reason."""
    import brain.cli as cli

    if hasattr(cli, "_labeler_profile_email"):
        monkeypatch.setattr(cli, "_labeler_profile_email", result)


def _patch_flow(
    monkeypatch: pytest.MonkeyPatch, token: str = "1//fresh-labeler-token"
) -> list[str]:
    """Replace _build_oauth_flow with a fake consent (no browser, no Google);
    returns the list of lifecycle events it went through."""
    import brain.cli as cli

    events: list[str] = []

    class _FakeFlow:
        def __init__(self) -> None:
            self.credentials = SimpleNamespace(
                refresh_token=token, token_uri="https://oauth2.googleapis.com/token",
                client_id="cid", client_secret="sek",
            )

        def run_local_server(self, port: int = 0) -> None:
            events.append("run")

    def fake_build(path: str, scopes: list[str]) -> _FakeFlow:
        events.append("build")
        return _FakeFlow()

    monkeypatch.setattr(cli, "_build_oauth_flow", fake_build)
    return events


def _granted_scope(monkeypatch: pytest.MonkeyPatch) -> None:
    """The consent gate passes (a modify-capable granted scope list); the
    REAL _labeler_token stays wired (keychain-backed)."""
    import brain.cli as cli
    import brain.services.gmail_labeler as labeler_mod

    monkeypatch.setattr(
        cli, "_labeler_client_config",
        lambda settings: {"client_id": "c", "client_secret": "s",
                          "token_uri": "https://oauth2.googleapis.com/token"},
    )
    real_check = labeler_mod.token_grants_modify

    def _checked(token: str, client_config: dict, poster: Any = None) -> bool:
        return real_check(token, client_config=client_config, poster=lambda f: {
            "scope": "https://www.googleapis.com/auth/gmail.readonly "
                     "https://www.googleapis.com/auth/gmail.modify"
        })

    monkeypatch.setattr(labeler_mod, "token_grants_modify", _checked)


def _granted_gate(monkeypatch: pytest.MonkeyPatch) -> None:
    """_granted_scope plus a stubbed _labeler_token (no keychain at all)."""
    import brain.cli as cli

    _granted_scope(monkeypatch)
    monkeypatch.setattr(cli, "_labeler_token", lambda: "1//tok")
    """The consent gate passes (a modify-capable granted scope list)."""
    import brain.cli as cli
    import brain.services.gmail_labeler as labeler_mod

    monkeypatch.setattr(cli, "_labeler_token", lambda: "1//tok")
    monkeypatch.setattr(
        cli, "_labeler_client_config",
        lambda settings: {"client_id": "c", "client_secret": "s",
                          "token_uri": "https://oauth2.googleapis.com/token"},
    )
    real_check = labeler_mod.token_grants_modify

    def _checked(token: str, client_config: dict, poster: Any = None) -> bool:
        return real_check(token, client_config=client_config, poster=lambda f: {
            "scope": "https://www.googleapis.com/auth/gmail.readonly "
                     "https://www.googleapis.com/auth/gmail.modify"
        })

    monkeypatch.setattr(labeler_mod, "token_grants_modify", _checked)


def _plan_labels(state: _ServiceState, tmp_path: Any, count: int) -> list[Any]:
    from brain.services.gmail_labeler import load_rules, plan_labels

    for i in range(count):
        mid = f"m{i:02d}"
        state.messages[mid] = _msg_with_headers(mid, sender="office@school.example",
                                                subject=f"trip {i}")
    state.list_result = {"messages": [{"id": f"m{i:02d}"} for i in range(count)]}
    client = _client(state)
    return plan_labels(
        client, rules=load_rules(_rules_file(tmp_path)), kid_names=[],
        label_id=LABEL, query="",
    )


class TestM1MaxApplyCap:
    def test_service_rejects_max_apply_below_one(self, tmp_path, database):
        """M1 (defense in depth): apply_labels refuses max_apply < 1 with
        ValueError and writes nothing. On main, 0 silently applied nothing
        and -1 applied EVERYTHING (the 'unlimited' branch)."""
        from brain.services.gmail_labeler import apply_labels

        state = _ServiceState()
        plans = _plan_labels(state, tmp_path, 3)
        client = _client(state)
        for bad in (0, -1):
            with pytest.raises(ValueError):
                apply_labels(client, plans, max_apply=bad, batch_id="b1")
        writes = [c for c in state.calls if c[0] == "messages.modify"]
        assert writes == [], "a rejected max_apply must leave zero write calls"

    @pytest.mark.parametrize("bad", ["-1", "0", "101", "abc"])
    def test_cli_refuses_out_of_range_max_apply(self, bad, tmp_path, monkeypatch, capsys):
        """M1: argparse refuses --max-apply outside 1..100 (exit 2) before
        ANY Gmail call. On main, -1 applied every planned label and 101 was
        accepted verbatim."""
        import brain.cli as cli

        state = _ServiceState()
        monkeypatch.setattr("brain.config.get_settings",
                            lambda: _MajorsSettings(rules_path=_rules_file(tmp_path)))
        monkeypatch.setattr(cli, "_labeler_client",
                            lambda settings, modify=False: _client(state))
        _granted_gate(monkeypatch)
        with pytest.raises(SystemExit) as exc:
            cli.main(["gmail-label", "--apply", "--max-apply", bad])
        assert exc.value.code == 2, "out-of-range --max-apply must be an argparse error (exit 2)"
        writes = [c for c in state.calls if c[0] in ("messages.modify", "labels.create")]
        assert writes == [], "a refused --max-apply must leave zero write calls"

    def test_cli_accepts_the_range_edges(self, tmp_path, monkeypatch, capsys, database):
        """Sibling inputs 1 and 100 are accepted (apply runs, capped). Each
        value runs on a FRESH fake state (the shared fake's created label
        ids are ints — a second plan pass would trip on them, a fake
        artifact real Gmail string ids never hit)."""
        import brain.cli as cli

        monkeypatch.setattr("brain.config.get_settings",
                            lambda: _MajorsSettings(rules_path=_rules_file(tmp_path)))
        _granted_gate(monkeypatch)
        for value, expected in (("1", 1), ("100", 3)):
            state = _ServiceState()
            plans = _plan_labels(state, tmp_path, 3)
            monkeypatch.setattr(cli, "_labeler_client",
                                lambda settings, modify=False, _s=state: _client(_s))
            code = cli.main(["gmail-label", "--apply", "--max-apply", value])
            assert code == 0
            modifies = [c for c in state.calls if c[0] == "messages.modify"]
            assert len(modifies) == min(int(value), len(plans)) == expected


class TestM2OwnKeychainItem:
    def test_auth_writes_the_labeler_item_never_the_sync_item(
        self, monkeypatch, capsys
    ):
        """M2 repro (b): a matching consent stores the token in the
        LABELER'S OWN keychain item and NEVER in the sync token's item. On
        main, --auth stored into second-brain-gmail (the sync item)."""
        import brain.cli as cli

        monkeypatch.setattr(
            "brain.config.get_settings",
            lambda: _MajorsSettings(gmail_account="owner@gmail.com"),
        )
        _patch_flow(monkeypatch)
        _patch_profile_email(monkeypatch, lambda credentials: "  Owner@Gmail.Com ")
        _patch_keychain(monkeypatch)
        code = cli.main(["gmail-label", "--auth"])
        assert code == 0
        sync_stores = [e for e in _RecordingKeychain.ledger if e[0] == SYNC_SERVICE]
        assert sync_stores == [], (
            "--auth must never write the sync token's keychain item "
            f"{SYNC_SERVICE!r} (wrote {sync_stores!r})"
        )
        labeler_stores = [e for e in _RecordingKeychain.ledger if e[0] == LABELER_SERVICE]
        assert [e[2] for e in labeler_stores] == ["1//fresh-labeler-token"], (
            "the labeler token goes in its OWN keychain item"
        )

    def test_auth_account_mismatch_stores_nothing(self, monkeypatch, capsys):
        """M2 repro (b): a consent for a DIFFERENT account is refused —
        nothing stored, both addresses printed, non-zero exit."""
        import brain.cli as cli

        monkeypatch.setattr(
            "brain.config.get_settings",
            lambda: _MajorsSettings(gmail_account="owner@gmail.com"),
        )
        _patch_flow(monkeypatch)
        _patch_profile_email(monkeypatch, lambda credentials: "attacker@gmail.com")
        _patch_keychain(monkeypatch)
        code = cli.main(["gmail-label", "--auth"])
        assert code != 0, "a mismatched consent must not be accepted"
        captured = capsys.readouterr()
        assert "owner@gmail.com" in captured.out + captured.err
        assert "attacker@gmail.com" in captured.out + captured.err, (
            "both addresses must be printed on a mismatch"
        )
        assert _RecordingKeychain.ledger == [], "a mismatched consent stores NOTHING"

    def test_auth_unset_account_refuses_before_any_consent(
        self, monkeypatch, capsys
    ):
        """M2: gmail_account unset → refusal BEFORE the browser flow, the
        message names the env var, nothing stored."""
        import brain.cli as cli

        monkeypatch.setattr(
            "brain.config.get_settings",
            lambda: _MajorsSettings(gmail_account=None),
        )
        events = _patch_flow(monkeypatch)
        _patch_keychain(monkeypatch)
        code = cli.main(["gmail-label", "--auth"])
        assert code != 0
        assert "GMAIL_ACCOUNT" in capsys.readouterr().err, (
            "the refusal must name the GMAIL_ACCOUNT env var"
        )
        assert events == [], "the refusal must happen BEFORE any OAuth flow"
        assert _RecordingKeychain.ledger == [], "nothing stored"

    def test_auth_get_profile_error_stores_nothing(self, monkeypatch, capsys):
        """M2: a getProfile error/timeout refuses to store the token."""
        import brain.cli as cli

        monkeypatch.setattr(
            "brain.config.get_settings",
            lambda: _MajorsSettings(gmail_account="owner@gmail.com"),
        )
        _patch_flow(monkeypatch)

        def _boom(credentials: Any) -> str:
            raise RuntimeError("getProfile timeout")

        _patch_profile_email(monkeypatch, _boom)
        _patch_keychain(monkeypatch)
        code = cli.main(["gmail-label", "--auth"])
        assert code != 0, "a failed profile check must refuse to store"
        assert _RecordingKeychain.ledger == [], "nothing stored on a profile error"

    def test_apply_refuses_when_the_labeler_token_item_is_missing(
        self, tmp_path, monkeypatch, capsys
    ):
        """M2: --apply (and the whole labeler path) reads ONLY the labeler's
        own keychain item — a missing item is a refusal (exit 78) with zero
        Gmail calls, NEVER a fallback to the sync token's item."""
        import brain.cli as cli

        state = _ServiceState()
        monkeypatch.setattr(
            "brain.config.get_settings",
            lambda: _MajorsSettings(rules_path=_rules_file(tmp_path)),
        )
        monkeypatch.setattr(cli, "_labeler_client",
                            lambda settings, modify=False: _client(state))
        # The SYNC item has a token; the labeler item does not exist. The
        # REAL _labeler_token decides (never the sync item, never a
        # fallback), so only the scope/config seams are stubbed here.
        _patch_keychain(monkeypatch, {(SYNC_SERVICE, "default"): "1//sync-token"})
        _granted_scope(monkeypatch)
        with pytest.raises(SystemExit) as exc:
            cli.main(["gmail-label", "--apply"])
        assert exc.value.code == 78, (
            "a missing labeler token must refuse (exit 78), never fall back "
            "to the sync token's item"
        )
        assert state.calls == [], "the refusal must happen before ANY Gmail call"
        assert "second-brain-gmail-labeler" in capsys.readouterr().err, (
            "the refusal message must name the labeler's own keychain item"
        )

    def test_settings_gmail_account_field_and_env(self, monkeypatch):
        """The Settings field exists and GMAIL_ACCOUNT feeds it (same env
        convention as the other gmail_* settings)."""
        from brain.config import get_settings

        monkeypatch.setenv("GMAIL_ACCOUNT", "owner@gmail.com")
        get_settings.cache_clear()
        try:
            assert get_settings().gmail_account == "owner@gmail.com"
        finally:
            get_settings.cache_clear()


class TestM3PruneExemption:
    def test_prune_keeps_labeler_audit_rows_past_retention(self, database):
        """M3 repro (c): a labeler audit row older than 7 days SURVIVES the
        prune (it is the undo ledger); an ordinary old ops event is still
        deleted. On main, the prune deleted both."""
        from brain.db.session import engine
        from brain.ops import prune_events

        with engine.begin() as conn:
            conn.execute(text("DELETE FROM ops_events WHERE component IN "
                              "('gmail-label', 'paste')"))
            old = datetime.now(UTC) - timedelta(days=8)
            conn.execute(
                text(
                    "INSERT INTO ops_events (ts, component, kind, ok, detail) "
                    "VALUES (:ts, 'gmail-label', 'apply', true, CAST(:d AS jsonb))"
                ),
                {"ts": old, "d": json.dumps(
                    {"batch": "oldbatch12345", "gmail_id": "mOld",
                     "label_id": "L_1", "state": "applied"})},
            )
            conn.execute(
                text(
                    "INSERT INTO ops_events (ts, component, kind, ok, detail) "
                    "VALUES (:ts, 'paste', 'step', true, '{}'::jsonb)"
                ),
                {"ts": old},
            )
        prune_events()
        with engine.connect() as conn:
            labeler_rows = conn.execute(
                text("SELECT count(*) FROM ops_events WHERE component = 'gmail-label'")
            ).scalar_one()
            ordinary_rows = conn.execute(
                text("SELECT count(*) FROM ops_events WHERE component = 'paste'")
            ).scalar_one()
        assert labeler_rows == 1, (
            "the labeler's audit rows are the undo ledger — the 7-day prune "
            "must exempt them"
        )
        assert ordinary_rows == 0, (
            "the prune must still delete ordinary old ops events (regression)"
        )


class TestM4WriteAheadAudit:
    def test_intent_row_is_committed_before_the_modify_call(self, tmp_path, database):
        """M4 repro (d): the intent audit row is COMMITTED before the Gmail
        modify call fires. On main, the row was written only AFTER the call
        (and a failed write still let the label through)."""
        from brain.services.gmail_labeler import apply_labels

        state = _ServiceState()
        plans = _plan_labels(state, tmp_path, 1)
        spy = _ModifySpy(_client(state))
        apply_labels(spy, plans, max_apply=1, batch_id="wa1")
        assert spy.states_at_modify["m00"] == ["intent"], (
            "the intent row must be committed BEFORE the Gmail modify call "
            f"(at call time the rows were {spy.states_at_modify.get('m00')!r})"
        )
        assert _audit_states("m00") == ["applied"], (
            "after the modify the row reads state=applied"
        )

    def test_failed_audit_write_means_the_label_is_not_applied(
        self, tmp_path, database, monkeypatch
    ):
        """M4 repro (d): when the write-ahead audit row cannot be committed,
        the label is NOT applied (no unaudited writes). On main the audit
        was best-effort and the label went out anyway."""
        from brain.services.gmail_labeler import apply_labels

        class _BrokenFactory:
            def __call__(self) -> Any:
                raise RuntimeError("audit db down")

        monkeypatch.setattr("brain.db.session.SessionLocal", _BrokenFactory())
        state = _ServiceState()
        plans = _plan_labels(state, tmp_path, 2)
        applied = apply_labels(_client(state), plans, max_apply=5, batch_id="bf1")
        assert applied == 0, "a failed audit write means the label is NOT applied"
        modifies = [c for c in state.calls if c[0] == "messages.modify"]
        assert modifies == [], ("no committed intent row -> zero message writes "
                                "(the one-time label CREATE may still happen: "
                                "ensure_labels precedes the per-message loop)")

    def test_modify_failure_marks_failed_and_undo_still_works(self, tmp_path, database):
        """A Gmail modify failure after the intent row: state=failed, the run
        aborts (current semantics), and --undo still works on the applied
        rows while skipping the failed one."""
        from brain.ai.gmail import GmailError
        from brain.services.gmail_labeler import apply_labels, undo_batch

        state = _ServiceState()
        plans = _plan_labels(state, tmp_path, 3)
        client = _FailOnModify(
            _client(state), fail_ids={"m01"}, exc=GmailError("boom 500")
        )
        with pytest.raises(GmailError):
            apply_labels(client, plans, max_apply=3, batch_id="mf1")
        assert _audit_states("m00", batch="mf1") == ["applied"], (
            "the modify that succeeded must leave state=applied"
        )
        assert _audit_states("m01", batch="mf1") == ["failed"], (
            "the modify that failed must leave state=failed"
        )
        undone = undo_batch(client, "mf1")
        assert undone == 1, "undo treats only intent+applied rows as candidates"
        removed = [c[1]["id"] for c in state.calls
                   if c[0] == "messages.modify" and c[1]["body"].get("removeLabelIds")]
        assert removed == ["m00"], (
            f"undo must remove only the applied label (removed {removed!r})"
        )

    def test_undo_treats_intent_and_applied_rows_as_candidates(self, database):
        """Rows from a run that died between the intent commit and the
        modify are still undo candidates (removing a never-applied label is
        harmless and must not error); failed rows are skipped. On main,
        _batch_events had no state filter and returned every row."""
        from brain.services.gmail_labeler import undo_batch

        state = _ServiceState()
        for mid in ("m1", "m2", "m3"):
            state.messages[mid] = _msg_with_headers(
                mid, sender="office@school.example", subject=mid
            )
        from brain.db.session import engine

        with engine.begin() as conn:
            conn.execute(text("DELETE FROM ops_events WHERE component = 'gmail-label' "
                              "AND detail->>'batch' = 'ix1'"))
            for gmail_id, row_state in (("m1", "applied"), ("m2", "intent"),
                                        ("m3", "failed")):
                conn.execute(
                    text(
                        "INSERT INTO ops_events (component, kind, ok, detail) "
                        "VALUES ('gmail-label', 'apply', true, CAST(:d AS jsonb))"
                    ),
                    {"d": json.dumps({"batch": "ix1", "gmail_id": gmail_id,
                                      "label_path": "SB/School", "label_id": "L_1",
                                      "source": "rules", "state": row_state})},
                )
        undone = undo_batch(_client(state), "ix1")
        assert undone == 2, (
            "undo candidates are the intent+applied rows; the failed row is "
            "skipped and removing a never-applied label must not error"
        )
