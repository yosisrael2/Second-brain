"""Nango as the token source for the Google connectors (BRAIN_GOOGLE_AUTH=nango).

Nango holds the OAuth connection (consent, refresh token, refresh); Second
Brain only asks it for a fresh ACCESS token whenever google-auth needs one.
Everything else — cursors, runner, ledger, adopt — is unchanged: the service
factory still returns a plain googleapiclient service.

- :class:`NangoTokenClient` is the one place that talks to Nango
  (``GET /connection/{id}?provider_config_key=...``). The httpx client is
  injectable; tests pass an ``httpx.MockTransport`` — no network, ever.
- :func:`nango_service_factory` builds a service whose credentials carry a
  ``refresh_handler`` (google-auth's hook for externally brokered tokens),
  so a long-running poller re-asks Nango on expiry instead of holding a
  refresh token itself.
- Nango errors map onto the connector taxonomy: a missing/revoked
  connection or a bad secret is an auth error (reconnect in Nango), 429/5xx
  and transport failures are retryable.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

from brain.connectors.base import ConnectorAuthError, ConnectorError, ConnectorRetryableError

NANGO_DEFAULT_HOST = "https://api.nango.dev"

# Nango refreshes a token close to expiry on its own, but google-auth refuses
# a token within its REFRESH_THRESHOLD (~4 min) of expiry; below this margin
# we ask Nango to force a refresh instead of handing google-auth a dud.
_MIN_TOKEN_LIFETIME = timedelta(minutes=5)
# When Nango omits expires_at, treat the token as short-lived: google-auth
# re-asks shortly after instead of trusting it forever.
_UNKNOWN_EXPIRY_LIFETIME = timedelta(minutes=10)


@dataclass(frozen=True)
class NangoConnection:
    """One Nango connection: the integration (provider config key) + id."""

    provider_config_key: str
    connection_id: str


class NangoTokenClient:
    """Fetches access tokens for Nango connections (secret key auth)."""

    def __init__(
        self,
        *,
        secret_key: str,
        host: str = NANGO_DEFAULT_HOST,
        client: httpx.Client | None = None,
        timeout_s: float = 15.0,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        if not secret_key:
            raise ConnectorAuthError("NANGO_SECRET_KEY is not set")
        self._secret_key = secret_key
        self._host = host.rstrip("/")
        self._client = client or httpx.Client(timeout=timeout_s)
        self._timeout_s = timeout_s
        self._now = now

    def access_token(self, connection: NangoConnection) -> tuple[str, datetime]:
        """A fresh (access_token, naive-UTC expiry) for ``connection``.

        Forces a Nango-side refresh when the stored token is about to expire.
        """
        token, expiry = self._fetch(connection, force_refresh=False)
        if expiry - self._now() < _MIN_TOKEN_LIFETIME:
            token, expiry = self._fetch(connection, force_refresh=True)
        # google-auth compares against a naive UTC "now".
        return token, expiry.astimezone(UTC).replace(tzinfo=None)

    def _fetch(self, connection: NangoConnection, *, force_refresh: bool) -> tuple[str, datetime]:
        url = f"{self._host}/connection/{connection.connection_id}"
        params = {"provider_config_key": connection.provider_config_key}
        if force_refresh:
            params["force_refresh"] = "true"
        context = f"nango {connection.provider_config_key}"
        try:
            response = self._client.get(
                url, params=params, headers={"Authorization": f"Bearer {self._secret_key}"}
            )
        except httpx.TimeoutException as exc:
            raise ConnectorRetryableError(
                f"{context} request timed out after {self._timeout_s}s"
            ) from exc
        except httpx.HTTPError as exc:
            raise ConnectorRetryableError(
                f"{context} request failed: {type(exc).__name__}"
            ) from exc
        status = response.status_code
        if status in (401, 403, 404):
            # Bad secret, or the connection is gone/revoked: reconnect in Nango.
            raise ConnectorAuthError(
                f"{context} http {status} — check NANGO_SECRET_KEY and the "
                "connection id, or reconnect the account in Nango",
                status=status,
            )
        if status == 429 or status >= 500:
            raise ConnectorRetryableError(f"{context} http {status}", status=status)
        if status != 200:
            raise ConnectorError(f"{context} http {status}", status=status)
        try:
            credentials = response.json().get("credentials") or {}
        except ValueError as exc:
            raise ConnectorError(f"{context} returned non-JSON") from exc
        token = credentials.get("access_token")
        if not token or not isinstance(token, str):
            raise ConnectorAuthError(
                f"{context} connection has no OAuth access token — reconnect it in Nango"
            )
        return token, self._parse_expiry(credentials.get("expires_at"))

    def _parse_expiry(self, raw: Any) -> datetime:
        if isinstance(raw, str) and raw:
            try:
                parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            except ValueError:
                parsed = None
            if parsed is not None:
                return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
        return self._now() + _UNKNOWN_EXPIRY_LIFETIME


def nango_service_factory(
    client: NangoTokenClient, connection: NangoConnection
) -> Callable[[str, list[str], str, str], Any]:
    """A GoogleConnector ``service_factory`` backed by one Nango connection.

    The factory's ``refresh_token`` argument is ignored (the connector's
    token_loader returns the connection id only to satisfy the "configured?"
    guard); scopes are whatever the Nango integration was granted.
    """

    def factory(_refresh_token: str, scopes: list[str], api_name: str, api_version: str) -> Any:
        try:
            # Lazy + importlib, like default_service_factory.
            import importlib  # noqa: PLC0415

            credentials_mod = importlib.import_module("google.oauth2.credentials")
            discovery = importlib.import_module("googleapiclient.discovery")
        except ImportError as exc:  # pragma: no cover - env without the extra
            raise ConnectorError(
                "google libraries not installed — run `pip install -e '.[gmail]'`"
            ) from exc

        def refresh_handler(_request: Any, scopes: Any = None) -> tuple[str, datetime]:
            return client.access_token(connection)

        credentials = credentials_mod.Credentials(
            token=None, scopes=list(scopes), refresh_handler=refresh_handler
        )
        return discovery.build(
            api_name, api_version, credentials=credentials, cache_discovery=False
        )

    return factory
