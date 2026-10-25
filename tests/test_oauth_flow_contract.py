"""The OAuth flow builders must call what the REAL google library provides.

Incident 25 Oct: `brain connectors auth google` crashed with
``AttributeError: type object 'InstalledAppFlow' has no attribute
'from_client_secret_file'`` — the real method is ``from_client_secrets_file``
(with an "s"). The test fakes had copied the typo, so the suite stayed green
while both production call sites were broken. These tests pin the contract to
the real library: the builders are called against a spy installed where the
real class lives, and the real class itself is asserted to have the method
(skipped when the gmail extra is not installed).
"""

from __future__ import annotations

import pytest

cli = pytest.importorskip("brain.cli")

google_flow = pytest.importorskip("google_auth_oauthlib.flow")
from google_auth_oauthlib.flow import InstalledAppFlow  # noqa: E402


class _SpyFlow:
    """Stands in for the flow object the real class would return."""

    def __init__(self) -> None:
        self.requested_scopes: list[str] | None = None

    def run_local_server(self, port: int = 0):  # pragma: no cover - not reached
        raise AssertionError("consent must never run in tests")


class _SpyFlowClass:
    """Records the method name the builders actually call."""

    method: str | None = None

    @classmethod
    def from_client_secrets_file(cls, path: str, scopes: list[str]) -> _SpyFlow:
        cls.method = "from_client_secrets_file"
        flow = _SpyFlow()
        flow.requested_scopes = list(scopes)
        return flow


def test_real_installed_app_flow_has_the_method_we_call():
    """The real library name — the incident's root: a typo'd name that only
    the fakes answered. Skipped when the gmail extra is not installed."""
    assert callable(getattr(InstalledAppFlow, "from_client_secrets_file", None)), (
        "google_auth_oauthlib.flow.InstalledAppFlow has no from_client_secrets_file"
    )
    assert not hasattr(InstalledAppFlow, "from_client_secret_file"), (
        "guard drifted: the typo'd name exists on the real class now"
    )


def test_both_flow_builders_call_the_real_library_method(monkeypatch, tmp_path):
    """Both production builders must dispatch to from_client_secrets_file on
    whatever class lives at google_auth_oauthlib.flow.InstalledAppFlow — a
    typo'd call site crashes here instead of at consent time."""
    secret = tmp_path / "client_secret.json"
    secret.write_text("{}")

    for builder, args, expected_scopes in (
        (cli._build_oauth_flow, (str(secret),), [cli.GMAIL_SCOPE]),
        (cli._build_connectors_oauth_flow, (str(secret), ["email.readonly"]), ["email.readonly"]),
    ):
        _SpyFlowClass.method = None
        monkeypatch.setattr(google_flow, "InstalledAppFlow", _SpyFlowClass)
        flow = builder(*args)
        assert _SpyFlowClass.method == "from_client_secrets_file", (
            "the builder called a method the real class does not have"
        )
        assert callable(getattr(flow, "run_local_server", None))
        assert flow.requested_scopes == expected_scopes
