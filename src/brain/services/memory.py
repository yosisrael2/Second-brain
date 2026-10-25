"""Memory layer service: durable, confirmed knowledge about the user's world.

Notes are WHAT HAPPENED (the capture stream); memory is WHAT IS TRUE. Facts
are time-versioned (close + open, never edited in place) and status-gated:
ONLY current confirmed facts drive :meth:`resolve_applies_to`,
:func:`~brain.services.ask.AskService`'s prompt block, and — from PR A —
extraction. Provenance records where a fact came from, so extraction can
insert proposals without a schema change.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from brain.db.models import Entity, EntityAlias, MemoryFact
from brain.repositories.memory import MemoryRepository, utcnow
from brain.services.triage import is_name_token, mentions_name

# Hebrew-letter numerals used in school emails ("כיתות ה-ו" = grades 5-6).
# Note ל=30 and מ=40 in real Hebrew numerals — they are NOT grades here.
HEBREW_DIGITS = {
    "א": 1, "ב": 2, "ג": 3, "ד": 4, "ה": 5, "ו": 6, "ז": 7, "ח": 8, "ט": 9,
}
HEBREW_TENS = {"י": 10}
# Compound forms after the tens letter: יא = 11, יב = 12.

VALID_GENDERS = frozenset({"girl", "boy"})
CHANNEL_KINDS = frozenset({"teacher", "parents", "kids", "club", "committee"})

BLOCK_MAX_CHARS = 6000  # ~1500 tokens; hard cap, truncated with a marker


class MemoryError(Exception):
    """A memory use case could not be completed (bad state transition)."""


def _fact_dict(fact: MemoryFact) -> dict[str, Any]:
    return {
        "id": str(fact.id),
        "subject_id": str(fact.subject_id),
        "predicate": fact.predicate,
        "object_id": str(fact.object_id) if fact.object_id else None,
        "value": fact.value,
        "valid_from": fact.valid_from.isoformat() if fact.valid_from else None,
        "valid_to": fact.valid_to.isoformat() if fact.valid_to else None,
        "status": fact.status,
        "supersedes_id": str(fact.supersedes_id) if fact.supersedes_id else None,
        "provenance": fact.provenance or {},
        "created_at": fact.created_at.isoformat() if fact.created_at else None,
    }


def _entity_dict(entity: Entity, aliases: list[str] | None = None) -> dict[str, Any]:
    return {
        "id": str(entity.id),
        "kind": entity.kind,
        "name": entity.name,
        "created_at": entity.created_at.isoformat() if entity.created_at else None,
        "aliases": aliases or [],
    }


class MemoryService:
    """Confirmed-knowledge use cases over the memory repository."""

    def __init__(self, repo: MemoryRepository) -> None:
        self._repo = repo

    # -- kids (confirmed seed / profile) ---------------------------------------

    def kids(self) -> list[dict[str, Any]]:
        """Current confirmed profile per kid: name + stored attributes.

        A kid is a person entity with a current confirmed 'grade' fact.
        Attributes without a fact are simply absent from that kid's dict.
        """
        people = self._repo.list_entities(kinds=["person"])
        facts = self._repo.current_facts(
            subject_ids=[e.id for e in people], status="confirmed"
        )
        by_subject: dict[uuid.UUID, dict[str, str]] = {}
        for fact in facts:
            if fact.value is not None:
                by_subject.setdefault(fact.subject_id, {})[fact.predicate] = fact.value
        kids: list[dict[str, Any]] = []
        for entity in people:
            attrs = by_subject.get(entity.id)
            if not attrs or "grade" not in attrs:
                continue
            kid: dict[str, Any] = {"name": entity.name}
            kid.update(attrs)
            kids.append(kid)
        return kids

    def channel_names_for_kid(self, kid: str) -> list[str]:
        """Confirmed channel names that apply to ONE kid (the ask kid-recall
        leg is the caller; THE one channel->kid mapping in the codebase).

        A channel applies to ``kid`` when its current confirmed 'applies_to'
        fact names the kid, or 'family' (a family channel belongs to every
        confirmed kid — the same query-time resolution _resolve_channel gives
        audience resolution). Names are the exact stored entity names — the
        same strings WhatsApp captures keep in meta.channel — so callers can
        match items structurally instead of by raw-text substring.
        """
        if not kid:
            return []
        channels = self._repo.list_entities(kinds=["channel"])
        if not channels:
            return []
        facts = self._repo.current_facts(
            subject_ids=[c.id for c in channels], status="confirmed"
        )
        applies: dict[uuid.UUID, list[str]] = {}
        for fact in facts:
            if fact.predicate == "applies_to" and fact.value is not None:
                applies.setdefault(fact.subject_id, []).append(fact.value)
        kid_names = {k["name"] for k in self.kids()}
        return sorted(
            c.name
            for c in channels
            if kid in applies.get(c.id, [])
            or ("family" in applies.get(c.id, []) and kid in kid_names)
        )

    def set_kid(
        self,
        name: str,
        *,
        gender: str | None = None,
        school: str | None = None,
        grade: str | int | None = None,
        class_: str | None = None,
        teacher: str | None = None,
    ) -> dict[str, Any]:
        """Create or update a kid's profile facts (user-confirmed by definition).

        An UNCHANGED attribute keeps its fact row (no history spam); a changed
        one closes the old version (valid_to=now) and opens a new row.
        """
        if gender is not None and gender not in VALID_GENDERS:
            raise ValueError(
                f"gender must be one of {sorted(VALID_GENDERS)} (got {gender!r})"
            )
        entity = self._ensure_entity(kind="person", name=name)
        provided = {
            "gender": gender,
            "school": school,
            "grade": None if grade is None else str(grade),
            "class": class_,
            "teacher": teacher,
        }
        for predicate, value in sorted(provided.items()):
            if value is None:
                continue
            current = self._current_confirmed_fact(entity.id, predicate)
            if current is not None and current.value == value:
                continue
            supersedes = None
            if current is not None:
                self._repo.close_fact(current, at=utcnow())
                supersedes = current.id  # keep the version chain queryable
            self._repo.add_fact(
                MemoryFact(
                    subject_id=entity.id,
                    predicate=predicate,
                    value=value,
                    status="confirmed",
                    valid_from=utcnow(),
                    supersedes_id=supersedes,
                    provenance={"source": "user"},
                )
            )
        self._repo.commit()
        for kid in self.kids():
            if kid["name"] == name:
                return kid
        return {"name": name}

    def set_channel(
        self, name: str, *, kind: str, applies_to: str
    ) -> dict[str, Any]:
        """Create or update a channel's audience facts (versioned like set_kid).

        ``applies_to`` must be a confirmed kid name (exact), 'me' or 'family';
        anything else raises ValueError instead of silently mis-resolving.
        Re-seeding the SAME value is a no-op; a different value closes the old
        fact and opens a new one (supersedes_id), so history stays complete.
        """
        if kind not in CHANNEL_KINDS:
            raise ValueError(
                f"kind must be one of {sorted(CHANNEL_KINDS)} (got {kind!r})"
            )
        if applies_to != "me" and applies_to != "family":
            kid_names = {kid["name"] for kid in self.kids()}
            if applies_to not in kid_names:
                raise ValueError(
                    "applies_to must be a confirmed kid name (exact), 'me' or "
                    f"'family' (got {applies_to!r}; known kids: {sorted(kid_names)})"
                )
        entity = self._ensure_entity(kind="channel", name=name)
        for predicate, value in (("channel_kind", kind), ("applies_to", applies_to)):
            current = self._current_confirmed_fact(entity.id, predicate)
            if current is not None and current.value == value:
                continue
            supersedes = None
            if current is not None:
                self._repo.close_fact(current, at=utcnow())
                supersedes = current.id
            self._repo.add_fact(
                MemoryFact(
                    subject_id=entity.id,
                    predicate=predicate,
                    value=value,
                    status="confirmed",
                    valid_from=utcnow(),
                    supersedes_id=supersedes,
                    provenance={"source": "user"},
                )
            )
        self._repo.commit()
        return _entity_dict(entity, self._repo.aliases_for(entity.id))

    def remember(self, text: str) -> dict[str, Any]:
        """Store a confirmed free-text fact on 'me' (predicate 'note')."""
        me = self._ensure_entity(kind="person", name="me")
        fact = self._repo.add_fact(
            MemoryFact(
                subject_id=me.id,
                predicate="note",
                value=text,
                status="confirmed",
                valid_from=utcnow(),
                provenance={"source": "user"},
            )
        )
        self._repo.commit()
        return _fact_dict(fact)

    # -- prompt block ------------------------------------------------------------

    def compact_block(self) -> str:
        """A short confirmed-facts block for the ask prompt ('' when empty).

        Empty output matters: with no memory the ask prompt is byte-identical
        to the pre-memory format (pinned by tests and the calendar eval).
        """
        facts = self._repo.current_facts(status="confirmed")
        if not facts:
            return ""
        entities = {e.id: e for e in self._repo.list_entities()}
        kid_names = {kid["name"] for kid in self.kids()}
        kid_attrs: dict[str, dict[str, str]] = {}
        notes: list[str] = []
        other: list[str] = []
        for fact in facts:
            if fact.value is None:
                continue  # object-linked facts render from PR A (relations)
            subject = entities.get(fact.subject_id)
            if subject is None:
                continue
            if subject.name in kid_names and fact.predicate != "note":
                kid_attrs.setdefault(subject.name, {})[fact.predicate] = fact.value
            elif fact.predicate == "note":
                notes.append(fact.value)
            else:
                other.append(f"{subject.name}: {fact.predicate} {fact.value}")
        lines = []
        order = ("gender", "grade", "class", "school", "teacher")
        for name, attrs in kid_attrs.items():
            joined = ", ".join(f"{k} {v}" for k in order if (v := attrs.get(k)))
            lines.append(f"- {name}: {joined}")
        lines.extend(f"- note: {note}" for note in notes)
        lines.extend(f"- {line}" for line in other)
        block = "\n".join(["Confirmed memory:", *lines])
        if len(block) > BLOCK_MAX_CHARS:
            block = block[:BLOCK_MAX_CHARS].rsplit("\n", 1)[0] + "\n- (memory truncated)"
        return block

    def default_audience_for_sender(self, sender: str | None) -> list[str] | None:
        """Source-level default audience from the SENDER (rule R1, item 33/42).

        A class teacher's update often never names the class in the body.
        Memory holds each kid's confirmed ``teacher`` fact; when the source's
        sender (email From display name / address, or a WhatsApp channel
        name) matches a kid's teacher, that kid (or those kids) is the
        DEFAULT audience for every child extracted from the source. The
        item-level audience may only NARROW it, never widen it. ``None``
        when there is no sender or no confirmed match (callers then keep the
        item-level resolution untouched).
        """
        if not sender:
            return None
        matched: list[str] = []
        for kid in self.kids():
            teacher = kid.get("teacher")
            if teacher is None or not str(teacher).strip():
                continue
            # review round 2, bug 2: the SHARED whole-word matcher, one
            # direction only (does the sender mention the teacher?). Raw
            # containment matched a same-first-name parent ('Rinat Levi'),
            # a word containing the name ('Rinatech') and a short sender
            # inside a long teacher name. A first-name-only teacher fact
            # can still match a same-named parent — acceptable for now:
            # the results are proposals (D2 Keep/Fix/Drop), not actions.
            if mentions_name(str(teacher), sender):
                matched.append(kid["name"])
        return sorted(matched) or None

    # -- applies_to resolution (design item 17) -----------------------------------

    def resolve_applies_to(self, audience: dict[str, Any] | None) -> list[str]:
        """Resolve a child's audience against confirmed memory.

        Returns kid names, ['me'], ['nobody'] or ['unknown']. ``unknown`` is
        the honest default: missing/unconfirmed memory or no audience info is
        NEVER collapsed to ``nobody`` (unknown children stay visible and are
        never auto-dropped, never auto-tasked). Gender is always an explicit
        fact — never inferred from a name.
        """
        kids = self.kids()
        if not audience or not any(
            value not in (None, [], "") for value in audience.values()
        ):
            return ["unknown"]
        if not kids:
            return ["unknown"]

        channel = audience.get("channel")
        if channel is not None:
            return self._resolve_channel(str(channel))

        named = audience.get("named_kids") or []
        if named:
            # FULL-TOKEN match required (the searching mentions_name is not
            # used here on purpose): an extracted classmate "רונה לוי" or
            # "Rona Cohen" must stay ["unknown"] — zero wrong-child is the
            # A2 bar, and unknown is always safer than a wrong kid. Hebrew
            # prefix runs (ורונה) still match, case-insensitively for
            # English; a name and its alias never double-count.
            # Alias lookup is scoped to the kids only — never every person
            # entity (teachers, parents are persons too and stay unqueried).
            people_by_name = {
                entity.name: entity
                for entity in self._repo.list_entities(kinds=["person"])
            }
            alias_map = {
                kid["name"]: self._repo.aliases_for(people_by_name[kid["name"]].id)
                for kid in kids
                if kid["name"] in people_by_name
            }
            known: list[str] = []
            for kid in kids:
                candidates = [kid["name"], *alias_map.get(kid["name"], [])]
                if any(
                    is_name_token(candidate, token)
                    for token in named
                    for candidate in candidates
                ):
                    known.append(kid["name"])
            return known if known else ["unknown"]

        raw_grades = audience.get("grades")
        if raw_grades is not None and _parse_grades(raw_grades) is None:
            # a grade constraint we cannot read ('???') — never guess a match
            return ["unknown"]

        matches: list[dict[str, Any]] = []
        unconfirmed = False  # a kid-side fact needed to decide was missing
        for kid in kids:
            state = self._match_kid(kid, audience)
            if state == "match":
                matches.append(kid)
            elif state == "unconfirmed":
                unconfirmed = True
            # else: a known incompatibility (gender/class/grade mismatch)

        role = audience.get("role")
        if isinstance(role, str) and (
            role == "parents" or role.startswith("parents_of")
        ):
            # parents_of_grade_N maps to 'me' when I have a matching kid
            if matches:
                return ["me"]
            return ["unknown"] if unconfirmed else ["nobody"]
        if role == "staff":
            # school staff: neither my kids nor me
            return ["nobody"]

        if matches:
            return [kid["name"] for kid in matches]
        if unconfirmed:
            return ["unknown"]
        return ["nobody"]

    def _resolve_channel(self, name: str) -> list[str]:
        """Audience of a named channel (exact match) from its confirmed fact.

        'family' resolves at QUERY time to all confirmed kids + 'me', so a
        kid seeded later is included without touching the channel.
        """
        entity = self._repo.find_entity(kind="channel", name=name)
        if entity is None:
            return ["unknown"]
        for fact in self._repo.current_facts(
            subject_ids=[entity.id], status="confirmed"
        ):
            if fact.predicate != "applies_to" or fact.value is None:
                continue
            if fact.value == "me":
                return ["me"]
            if fact.value == "family":
                return sorted(kid["name"] for kid in self.kids()) + ["me"]
            if fact.value in {kid["name"] for kid in self.kids()}:
                return [fact.value]
            return ["unknown"]  # names a kid with no confirmed memory
        return ["unknown"]

    def _match_kid(self, kid: dict[str, Any], audience: dict[str, Any]) -> str:
        """'match' | 'incompatible' | 'unconfirmed' for one kid vs an audience."""
        state = "match"
        school = audience.get("school")
        if school is not None:
            kid_school = kid.get("school")
            if kid_school is None:
                state = "unconfirmed"
            elif _norm_label(kid_school) != _norm_label(school):
                return "incompatible"
        cls = audience.get("class")
        grades = _parse_grades(audience.get("grades"))
        if cls is None and grades:
            kid_grade = kid.get("grade")
            if kid_grade is None:
                state = "unconfirmed"
            elif str(kid_grade) not in grades:
                return "incompatible"
        elif cls is not None:
            kid_class = kid.get("class")
            if kid_class is None:
                state = "unconfirmed"
            elif _norm_label(kid_class) != _norm_label(cls):
                return "incompatible"
            kid_grade = kid.get("grade")
            if grades and kid_grade is not None and str(kid_grade) not in grades:
                return "incompatible"
            if grades and kid_grade is None:
                state = "unconfirmed"
        gender = audience.get("gender")
        if gender in ("girls", "boys"):
            want = "girl" if gender == "girls" else "boy"
            kid_gender = kid.get("gender")
            if kid_gender is None:
                state = "unconfirmed"
            elif kid_gender != want:
                return "incompatible"
        return state

    # -- entity / fact CRUD (PR 0b routes + cockpit tab sit on these) -------------

    def overview(
        self, *, status: str = "confirmed", q: str | None = None
    ) -> list[dict[str, Any]]:
        """Entities with their current facts of ``status``, grouped per entity.

        ``q`` filters by entity name/alias or fact value substring.
        """
        needle = (q or "").strip().lower()
        entities = self._repo.list_entities()
        if needle:
            wanted = set()
            for entity in entities:
                aliases = self._repo.aliases_for(entity.id)
                if needle in entity.name.lower() or any(
                    needle in alias.lower() for alias in aliases
                ):
                    wanted.add(entity.id)
            for fact in self._repo.search_facts(needle):
                wanted.add(fact.subject_id)
            entities = [entity for entity in entities if entity.id in wanted]
        out = []
        for entity in entities:
            facts = self._repo.current_facts(subject_ids=[entity.id], status=status)
            out.append(
                {
                    **_entity_dict(entity, self._repo.aliases_for(entity.id)),
                    "facts": [_fact_dict(fact) for fact in facts],
                }
            )
        return out

    def add_entity(
        self, kind: str, name: str, *, aliases: list[str] | None = None
    ) -> dict[str, Any]:
        """Create an entity (409-dupes are the route's call; find first here)."""
        existing = self._repo.find_entity(kind=kind, name=name)
        if existing is not None:
            return _entity_dict(existing, self._repo.aliases_for(existing.id))
        entity = self._repo.add_entity(Entity(kind=kind, name=name))
        for alias in aliases or []:
            self._repo.add_alias(
                EntityAlias(entity_id=entity.id, alias=alias, lang=None)
            )
        self._repo.commit()
        return _entity_dict(entity, self._repo.aliases_for(entity.id))

    def patch_entity(self, entity_id: uuid.UUID, *, name: str | None = None) -> dict[str, Any]:
        """Rename an entity (identity stays — facts keep pointing here)."""
        entity = self._repo.get_entity(entity_id)
        if entity is None:
            raise KeyError(entity_id)
        if name:
            entity.name = name
        self._repo.commit()
        return _entity_dict(entity, self._repo.aliases_for(entity.id))

    def delete_entity(self, entity_id: uuid.UUID) -> None:
        """Hard-delete an entity (aliases + closed-fact history cascade).

        Refuses while the entity still has ANY current fact (valid_to NULL,
        any status): the user soft-deletes facts first — delete-with-history
        (design item 31a), never a silent wipe of live memory.
        """
        entity = self._repo.get_entity(entity_id)
        if entity is None:
            raise KeyError(entity_id)
        if self._repo.current_facts(subject_ids=[entity.id]):
            raise MemoryError(
                "entity still has current facts — soft-delete them first"
            )
        self._repo.delete_entity(entity)
        self._repo.commit()

    def add_fact(
        self,
        subject_id: uuid.UUID,
        predicate: str,
        *,
        object_id: uuid.UUID | None = None,
        value: str | None = None,
        valid_from: datetime | None = None,
        status: str = "confirmed",
        provenance: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Insert a fact row (PR A inserts proposals with the same call)."""
        if self._repo.get_entity(subject_id) is None:
            raise KeyError(subject_id)
        if object_id is None and value is None:
            raise ValueError("a fact needs an object or a value")
        fact = self._repo.add_fact(
            MemoryFact(
                subject_id=subject_id,
                predicate=predicate,
                object_id=object_id,
                value=value,
                status=status,
                valid_from=valid_from or utcnow(),
                provenance=provenance or {"source": "user"},
            )
        )
        self._repo.commit()
        return _fact_dict(fact)

    def patch_fact(self, fact_id: uuid.UUID, **changes: Any) -> dict[str, Any]:
        """Edit a fact = close the old version + open a new one (never in-place).

        A CLOSED fact (superseded, or soft-deleted/rejected) must not be
        edited: that would silently resurrect history. Raise instead.
        """
        old = self._repo.get_fact(fact_id)
        if old is None:
            raise KeyError(fact_id)
        if old.valid_to is not None:
            raise MemoryError("cannot edit a closed fact (superseded or deleted)")
        now = utcnow()
        self._repo.close_fact(old, at=now)
        new = MemoryFact(
            subject_id=old.subject_id,
            predicate=changes.get("predicate", old.predicate),
            object_id=changes.get("object_id", old.object_id),
            value=changes.get("value", old.value),
            status=old.status,
            valid_from=now,
            supersedes_id=old.id,
            provenance=old.provenance,
        )
        if new.object_id is None and new.value is None:
            raise ValueError("a fact needs an object or a value")
        created = self._repo.add_fact(new)
        self._repo.commit()
        return _fact_dict(created)

    def delete_fact(self, fact_id: uuid.UUID) -> dict[str, Any]:
        """Soft-delete: status 'rejected', valid_to=now; history is kept."""
        fact = self._repo.get_fact(fact_id)
        if fact is None:
            raise KeyError(fact_id)
        if fact.valid_to is not None:
            raise MemoryError("fact is already closed")
        fact.status = "rejected"
        fact.valid_to = utcnow()
        self._repo.commit()
        return _fact_dict(fact)

    def confirm_fact(self, fact_id: uuid.UUID) -> dict[str, Any]:
        """proposed -> confirmed (one tap from the Proposed inbox)."""
        fact = self._repo.get_fact(fact_id)
        if fact is None:
            raise KeyError(fact_id)
        if fact.status != "proposed":
            raise MemoryError(f"only proposed facts can be confirmed (status={fact.status})")
        fact.status = "confirmed"
        self._repo.commit()
        return _fact_dict(fact)

    def fact_history(self, fact_id: uuid.UUID) -> list[dict[str, Any]]:
        """The full version chain around a fact (older first)."""
        anchor = self._repo.get_fact(fact_id)
        if anchor is None:
            raise KeyError(fact_id)
        chain = {anchor.id: anchor}
        cursor: MemoryFact | None = anchor
        while cursor is not None and cursor.supersedes_id not in chain:
            previous = self._repo.get_fact(cursor.supersedes_id) if cursor.supersedes_id else None
            if previous is None:
                break
            chain[previous.id] = previous
            cursor = previous
        forward = [f for f in self._repo.facts_about(anchor.subject_id) if f.supersedes_id in chain]
        while forward:
            for fact in forward:
                chain.setdefault(fact.id, fact)
            forward = [
                f
                for f in self._repo.facts_about(anchor.subject_id)
                if f.supersedes_id in chain and f.id not in chain
            ]
        ordered = sorted(chain.values(), key=lambda f: (f.valid_from, f.created_at))
        return [_fact_dict(fact) for fact in ordered]

    # -- internals -----------------------------------------------------------------

    def _ensure_entity(self, *, kind: str, name: str) -> Entity:
        entity = self._repo.find_entity(kind=kind, name=name)
        if entity is None:
            entity = self._repo.add_entity(Entity(kind=kind, name=name))
        return entity

    def _current_confirmed_fact(self, subject_id: uuid.UUID, predicate: str) -> MemoryFact | None:
        for fact in self._repo.current_facts(subject_ids=[subject_id], status="confirmed"):
            if fact.predicate == predicate:
                return fact
        return None


def _parse_grades(raw: Any) -> set[str] | None:
    """Grades as strings; accepts [5, 6], ['5','6'], '5-6' or Hebrew forms.

    Hebrew spellings pass through textnorm first, which unifies geresh (ה'),
    gershayim and the maqaf/en-dash separators (ה־ו, ה–ו) into the plain
    forms parsed below; quote marks are then stripped ('ה\'' -> ה = 5).
    Unparseable input returns None (the constraint is then 'unconfirmed',
    never 'incompatible' — we must not claim a kid does not apply just
    because we failed to read the range).
    """
    from brain.services.textnorm import normalize  # noqa: PLC0415  (cheap, local)

    _QUOTE_CHARS = "\"'׳״’”"
    if raw is None:
        return None
    if isinstance(raw, (list, tuple, set)):
        return {
            str(_hebrew_grade(normalize(str(item)).strip(_QUOTE_CHARS)) or item)
            for item in raw
        }
    text = normalize(str(raw)).strip().strip(_QUOTE_CHARS)
    if not text:
        return None
    if "-" in text:
        # quote marks can appear on BOTH ends of the range (ה'-ו', ה׳–ו׳):
        # strip them per part after splitting, not just around the whole span
        low, _, high = text.partition("-")
        low_d = _hebrew_grade(low.strip().strip(_QUOTE_CHARS))
        high_d = _hebrew_grade(high.strip().strip(_QUOTE_CHARS))
        if low_d is None or high_d is None:
            return None
        return {str(g) for g in range(min(low_d, high_d), max(low_d, high_d) + 1)}
    single = _hebrew_grade(text)
    if single is None:
        return None
    return {str(single)}


def _hebrew_grade(text: str) -> int | None:
    cleaned = str(text).strip()
    if cleaned.isdigit():
        return int(cleaned)
    if len(cleaned) == 1:
        if cleaned in HEBREW_DIGITS:
            return HEBREW_DIGITS[cleaned]
        if cleaned in HEBREW_TENS:
            return HEBREW_TENS[cleaned]  # bare י = grade 10
    if len(cleaned) == 2 and cleaned[0] in HEBREW_TENS and cleaned[1] in HEBREW_DIGITS:
        return HEBREW_TENS[cleaned[0]] + HEBREW_DIGITS[cleaned[1]]
    return None


def _norm_label(value: Any) -> str:
    """Comparable form of a class/school/teacher label (A2 run-#1 fix 1).

    Both sides of every comparison go through this: textnorm.normalize
    (geresh/gershayim, maqaf/dashes, nikud, bidi, whitespace, case) plus
    label-specific folding — quote marks dropped (ה' -> ה), dashes to
    spaces (ה-ספיר -> ה ספיר), and a leading כיתה/class word stripped
    (כיתה ה' ספיר -> ה ספיר). Memory stores e.g. ה' ספיר while text or
    the LLM may write ה׳ ספיר, ה ספיר, כיתה ה' ספיר or ה-ספיר.
    """
    import re as _re  # noqa: PLC0415

    from brain.services.textnorm import normalize  # noqa: PLC0415

    text = normalize(str(value))
    text = _re.sub("['\"]", "", text)
    text = _re.sub("[-]+", " ", text)
    text = " ".join(text.split())
    for prefix in ("כיתה", "class"):
        if text.startswith(prefix + " "):
            text = text[len(prefix) + 1 :]
    return text.strip()
