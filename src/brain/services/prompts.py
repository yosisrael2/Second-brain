"""User-editable LLM prompts + the user memory file (registry, store, lookup).

Every system prompt the code sends to an LLM is registered here under a
key, with its code default (the hard-coded text, byte-identical), a short
description and the set of template placeholders the code fills in with
``str.format``. The user edits them from the cockpit (``/prompts`` API);
edits are append-only rows in ``prompt_versions`` (current = the highest
version per key, ``text`` NULL = back on the default), so history is never
lost and every rendered prompt stays traceable to an exact version.

Lookup (:func:`resolve`) reads the DB at use time with the code default as
the fallback: any DB failure, a missing table or a stored text that no
longer matches the code's placeholders falls back to the default and logs a
warning — a prompt lookup can never break the pipeline that renders it.
All keys are read in ONE query and cached for :data:`CACHE_TTL_S` seconds,
so callers never query per call; writes through :class:`PromptStore`
invalidate this process's cache (other processes pick it up within the TTL).

The memory file (key :data:`MEMORY_FILE_KEY`) is one free markdown text
that :func:`with_memory_file` adds to every LLM prompt in a delimited
block. Empty (the default) adds nothing, so prompts stay byte-identical.
It is separate from the memory-layer facts (MemoryService.compact_block).

Defaults live next to the code that uses them (ask.py, llm_extractor.py,
...); the registry imports them lazily so those modules can import this
one without a cycle.
"""

from __future__ import annotations

import logging
import string
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from functools import lru_cache
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from brain.db.models import PromptVersion

logger = logging.getLogger(__name__)

MEMORY_FILE_KEY = "memory_file"
MEMORY_FILE_MAX_CHARS = 8000
PROMPT_MAX_CHARS = 50_000
CACHE_TTL_S = 30.0

MEMORY_FILE_HEADER = "User memory file (standing instructions and context from the user):"
MEMORY_FILE_BEGIN = "<<<BEGIN_MEMORY_FILE>>>"
MEMORY_FILE_END = "<<<END_MEMORY_FILE>>>"


class PromptValidationError(ValueError):
    """A prompt text the code could not render safely (rejected on save)."""


@dataclass(frozen=True)
class PromptSpec:
    """One registry entry: the code default and the contract an edit keeps."""

    key: str
    title: str
    description: str
    default: str
    placeholders: frozenset[str]
    max_chars: int = PROMPT_MAX_CHARS

    @property
    def is_template(self) -> bool:
        """True when the code renders this text with ``str.format``."""
        return bool(self.placeholders)


def _ask_specs() -> list[PromptSpec]:
    from brain.ai.ask import CITATION_RULES, CONFLICT_RULES  # noqa: PLC0415

    return [
        PromptSpec(
            key="ask.citation_rules",
            title="Ask — citation rules",
            description=(
                "How answers use and cite the source items (the question, current "
                "time, memory facts and sources are filled in by the code)."
            ),
            default=CITATION_RULES,
            placeholders=frozenset(),
        ),
        PromptSpec(
            key="ask.conflict_rules",
            title="Ask — conflict rules",
            description="How answers handle two sources that disagree about one event.",
            default=CONFLICT_RULES,
            placeholders=frozenset(),
        ),
    ]


def _extraction_specs() -> list[PromptSpec]:
    from brain.services.extraction.llm_extractor import _PROMPT_TEMPLATE  # noqa: PLC0415

    return [
        PromptSpec(
            key="extraction",
            title="Extraction",
            description=(
                "Turns one message into events / tasks / reminders / facts (strict "
                "JSON). Overriding it changes the recorded prompt version "
                "(e.g. v8+u3). Literal braces must stay doubled: {{ }}."
            ),
            default=_PROMPT_TEMPLATE,
            placeholders=frozenset(
                {"sent_local", "tz_name", "today_local", "memory_context", "email_text"}
            ),
        )
    ]


def _judge_specs() -> list[PromptSpec]:
    from brain.services.extraction.judge import _LLM_PROMPT_TEMPLATE  # noqa: PLC0415

    return [
        PromptSpec(
            key="recon_judge",
            title="Entity judge",
            description=(
                "Decides whether a new mention and a stored item are the same "
                "real-world thing (LLM fallback after Jev). Literal braces must "
                "stay doubled: {{ }}."
            ),
            default=_LLM_PROMPT_TEMPLATE,
            placeholders=frozenset({"candidate", "existing", "version"}),
        )
    ]


