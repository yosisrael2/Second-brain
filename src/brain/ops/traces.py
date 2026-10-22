"""Capture / search / ask dry-run traces — step-by-step, no side effects.

Each trace returns ``{"steps": [{step, ok, latency_ms, detail}]}``. The
capture trace NEVER writes (no item row, no vector); the ask trace uses the
real LLM (user-initiated button, costs tokens); everything else is
read-only. Item bodies stay out of the step details — ids, scores, lengths.
"""

from __future__ import annotations

import math
import time
from collections.abc import Mapping
from typing import Any

from brain.config import get_settings
from brain.services.ask import SOURCE_PROMPT_MAX_CHARS


def _step(step: str, ok: bool, started: float, detail: dict[str, Any]) -> dict[str, Any]:
    return {
        "step": step,
        "ok": ok,
        "latency_ms": round((time.perf_counter() - started) * 1000, 1),
        "detail": detail,
    }


def _session_services() -> tuple[Any, Any, Any]:
    """Fresh services over the shared AI clients (read-only usage)."""
    from brain.api import deps  # noqa: PLC0415
    from brain.db.session import SessionLocal  # noqa: PLC0415
    from brain.repositories.items import ItemsRepository  # noqa: PLC0415
    from brain.services.ask import AskService  # noqa: PLC0415
    from brain.services.search import SearchService  # noqa: PLC0415

    session = SessionLocal()
    search = SearchService(
        ItemsRepository(session),
        deps.get_embedder(),
        deps.get_vector_store(),
        deps.get_reranker(),
    )
    return session, search, AskService(search, deps.get_llm_client())


def trace_capture(text: str) -> dict[str, Any]:
    """Paste detection → Jev classification → embedding → Qdrant neighbours."""
    from brain.ai.classify import route  # noqa: PLC0415
    from brain.api import deps  # noqa: PLC0415
    from brain.services.paste import parse as parse_paste  # noqa: PLC0415

    steps: list[dict[str, Any]] = []
    kind = "note"

    started = time.perf_counter()
    try:
        parsed = parse_paste(text)
        kind = parsed.kind
        steps.append(_step("parse", True, started, {
            "kind": parsed.kind,
            "chunks": len(parsed.chunks),
            "chars": [len(chunk.raw_text) for chunk in parsed.chunks],
        }))
        first_text = parsed.chunks[0].raw_text if parsed.chunks else text
    except Exception as exc:  # noqa: BLE001  (detection never raises by contract)
        steps.append(_step("parse", False, started,
                           {"error": f"{type(exc).__name__}: {exc!s}"[:200]}))
        first_text = text

    started = time.perf_counter()
    try:
        result = deps.get_classifier().classify(first_text)
        confidence = float(result.get("confidence") or 0)
        steps.append(_step("classify", True, started, {
            "labels": {
                key: str(result[key])
                for key in ("type", "project", "importance", "actionability")
                if result.get(key) is not None
            },
            "confidence": confidence,
            "routing": str(route(confidence)),
        }))
    except Exception as exc:  # noqa: BLE001
        steps.append(_step("classify", False, started,
                           {"error": f"{type(exc).__name__}: {exc!s}"[:200]}))

    vector: list[float] | None = None
    started = time.perf_counter()
    try:
        vector = deps.get_embedder().embed(first_text)
        norm = math.sqrt(sum(v * v for v in vector))
        steps.append(_step("embed", True, started, {
            "dims": len(vector), "norm": round(norm, 4),
            "provider": get_settings().embedder_provider,
        }))
    except Exception as exc:  # noqa: BLE001
        steps.append(_step("embed", False, started,
                           {"error": f"{type(exc).__name__}: {exc!s}"[:200]}))

    started = time.perf_counter()
    neighbours: list[dict[str, Any]] = []
    if vector is not None:
        try:
            raw = deps.get_vector_store().search(vector, k=5)
            neighbours = [
                {"id": str(hit["id"]), "score": round(float(hit["score"]), 4)} for hit in raw
            ]
            steps.append(_step("qdrant", True, started, {"neighbours": neighbours}))
        except Exception as exc:  # noqa: BLE001
            steps.append(_step("qdrant", False, started,
                               {"error": f"{type(exc).__name__}: {exc!s}"[:200]}))
    else:
        steps.append(_step("qdrant", False, started, {"error": "no vector (embed failed)"}))

    return {"kind": kind, "steps": steps, "neighbours": neighbours}


