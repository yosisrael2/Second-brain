"""GoogleConnector: everything the Google adapters share.

- ONE OAuth client + ONE refresh token: the keychain store and client-secret
  path are the ones brain/ai/gmail.py already uses (service
  ``second-brain-gmail``, account ``default``, GMAIL_CREDENTIALS_PATH) — the
  SAME consent covers every Google source because scopes are the union of the
  enabled connectors' scopes (READ-ONLY this ticket).
- The google libraries are imported ONLY inside :func:`default_service_factory`
  (lazy, like ai/gmail.py); tests inject a fake service factory and duck-typed
  errors — tests never touch the network or the real keychain.
- Error translation lives HERE and is the single source of truth for the
  connector family. (Known, scheduled duplicate: ai/gmail.py still has its own
  mapping until C5 — the Gmail port — routes through this one and deletes it.)
- :meth:`iter_pages` walks ``nextPageToken`` pagination, translating errors.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator, Sequence
from typing import Any, ClassVar

from brain.connectors.base import (
    Connector,
    ConnectorAuthError,
    ConnectorError,
    ConnectorRetryableError,
)

# Canonical connector name -> its read-only scope(s). "gmail" resolves to the
# "email" connector (registry alias; items.source keeps "email").
SCOPE_BY_CONNECTOR: dict[str, tuple[str, ...]] = {
    "email": ("https://www.googleapis.com/auth/gmail.readonly",),
    "gcal": ("https://www.googleapis.com/auth/calendar.readonly",),
    "gdrive": ("https://www.googleapis.com/auth/drive.readonly",),
    "gtasks": ("https://www.googleapis.com/auth/tasks.readonly",),
}
_CONNECTOR_ALIASES = {"gmail": "email"}


def scopes_for(enabled: Sequence[str]) -> list[str]:
    """The union of the enabled connectors' scopes, stable order, deduped.

    Unknown names raise ValueError: a typo in BRAIN_CONNECTORS must fail at
    consent time, not silently request a subset of scopes.
    """
    scopes: list[str] = []
    for name in enabled:
        canonical = _CONNECTOR_ALIASES.get(name, name)
        connector_scopes = SCOPE_BY_CONNECTOR.get(canonical)
        if connector_scopes is None:
            raise ValueError(f"unknown connector name {name!r}")
        for scope in connector_scopes:
            if scope not in scopes:
                scopes.append(scope)
    return scopes


def default_service_factory(refresh_token: str, scopes: list[str]) -> Any:
    """Production service factory: the ONLY place google libs are imported.

    Same client-secret file and keychain token as the email source (one OAuth
    client, one refresh token); ``scopes`` is the union from :func:`scopes_for`.
    """
    import json  # noqa: PLC0415
    from pathlib import Path  # noqa: PLC0415

    from brain.config import get_settings  # noqa: PLC0415

    path = get_settings().gmail_credentials_path
    client_file = Path(path).expanduser() if path else None
    if not client_file or not client_file.is_file():
        raise ConnectorAuthError(
            f"client secret missing/unreadable at {path!r} — re-run `brain connectors "
            "auth google --client-secret /path/to/secret.json`"
        )
    try:
        data = json.loads(client_file.read_text())
    except (OSError, ValueError) as exc:
        raise ConnectorAuthError(
            f"client secret unreadable at {path!r}: {type(exc).__name__}"
        ) from exc
    client = data.get("installed") or data.get("web") or {}
    client_id = client.get("client_id")
    client_secret = client.get("client_secret")
    token_uri = client.get("token_uri")
    if not (client_id and client_secret and token_uri):
        raise ConnectorAuthError(
            f"client secret file at {path!r} is not an OAuth client JSON "
            "(client_id/client_secret/token_uri missing)"
        )
    try:
        # Lazy + importlib: keeps mypy clean whether or not the gmail extra is
        # installed (warn_unused_ignores would make static ignores flaky).
        import importlib  # noqa: PLC0415

        credentials_mod = importlib.import_module("google.oauth2.credentials")
        discovery = importlib.import_module("googleapiclient.discovery")
    except ImportError as exc:  # pragma: no cover - env without the extra
        raise ConnectorError(
            "google libraries not installed — run `pip install -e '.[gmail]'`"
        ) from exc
    credentials = credentials_mod.Credentials(
        token=None,
        refresh_token=refresh_token,
        client_id=client_id,
        client_secret=client_secret,
        token_uri=token_uri,
        scopes=list(scopes),
    )
    return discovery.build("gmail", "v1", credentials=credentials, cache_discovery=False)


class GoogleConnector(Connector):
    """Shared base for Google data sources (gmail/gcal/gdrive/gtasks adapters).

    Subclasses set :attr:`name` (== SourceRecord.source == items.source) and
    declare their read-only :attr:`SCOPES`; the union across enabled
    connectors is what consent requests and the service factory use.
    """

    # Set by subclasses (e.g. ``name = "gcal"``). Declared on Connector as a
    # ClassVar; redeclared here only as documentation — mypy is satisfied by
    # the base declaration.
    SCOPES: ClassVar[tuple[str, ...]] = ()

    def __init__(
        self,
        *,
        token_loader: Callable[[], str],
        service_factory: Callable[[str, list[str]], Any] = default_service_factory,
        user_id: str = "me",
    ) -> None:
        """``token_loader`` returns the keychain refresh token (empty = none);
        ``service_factory`` builds a service-like object for
        (refresh_token, scopes). Both injectable — tests pass fakes."""
        self._token_loader = token_loader
        self._service_factory = service_factory
        self._user_id = user_id
        self._service_instance: Any = None

    def _service(self) -> Any:
        """The lazily built, cached Google service object."""
        if self._service_instance is None:
            token = self._token_loader()
            if not token:
                raise ConnectorAuthError(
                    "no refresh token in the keychain — run `brain connectors auth google`"
                )
            self._service_instance = self._service_factory(token, self.scopes())
        return self._service_instance

    @property
    def _http_error(self) -> str:  # pragma: no cover - documentation helper
        return "duck-typed HttpError"

    def scopes(self) -> list[str]:
        """This connector's scopes (adapters declare SCOPES; the union across
        enabled connectors is applied by the consent flow)."""
        return list(self.SCOPES)

    def _execute(self, request: Any) -> Any:
        """Execute one API request, translating errors into the contract's."""
        try:
            return request.execute()
        except Exception as exc:  # noqa: BLE001 — translated below
            translated = self._translate_error(exc)
            raise translated from exc

    @staticmethod
    def iter_pages(request_fn: Callable[[str | None], Any]) -> Iterator[dict[str, Any]]:
        """Execute ``request_fn(page_token)`` page by page (error-translating).

        Yields each page dict until one carries no ``nextPageToken``.
        ``request_fn`` may return either an already-executed mapping or a
        request-like object (``.execute()``) — execution and error
        translation happen here, so adapters never see raw HttpErrors.
        """
        page_token: str | None = None
        while True:
            page = request_fn(page_token)
            if hasattr(page, "execute"):
                try:
                    page = page.execute()
                except Exception as exc:  # noqa: BLE001 — translated below
                    raise GoogleConnector._translate_error(exc) from exc
            yield page
            page_token = page.get("nextPageToken") if isinstance(page, dict) else None
            if not page_token:
                return

    @staticmethod
    def _translate_error(exc: Exception) -> ConnectorError:
        """Map a duck-typed Google error onto the connector error taxonomy.

        Works without the google libs importable: HttpError carries
        ``.resp.status``; RefreshError is recognized by type name (same
        approach as ai/gmail.py until C5 consolidates them).
        """
        if type(exc).__name__ == "RefreshError" or "invalid_grant" in str(exc):
            return ConnectorAuthError(f"refresh token rejected ({exc})")
        resp = getattr(exc, "resp", None)
        status = getattr(resp, "status", None)
        if status is not None:
            if status in (401, 403):
                return ConnectorAuthError(f"google http {status}", status=status)
            if status == 410:  # Gone: incremental cursor expired -> full resync
                return ConnectorRetryableError(
                    f"google http {status} (sync cursor expired)", status=410
                )
            if status >= 500 or status == 429:
                return ConnectorRetryableError(f"google http {status}", status=status)
            return ConnectorError(f"google http {status}", status=status)
        return ConnectorError(f"google call failed: {type(exc).__name__}: {exc}")