def _triage_specs() -> list[PromptSpec]:
    from brain.services.triage import _PROMPT_TEMPLATE  # noqa: PLC0415

    return [
        PromptSpec(
            key="whatsapp_triage",
            title="WhatsApp triage",
            description=(
                "Labels a WhatsApp conversation relevant or noise (LLM fallback "
                "after Jev). Overriding it changes the recorded triage version. "
                "Literal braces must stay doubled: {{ }}."
            ),
            default=_PROMPT_TEMPLATE,
            placeholders=frozenset({"text", "version"}),
        )
    ]


def _attachment_specs() -> list[PromptSpec]:
    from brain.services.attachments import _READ_INSTRUCTION  # noqa: PLC0415

    return [
        PromptSpec(
            key="attachments.read_instruction",
            title="Attachment reader",
            description=(
                "System prompt for transcribing an attachment (image / scanned "
                "PDF) with the Claude reader."
            ),
            default=_READ_INSTRUCTION,
            placeholders=frozenset(),
        )
    ]


def _memory_file_specs() -> list[PromptSpec]:
    return [
        PromptSpec(
            key=MEMORY_FILE_KEY,
            title="Memory file",
            description=(
                "Standing instructions and context, added to every LLM prompt above "
                "in a delimited block. Empty = nothing is added."
            ),
            default="",
            placeholders=frozenset(),
            max_chars=MEMORY_FILE_MAX_CHARS,
        )
    ]


_SPEC_BUILDERS: tuple[Callable[[], list[PromptSpec]], ...] = (
    _ask_specs,
    _extraction_specs,
    _judge_specs,
    _triage_specs,
    _attachment_specs,
    _memory_file_specs,
)


@lru_cache(maxsize=1)
def specs() -> dict[str, PromptSpec]:
    """Every registered key -> its spec, in display order."""
    return {spec.key: spec for build in _SPEC_BUILDERS for spec in build()}


def get_spec(key: str) -> PromptSpec:
    """The spec for ``key``; KeyError for an unknown key."""
    return specs()[key]


# --- validation -----------------------------------------------------------------


def template_fields(text: str) -> set[str]:
    """The named ``str.format`` fields in ``text`` (parsed with string.Formatter).

    Raises :class:`PromptValidationError` for anything ``format`` with
    keyword arguments could crash on or misuse: unbalanced braces, positional
    fields (``{}`` / ``{0}``), attribute or index access (``{x.y}``,
    ``{x[0]}``), conversions (``{x!r}``) and format specs (``{x:>9}``).
    """
    fields: set[str] = set()
    try:
        parsed = list(string.Formatter().parse(text))
    except ValueError as exc:
        raise PromptValidationError(
            f"malformed braces ({exc}); write a literal brace as {{{{ or }}}}"
        ) from None
    for _, field, spec, conversion in parsed:
        if field is None:
            continue
        if field == "" or field.isdigit():
            raise PromptValidationError(
                "positional placeholder '{}' is not allowed; write a literal brace as {{ or }}"
            )
        if not field.isidentifier():
            raise PromptValidationError(
                f"placeholder {{{field}}} is not allowed (only plain names, no "
                "attribute or index access); write a literal brace as {{ or }}"
            )
        if conversion or spec:
            raise PromptValidationError(
                f"placeholder {{{field}}} must not carry a conversion or format spec"
            )
        fields.add(field)
    return fields


def validate(key: str, text: str) -> None:
    """Reject a text the code could not render for ``key`` (KeyError: unknown key).

    Templates must keep EXACTLY the placeholders the code fills in (a dropped
    one loses data, an added one would crash at format time). Non-template
    texts are used verbatim, so braces there are just text.
    """
    spec = get_spec(key)
    if len(text) > spec.max_chars:
        raise PromptValidationError(
            f"text is {len(text)} characters; the limit for {key!r} is {spec.max_chars}"
        )
    if key != MEMORY_FILE_KEY and not text.strip():
        raise PromptValidationError("prompt text must not be empty (use reset to default)")
    if not spec.is_template:
        return
    fields = template_fields(text)
    missing = sorted(spec.placeholders - fields)
    extra = sorted(fields - spec.placeholders)
    problems = []
    if missing:
        problems.append("missing placeholder(s): " + ", ".join(f"{{{f}}}" for f in missing))
    if extra:
        problems.append("unknown placeholder(s): " + ", ".join(f"{{{f}}}" for f in extra))
    if problems:
        raise PromptValidationError(
            "; ".join(problems)
            + ". Allowed: "
            + ", ".join(f"{{{f}}}" for f in sorted(spec.placeholders))
            + " (write a literal brace as {{ or }})"
        )


