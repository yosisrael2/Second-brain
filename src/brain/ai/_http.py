"""Shared HTTP post helper mapping transport failures onto client error types."""

from __future__ import annotations

from typing import Any

import httpx


def post_json(
    client: httpx.Client,
    url: str,
    *,
    headers: dict[str, str],
    payload: Any,
    error: type[RuntimeError],
    context: str,
    timeout_s: float,
) -> httpx.Response:
    """POST ``payload`` as JSON; map timeout/HTTP errors onto ``error``.

    ``context`` names the service in error messages (e.g. "jev"). The raised
    messages never include request headers, so API keys stay out of logs.
    """
    try:
        response = client.post(url, headers=headers, json=payload)
        response.raise_for_status()
        return response
    except httpx.TimeoutException as exc:
        raise error(f"{context} request timed out after {timeout_s}s") from exc
    except httpx.HTTPError as exc:
        raise error(f"{context} request failed: {type(exc).__name__}: {exc}") from exc