def trace_search(query: str) -> dict[str, Any]:
    """Embed → raw Qdrant candidates (cosine) → rerank before/after → fallback."""
    from brain.api import deps  # noqa: PLC0415
    from brain.db.session import SessionLocal  # noqa: PLC0415
    from brain.repositories.items import ItemsRepository  # noqa: PLC0415
    from brain.services.search import SearchService  # noqa: PLC0415

    steps: list[dict[str, Any]] = []

    started = time.perf_counter()
    try:
        vector = deps.get_embedder().embed(query)
        steps.append(_step("embed", True, started, {"dims": len(vector)}))
    except Exception as exc:  # noqa: BLE001
        steps.append(_step("embed", False, started,
                           {"error": f"{type(exc).__name__}: {exc!s}"[:200]}))
        return _fallback_step(steps, f"embed failed: {type(exc).__name__}")

    started = time.perf_counter()
    try:
        raw = deps.get_vector_store().search(vector, k=10)
        candidates: list[dict[str, Any]] = [
            {"id": str(hit["id"]),
             "vector_score": round(float(hit["score"]), 4),
             "chars": len(str((hit.get("payload") or {}).get("text", "")))}
            for hit in raw
        ]
        steps.append(_step("qdrant", True, started, {"candidates": candidates}))
    except Exception as exc:  # noqa: BLE001
        steps.append(_step("qdrant", False, started,
                           {"error": f"{type(exc).__name__}: {exc!s}"[:200]}))
        return _fallback_step(steps, f"qdrant failed: {type(exc).__name__}")

    started = time.perf_counter()
    session = SessionLocal()
    try:
        service = SearchService(
            ItemsRepository(session),
            deps.get_embedder(),
            deps.get_vector_store(),
            deps.get_reranker(),
        )
        ranked: list[Mapping[str, Any]] = list(
            deps.get_reranker().rerank(query, candidates, top_n=10)
        )
        steps.append(_step("rerank", True, started, {
            "before": [c["id"][:8] for c in candidates],
            "after": [str(c.get("id", "?"))[:8] for c in ranked],
            "rerank_used": True,
        }))
        hits = service._load_items(ranked)  # noqa: SLF001  (trace reuses the loader)
        steps.append(_step("resolve", True, time.perf_counter(), {
            "items": [{"id": str(h.item.id)[:8], "score": _fmt_score(h.score)} for h in hits],
        }))
    except Exception as exc:  # noqa: BLE001
        steps.append(_step("rerank", False, started,
                           {"error": f"{type(exc).__name__}: {exc!s}"[:200]}))
    finally:
        session.close()

    return _fallback_step(steps, None)


def _fmt_score(score: float | None) -> float | None:
    return round(score, 4) if score is not None else None


def _fallback_step(steps: list[dict[str, Any]], reason: str | None) -> dict[str, Any]:
    steps.append(_step("keyword_fallback", True, time.perf_counter(),
                       {"taken": reason is not None, "reason": reason}))
    return {"steps": steps}


def trace_ask(question: str) -> dict[str, Any]:
    """Search → per-source prompt sizes → real LLM → answer + citations."""
    from brain.ai.ask import LLMError  # noqa: PLC0415

    steps: list[dict[str, Any]] = []
    session, search, ask_service = _session_services()
    try:
        started = time.perf_counter()
        try:
            hits = list(search.search(question, limit=5))
            steps.append(_step("search", True, started, {
                "sources": [
                    {"id": str(hit.item.id), "score": _fmt_score(hit.score),
                     "chars": len(hit.item.raw_text)}
                    for hit in hits
                ],
            }))
        except Exception as exc:  # noqa: BLE001
            steps.append(_step("search", False, started,
                               {"error": f"{type(exc).__name__}: {exc!s}"[:200]}))
            return {"steps": steps}

        started = time.perf_counter()
        context = [
            {"id": str(hit.item.id),
             "raw_chars": len(hit.item.raw_text),
             "prompt_chars": min(len(hit.item.raw_text), SOURCE_PROMPT_MAX_CHARS),
             "truncated": len(hit.item.raw_text) > SOURCE_PROMPT_MAX_CHARS}
            for hit in hits
        ]
        steps.append(_step("prompt", True, started, {
            "sources": context,
            "total_prompt_chars": sum(entry["prompt_chars"] for entry in context),
        }))

        started = time.perf_counter()
        try:
            result = ask_service.ask(question)
            steps.append(_step("llm", True, started, {
                "answer": result.answer[:400],
                "cited_ids": result.cited_ids,
                "model": result.model,
                "latency_ms": round((time.perf_counter() - started) * 1000, 1),
            }))
            return {"steps": steps, "answer": result.answer,
                    "cited_ids": result.cited_ids, "model": result.model}
        except LLMError as exc:
            steps.append(_step("llm", False, started,
                               {"error": f"LLMError: {exc!s}"[:200]}))
            return {"steps": steps}
    finally:
        session.close()