# --- lookup (cached, DB-tolerant) ------------------------------------------------


@dataclass(frozen=True)
class ResolvedPrompt:
    """The text to use now for one key; ``version`` None = the code default."""

    key: str
    text: str
    version: int | None = None

    @property
    def overridden(self) -> bool:
        return self.version is not None


_lock = threading.Lock()
_cache: tuple[float, dict[str, tuple[int, str]]] | None = None


def invalidate_cache() -> None:
    """Drop this process's cached overrides (writes and tests call this)."""
    global _cache
    with _lock:
        _cache = None


def _load_overrides() -> dict[str, tuple[int, str]]:
    """key -> (version, text) for every key whose latest row is an override."""
    from brain.db.session import SessionLocal  # noqa: PLC0415 - lazy: keeps imports light

    with SessionLocal() as session:
        rows = PromptStore(session).latest_rows()
    return {key: (row.version, row.text) for key, row in rows.items() if row.text is not None}


def _overrides() -> dict[str, tuple[int, str]]:
    global _cache
    now = time.monotonic()
    with _lock:
        if _cache is not None and _cache[0] > now:
            return _cache[1]
        try:
            loaded = _load_overrides()
        except Exception as exc:  # noqa: BLE001 - any DB failure -> code defaults
            logger.warning(
                "prompts: override lookup failed, using code defaults (%s: %s)",
                type(exc).__name__,
                str(exc)[:200],
            )
            loaded = {}
        _cache = (now + CACHE_TTL_S, loaded)
        return loaded


def resolve(key: str) -> ResolvedPrompt:
    """The current text for ``key``: the latest override, else the code default.

    A stored text that fails today's validation (e.g. the code changed a
    template's placeholders after it was saved) is ignored with a warning.
    """
    spec = get_spec(key)
    override = _overrides().get(key)
    if override is None:
        return ResolvedPrompt(key, spec.default)
    version, text = override
    try:
        validate(key, text)
    except PromptValidationError as exc:
        logger.warning(
            "prompts: stored %s v%d is invalid now, using the code default (%s)",
            key,
            version,
            exc,
        )
        return ResolvedPrompt(key, spec.default)
    return ResolvedPrompt(key, text, version)


def prompt_text(key: str) -> str:
    """Shorthand: the current text for ``key``."""
    return resolve(key).text


def version_tag(base: str, template: ResolvedPrompt, memory: ResolvedPrompt) -> str:
    """Provenance tag for a prompt rendered from ``template`` + ``memory``.

    ``base`` plus ``+u<N>`` when the template is user version N and
    ``+m<N>`` when a non-empty memory file (version N) rode along — just
    ``base`` ('v8') with neither. Pass the SAME resolved objects the prompt
    was rendered from, so the tag can never disagree with the prompt.
    """
    tag = base
    if template.overridden:
        tag += f"+u{template.version}"
    if memory.overridden and memory.text.strip():
        tag += f"+m{memory.version}"
    return tag


def memory_file_block(text: str | None = None) -> str:
    """The delimited memory-file block ('' when the memory file is empty).

    ``text`` None reads the current memory file.
    """
    body = (prompt_text(MEMORY_FILE_KEY) if text is None else text).strip()
    if not body:
        return ""
    return f"{MEMORY_FILE_HEADER}\n{MEMORY_FILE_BEGIN}\n{body}\n{MEMORY_FILE_END}"


def with_memory_file(prompt: str, memory_text: str | None = None) -> str:
    """``prompt`` with the memory-file block ahead of it (unchanged when empty).

    The block goes FIRST so every prompt still ends with its own response
    cue ("JSON response:", the JSON answer line). ``memory_text`` None
    reads the current memory file.
    """
    block = memory_file_block(memory_text)
    return f"{block}\n\n{prompt}" if block else prompt


# --- store (writes + history) ----------------------------------------------------


