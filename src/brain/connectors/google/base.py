"""GoogleConnector: shared base for Google data sources (SKELETON).

This file is the contract skeleton pushed first so adapter workers can branch;
the bodies land with the base-framework commit of the same ticket. What it
will own (per the connectors contract):

- ONE OAuth client + refresh token, stored where ``brain/ai/gmail.py`` already
  keeps its keychain token; the google libs are imported lazily only here.
- ``scopes`` = the union of the enabled connectors' scopes (READ-ONLY this
  ticket: gmail.readonly / calendar.readonly / drive.readonly / tasks.readonly).
- ``service_factory`` injectable — tests pass a fake service object; tests
  never touch the network.
- Google ``HttpError`` translated to ConnectorAuthError (401/403, revoked
  token) / ConnectorRetryableError (429 / 5xx / network); everything else is a
  ConnectorError.
- ``iter_pages(request_fn)`` walks ``nextPageToken`` pagination.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from typing import Any, ClassVar

from brain.connectors.base import Connector, ConnectorError


class GoogleConnector(Connector):
    """Shared base for Google sources (see module docstring for the plan).

    Subclasses set :attr:`name` to their canonical source name and implement
    :meth:`~brain.connectors.base.Connector.fetch`.
    """

    # Set by subclasses: "email" (gmail, for items.source compat), "gcal",
    # "gdrive", "gtasks".
    name: ClassVar[str]

    def __init__(
        self,
        *,
        token_loader: Callable[[], str],
        service_factory: Callable[[str, list[str]], Any],
        user_id: str = "me",
        scopes: tuple[str, ...] = (),
    ) -> None:
        """``token_loader`` returns the keychain refresh token; the factory
        builds a service-like object for (refresh_token, scopes). Both are
        injectable so tests pass fakes."""
        raise NotImplementedError("implemented in the base-framework commit")

    def _service(self) -> Any:
        """The (lazily built, cached) Google service object."""
        raise NotImplementedError("implemented in the base-framework commit")

    @staticmethod
    def iter_pages(request_fn: Callable[[str | None], Any]) -> Iterator[dict[str, Any]]:
        """Execute ``request_fn(page_token)`` until no ``nextPageToken`` remains,
        yielding each page dict."""
        raise NotImplementedError("implemented in the base-framework commit")

    @staticmethod
    def _translate_error(exc: Exception) -> ConnectorError:
        """Map a duck-typed Google error onto the connector error taxonomy."""
        raise NotImplementedError("implemented in the base-framework commit")
