"""Ask use cases: answer a question from retrieved context (Service layer)."""

from __future__ import annotations

import hashlib
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol
from zoneinfo import ZoneInfo

from brain import ops
from brain.ai.ask import LLMClient, answer
from brain.db.models import MemoryItem
from brain.services.search import SearchHit, SearchService
from brain.services.temporal import parse_question_ranges
from brain.services.triage import mentions_name

DEFAULT_TOP_K = 8
# Candidates fetched from semantic search before truncating to top_k. The
# reranker can only reorder what reaches it: measured on the live corpus, an
# English question against Hebrew emails scores ~0.2 cosine (the TALAN email
# sits at vector rank 23) — with a pool of 8 the reranker never sees it; at
# 32 it lands in the top-8 after rerank. One rerank call, ~4x documents.
DEFAULT_RECALL_POOL = 32
# Each source is truncated in the LLM prompt only; the API response keeps the
# full raw_text. Bounds prompt size (top_k x raw_text can otherwise reach
# ~50k chars) and blunts cost/latency surprises.
SOURCE_PROMPT_MAX_CHARS = 2000
# Day/date questions fetch every event in the resolved range; this cap keeps
# a week-long range from swallowing the whole prompt.
TEMPORAL_FETCH_CAP = 30
# Kid-recall leg (PR 0d): when a question names a confirmed kid, items from
# that kid's channels/class join the recall pool before the rerank. Default
# cap for the leg (Settings.ask_kid_recall_limit overrides).
DEFAULT_KID_RECALL_LIMIT = 16
# WA39 run 3: the answer to a question about day D is often an announcement
# SENT on D-1 ('tomorrow the class is at the farm') — the kid-channel leg
# bounds by occurred_at (the SENT time), so it widens the resolved window's
# START by this many days. Settings.ask_kid_recall_lookback_days overrides.
DEFAULT_KID_RECALL_LOOKBACK_DAYS = 3


@dataclass(frozen=True)
class AskResult:
    """A grounded answer plus the items it was built from."""

    answer: str
    cited_ids: list[str] = field(default_factory=list)
    sources: list[SearchHit] = field(default_factory=list)
    model: str | None = None


def _unique_by_text(hits: list[SearchHit]) -> list[SearchHit]:
    """Collapse hits with identical raw_text (split WhatsApp chunks)."""
    seen: set[str] = set()
    unique: list[SearchHit] = []
    for hit in hits:
        if hit.item.raw_text not in seen:
            seen.add(hit.item.raw_text)
            unique.append(hit)
    return unique


class MemoryServiceProtocol(Protocol):
    """What the kid-recall leg needs from MemoryService (overview)."""

    def overview(
        self, *, status: str = "confirmed", q: str | None = None
    ) -> list[dict[str, Any]]: ...


class KidRecallRepoProto(Protocol):
    """What the kid-recall leg needs from the items repository."""

    def by_text_any(self, needles: Sequence[str], *, limit: int) -> Sequence[MemoryItem]: ...

    def conflict_counterparts(
        self, item_ids: Sequence[uuid.UUID], *, limit: int
    ) -> Sequence[tuple[Any, MemoryItem]]: ...


# PR-b: at most this many conflict counterparts join the context (the plan's
# cap — beyond four the answer drowns; the rest stay relations).
DEFAULT_CONFLICT_LEG_LIMIT = 4