class PromptStore:
    """Append-only access to ``prompt_versions`` over one session."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def latest_rows(self) -> dict[str, PromptVersion]:
        """key -> its highest-version row, for every key that has rows."""
        latest = (
            select(PromptVersion.key, func.max(PromptVersion.version).label("version"))
            .group_by(PromptVersion.key)
            .subquery()
        )
        rows = self._session.scalars(
            select(PromptVersion).join(
                latest,
                (PromptVersion.key == latest.c.key) & (PromptVersion.version == latest.c.version),
            )
        )
        return {row.key: row for row in rows}

    def history(self, key: str) -> list[PromptVersion]:
        """Every row for ``key``, newest first."""
        get_spec(key)
        return list(
            self._session.scalars(
                select(PromptVersion)
                .where(PromptVersion.key == key)
                .order_by(PromptVersion.version.desc())
            )
        )

    def _latest(self, key: str) -> PromptVersion | None:
        return self._session.scalars(
            select(PromptVersion)
            .where(PromptVersion.key == key)
            .order_by(PromptVersion.version.desc())
            .limit(1)
        ).first()

    def _append(self, key: str, text: str | None, source: str) -> PromptVersion:
        latest = self._latest(key)
        row = PromptVersion(
            key=key,
            version=(latest.version + 1) if latest else 1,
            text=text,
            source=source,
        )
        self._session.add(row)
        try:
            self._session.commit()
        except IntegrityError:
            self._session.rollback()
            raise PromptConflictError(
                f"{key!r} was changed concurrently; reload and try again"
            ) from None
        self._session.refresh(row)
        invalidate_cache()
        return row

    def save(self, key: str, text: str, *, source: str = "edit") -> PromptVersion | None:
        """Append ``text`` as the new current version (validated first).

        Returns None (no row) when nothing would change. Saving the exact
        code default is stored as a reset (NULL text), so a default-equal
        prompt keeps the default's provenance.
        """
        validate(key, text)
        spec = get_spec(key)
        latest = self._latest(key)
        current = latest.text if latest is not None and latest.text is not None else None
        stored = None if text == spec.default else text
        if stored == current:
            return None
        return self._append(key, stored, source)

    def reset(self, key: str) -> PromptVersion | None:
        """Back to the code default (a NULL-text row); None when already there."""
        get_spec(key)
        latest = self._latest(key)
        if latest is None or latest.text is None:
            return None
        return self._append(key, None, "reset")

    def restore(self, key: str, version: int) -> PromptVersion | None:
        """Copy an older version's text forward as the new current version."""
        get_spec(key)
        row = self._session.scalars(
            select(PromptVersion).where(PromptVersion.key == key, PromptVersion.version == version)
        ).first()
        if row is None:
            raise LookupError(f"{key!r} has no version {version}")
        if row.text is None:
            return self.reset(key)
        return self.save(key, row.text, source="restore")

    def describe(self, key: str) -> dict[str, Any]:
        """The full view of one key: current/default text, placeholders, history."""
        spec = get_spec(key)
        history = self.history(key)
        latest = history[0] if history else None
        overridden = latest is not None and latest.text is not None
        return {
            **_summary(spec, latest),
            "text": latest.text if overridden and latest is not None else spec.default,
            "default_text": spec.default,
            "placeholders": sorted(spec.placeholders),
            "max_chars": spec.max_chars,
            "history": [
                {
                    "version": row.version,
                    "source": row.source,
                    "is_default": row.text is None,
                    "text": row.text if row.text is not None else spec.default,
                    "created_at": row.created_at,
                }
                for row in history
            ],
        }

    def summaries(self) -> list[dict[str, Any]]:
        """One summary per registered key (list view)."""
        latest = self.latest_rows()
        return [_summary(spec, latest.get(key)) for key, spec in specs().items()]


class PromptConflictError(RuntimeError):
    """A concurrent save took the same version number."""


def _summary(spec: PromptSpec, latest: PromptVersion | None) -> dict[str, Any]:
    updated_at: datetime | None = latest.created_at if latest is not None else None
    return {
        "key": spec.key,
        "title": spec.title,
        "description": spec.description,
        "kind": "memory_file" if spec.key == MEMORY_FILE_KEY else "prompt",
        "is_overridden": latest is not None and latest.text is not None,
        "version": latest.version if latest is not None and latest.text is not None else None,
        "latest_version": latest.version if latest is not None else None,
        "updated_at": updated_at,
    }
