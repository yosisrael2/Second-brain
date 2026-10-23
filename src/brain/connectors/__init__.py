"""Registry for data-source connectors.

Adding a new source = subclass :class:`brain.connectors.base.Connector` and
decorate it with :func:`register` — no other wiring.

Registry keys are connector names; ``get`` also resolves a small alias map
(so ``get("gmail")`` can resolve to the connector named ``"email"``, keeping
items.source compatible with the existing email data).
"""

from __future__ import annotations

from typing import TypeVar

from brain.connectors.base import Connector

__all__ = [
    "Connector",
    "all_names",
    "alias",
    "get",
    "register",
]

_REGISTRY: dict[str, type[Connector]] = {}
# registry key -> canonical connector name (e.g. "gmail" -> "email").
_ALIASES: dict[str, str] = {}

T = TypeVar("T", bound=type[Connector])


def register(cls: T) -> T:
    """Class decorator: add a Connector subclass to the registry."""
    name = getattr(cls, "name", None)
    if not name:
        raise ValueError(f"{cls.__name__} cannot register: Connector.name is not set")
    if name in _REGISTRY:
        raise ValueError(f"connector name {name!r} already registered")
    _REGISTRY[name] = cls
    return cls


def alias(key: str, name: str) -> None:
    """Map an alternate lookup ``key`` onto the canonical connector ``name``."""
    _ALIASES[key] = name


def get(name: str) -> type[Connector]:
    """Look up a connector class by name (aliases resolved). KeyError if absent."""
    key = _ALIASES.get(name, name)
    return _REGISTRY[key]


def all_names() -> list[str]:
    """All registered connector names, sorted (canonical names, not aliases)."""
    return sorted(_REGISTRY)