class AskService:
    """Answers questions over the memory store: search top-k, then LLM.

    Day/date questions ("Tuesday", "Friday evening", "השבוע") cannot be
    answered by top-k similarity alone: :mod:`brain.services.temporal`
    resolves them to [start, end) ranges and EVERY item occurring in the
    range is merged into the context AHEAD of the semantic hits (dedupe by
    id and identical text). LLM failures propagate as
    :class:`brain.ai.ask.LLMError` (routers map them to 502); search itself
    already degrades to keyword fallback.
    """

    def __init__(
        self,
        search: SearchService,
        llm: LLMClient,
        top_k: int = DEFAULT_TOP_K,
        recall_pool: int = DEFAULT_RECALL_POOL,
        tz: ZoneInfo | None = None,
        memory_block: Callable[[], str] | None = None,
        memory: MemoryServiceProtocol | None = None,
        items_repo: KidRecallRepoProto | None = None,
        kid_recall_limit: int = DEFAULT_KID_RECALL_LIMIT,
        kid_recall_lookback_days: int = DEFAULT_KID_RECALL_LOOKBACK_DAYS,
        conflict_leg_limit: int = DEFAULT_CONFLICT_LEG_LIMIT,
    ) -> None:
        self._search = search
        self._llm = llm
        self._top_k = top_k
        self._recall_pool = recall_pool
        self._tz = tz or ZoneInfo("UTC")
        # Returns the confirmed-memory prompt block ('' when memory is empty);
        # empty output keeps the prompt byte-identical to the pre-memory format.
        self._memory_block = memory_block
        # PR 0d kid-recall leg: both must be present for the leg to run.
        self._memory = memory
        self._items_repo = items_repo
        self._kid_recall_limit = kid_recall_limit
        self._kid_recall_lookback_days = kid_recall_lookback_days
        # PR-b conflict leg: both must be present for the leg to run.
        self._conflict_leg_limit = conflict_leg_limit

    def ask(
        self, question: str, *, now: datetime | None = None, record_ops: bool = True
    ) -> AskResult:
        """Answer ``question`` from the top-k most relevant memory items.

        ``now`` anchors relative date expressions ("today", "this week");
        production passes nothing (real clock), tests pin it.

        ``record_ops=False`` runs the whole path WITHOUT recording ops
        spans/events (ask->search, ask->llm, kid_recall). DIAGNOSTICS ONLY:
        live checks that exercise the ask path with a fake LLM must pass it,
        otherwise /ops/health shows 'llm — last call failed' until the next
        real call. Real requests keep the default (True) — no change.

        A SYNTHETIC client (FakeLLMClient and its subclasses) never records
        ops regardless of ``record_ops``: the default True must stay safe
        for verification runs against a live DB (they poison llm health
        otherwise), so real-LLM evidence is the only thing /ops/health's
        llm component reads.
        """
        # Fail-safe default: a synthetic (test) client's events are not
        # evidence of real llm health — write none at all (no partial
        # ask->search spans either, so the run leaves zero ops rows).
        recording = record_ops and not getattr(self._llm, "synthetic", False)
        span_factory = ops.span if recording else _null_span
        with span_factory("ask", "search", edge="ask->search") as span:
            # rerank sees the whole pool; only the top_k best reach the prompt.
            # No kid named -> the call is EXACTLY the pre-0d one (byte-identical
            # pool; also keeps third-party search fakes without the kwarg working).
            kid_items = self._kid_recall_items(
                question, record_ops=recording, now=now
            )
            if kid_items:
                pool = self._search.search(
                    question,
                    limit=self._recall_pool,
                    extra_items=kid_items,
                )
            else:
                pool = self._search.search(question, limit=self._recall_pool)
            hits = pool[: self._top_k]
            pool_ids = [str(hit.item.id) for hit in pool]
            conflict_hits, conflict_map = self._conflict_leg(
                pool_ids, recording=recording
            )
            span.detail = {
                "kid_leg_fired": bool(kid_items),
                "kid_leg_count": len(kid_items),
                "conflict_leg_count": len(conflict_hits),
                "pool_size": len(pool_ids),
                "pool_hash": hashlib.sha1(",".join(pool_ids).encode()).hexdigest()[:12],
            }
        context_hits = self._temporal_hits(question, now) + hits + conflict_hits
        context_hits = _unique_by_text(self._unique_by_id(context_hits))
        context = [
            {
                "id": str(hit.item.id),
                "text": hit.item.raw_text[:SOURCE_PROMPT_MAX_CHARS],
                "when": self._when_of(hit.item),
                # PR-b: only set when the item participates in a conflicts
                # relation inside the context — absent otherwise, so prompts
                # without conflicts stay byte-identical.
                **(
                    {"conflict_with": conflict_map[str(hit.item.id)]}
                    if str(hit.item.id) in conflict_map
                    else {}
                ),
            }
            for hit in context_hits
        ]
        with span_factory("ask", "llm", edge="ask->llm") as span:
            result = answer(
                question,
                context,
                llm=self._llm,
                now=now,
                tz=self._tz,
                memory_block=self._memory_block() if self._memory_block else "",
            )
            span.detail = {"model": result["model"], "cited": len(result["cited_ids"])}
        return AskResult(
            answer=result["answer"],
            cited_ids=result["cited_ids"],
            sources=_unique_by_text(context_hits),
            model=result["model"],
        )

    def _kid_recall_items(
        self,
        question: str,
        *,
        record_ops: bool = True,
        now: datetime | None = None,
    ) -> list[MemoryItem]:
        """Extra candidates: items from a named kid's channels and class (PR 0d).

        The question is NEVER rewritten — this only widens recall. A kid is
        named when the question contains their name or an alias
        (case-insensitive); their channels are the ones memory says apply to
        them (MemoryService.channel_names_for_kid — THE one channel->kid
        mapping; a channel's confirmed ``applies_to`` is that kid or
        ``family``). Those channels' WhatsApp conversations join the pool TWICE
        over: by their STRUCTURED meta.channel (bounded by the question's
        resolved time window when one resolves — the SAME parse the temporal
        leg runs — widened at its START by ask_kid_recall_lookback_days,
        since the announcement about day D is usually SENT on D-1 and
        occurred_at is the sent time; WA39 run 1: a mid-week question must
        not drown under the newest items of the same channels — else the
        newest-N cap, never all history) and by the channel NAME as a text
        needle (captures predating meta.channel keep the chat name only in
        their raw_text header). The
        kid's class name stays a text needle too (a class is not a channel;
        nothing structured carries it). Noise-triaged conversations are
        excluded, exactly as from listings. No kid named ->
        no leg at all: behaviour is byte-identical to the pre-0d pipeline.
        """
        if self._memory is None or self._items_repo is None:
            return []
        entities = self._memory.overview(status="confirmed", q=None)

        def mentions(name: str) -> bool:
            # THE shared whole-word matcher (kid-recall + triage): boundaries,
            # Hebrew glue prefixes, case-insensitive.
            return mentions_name(name, question)

        kids: list[dict[str, Any]] = []
        for entity in entities:
            facts = entity.get("facts", [])
            if entity.get("kind") == "person" and any(
                fact.get("predicate") == "grade" for fact in facts
            ):
                kids.append(entity)
        needles: list[str] = []
        kid_channels: set[str] = set()
        for kid in kids:
            names = [kid.get("name", ""), *(kid.get("aliases") or [])]
            if not any(mentions(name) for name in names):
                continue
            kid_channels.update(self._memory.channel_names_for_kid(kid.get("name", "")))
            class_value = next(
                (
                    fact.get("value")
                    for fact in kid.get("facts", [])
                    if fact.get("predicate") == "class" and fact.get("value")
                ),
                None,
            )
            if class_value:
                needles.append(class_value)
        # The channel names double as text needles: captures predating
        # meta.channel keep the chat name only in their raw_text header, and
        # the same strings drive the structured query above.
        needles.extend(sorted(kid_channels))
        needles = [needle for needle in dict.fromkeys(needles) if needle]
        items: dict[uuid.UUID, MemoryItem] = {}
        if needles:
            for item in self._items_repo.by_text_any(
                needles, limit=self._kid_recall_limit
            ):
                items[item.id] = item
        window_ids: set[uuid.UUID] = set()
        lookback_ids: set[uuid.UUID] = set()
        if kid_channels:
            # The SAME date resolution the temporal leg runs: with a resolved
            # window the channel leg is bounded BY the window widened at its
            # START by lookback days — the announcement about day D is usually
            # SENT on D-1 (WA39 run 3; occurred_at is the SENT time), while a
            # mid-week question must still not drown under the newest items
            # of the same channels (WA39 run 1). Without a resolved window it
            # falls back to newest-N, never all history.
            ranges = parse_question_ranges(
                question, self._tz, now or datetime.now(UTC)
            )
            if ranges:
                lookback = timedelta(days=max(self._kid_recall_lookback_days, 0))
                for start, end in ranges:
                    for item in self._items_repo.by_channels(
                        kid_channels, limit=self._kid_recall_limit,
                        ranges=[(start, end)],
                    ):
                        items.setdefault(item.id, item)
                        window_ids.add(item.id)
                    if lookback:
                        for item in self._items_repo.by_channels(
                            kid_channels, limit=self._kid_recall_limit,
                            ranges=[(start - lookback, start)],
                        ):
                            items.setdefault(item.id, item)
                            lookback_ids.add(item.id)
            else:
                for item in self._items_repo.by_channels(
                    kid_channels,
                    limit=self._kid_recall_limit,
                    ranges=None,
                ):
                    items.setdefault(item.id, item)
        if not items:
            return []
        # The overall extra pool still respects the kid-recall cap, with slot
        # priority: in-window channel items (what the question is about —
        # WA39 run 1), then lookback items (the announcements SENT before the
        # window opens — WA39 run 3), then the newest text-needle items. The
        # newest flood must not crowd the earlier buckets out. Without a
        # resolved window everything competes by recency.
        window_items = [i for i in items.values() if i.id in window_ids]
        lookback_items = [
            i for i in items.values()
            if i.id in lookback_ids and i.id not in window_ids
        ]
        rest = [
            i for i in items.values()
            if i.id not in window_ids and i.id not in lookback_ids
        ]
        rest.sort(
            key=lambda i: (
                i.occurred_at or datetime.min.replace(tzinfo=UTC),
                i.created_at,
            ),
            reverse=True,
        )
        pool = (window_items + lookback_items + rest)[: self._kid_recall_limit]
        if record_ops:
            ops.record_event(
                "ask",
                "kid_recall",
                ok=True,
                detail={
                    "needles": len(needles),
                    "channels": len(kid_channels),
                    "fetched": len(pool),
                },
            )
        return pool

    def _conflict_leg(
        self, pool_ids: list[str], *, recording: bool
    ) -> tuple[list[SearchHit], dict[str, list[str]]]:
        """PR-b (item 38's ask leg): pull conflict counterparts into the context.

        Every conflicts relation touching a recalled item contributes its
        OTHER side to the context — even when that side is only a PROPOSED
        child (a contradiction the user has not confirmed yet is exactly
        what the answer must quote next to the current version). Both ends
        of every in-context relation are marked ``conflict_with`` so the
        prompt renders the ⚠️ line and the answer quotes both versions.

        Returns (counterpart hits, item_id -> [conflicting item ids]). With
        no repository or no relations: ([], {}) — the pipeline is
        byte-identical to the pre-leg one. An older repository without the
        method (third-party fakes) gets no leg, no change.
        """
        if self._items_repo is None or not pool_ids:
            return [], {}
        repo_method = getattr(self._items_repo, "conflict_counterparts", None)
        if repo_method is None:
            return [], {}
        try:
            uuids = [uuid.UUID(item_id) for item_id in pool_ids]
        except ValueError:
            return [], {}
        pairs = list(repo_method(uuids, limit=self._conflict_leg_limit))
        if not pairs:
            return [], {}
        pool_set = set(uuids)
        pulled: dict[uuid.UUID, MemoryItem] = {}
        for _rel, other in pairs:
            if other.id not in pool_set:
                pulled.setdefault(other.id, other)
        context_ids = pool_set | set(pulled)
        conflict_map: dict[str, list[str]] = {}
        for rel, _other in pairs:
            a, b = rel.from_id, rel.to_id
            if a in context_ids and b in context_ids:
                conflict_map.setdefault(str(a), [])
                if str(b) not in conflict_map[str(a)]:
                    conflict_map[str(a)].append(str(b))
                conflict_map.setdefault(str(b), [])
                if str(a) not in conflict_map[str(b)]:
                    conflict_map[str(b)].append(str(a))
        if recording:
            ops.record_event(
                "ask",
                "conflict_leg",
                ok=True,
                detail={"pulled": len(pulled), "relations": len(pairs)},
            )
        return [SearchHit(item=item, score=None) for item in pulled.values()], conflict_map

    def _temporal_hits(self, question: str, now: datetime | None) -> list[SearchHit]:
        """Fetch every item inside the question's resolved date ranges."""
        instant = now or datetime.now(UTC)
        ranges = parse_question_ranges(question, self._tz, instant)
        hits: list[SearchHit] = []
        for start, end in ranges:
            hits.extend(self._search.by_occurrence(start, end, limit=TEMPORAL_FETCH_CAP))
        return hits

    def _unique_by_id(self, hits: list[SearchHit]) -> list[SearchHit]:
        """Order-preserving id dedupe (temporal + semantic can overlap)."""
        seen: set[str] = set()
        unique: list[SearchHit] = []
        for hit in hits:
            key = str(hit.item.id)
            if key not in seen:
                seen.add(key)
                unique.append(hit)
        return unique

    def _when_of(self, item: MemoryItem) -> str | None:
        """Local When-line for the prompt ('Fri 23 Oct 19:00-22:00'), or None."""
        start = item.occurred_at
        if start is None:
            return None
        local = start.astimezone(self._tz)
        end = (item.meta or {}).get("end") or (item.meta or {}).get("dtend")
        if isinstance(end, str):
            try:
                end_dt = datetime.fromisoformat(end)
                end_str = f"-{end_dt.astimezone(self._tz):%H:%M}"
            except ValueError:
                end_str = ""
        else:
            end_str = ""
        return f"{local:%a} {local.day} {local:%b} {local:%H:%M}{end_str}"


class _NullSpan:
    """Stand-in for ops.span when recording is off (diagnostics runs)."""

    def __init__(self) -> None:
        self.detail: dict[str, Any] = {}

    def __enter__(self) -> _NullSpan:
        return self

    def __exit__(self, *exc: object) -> None:
        return None


def _null_span(component: str, kind: str, edge: str | None = None) -> _NullSpan:
    """Drop-in for ops.span that records nothing."""
    return _NullSpan()
