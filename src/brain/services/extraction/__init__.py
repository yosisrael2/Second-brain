"""Meaning extraction: turn one capture into grounded CHILD items.

Design: meaning-extraction-spec.md + v2 delta (the delta wins). PR A1 ships
the data model, the durable queue, the deterministic validation pipeline and
a stub extractor; the real LLM extractor lands in A2 behind the same
interface (:class:`~brain.services.extraction.base.Extractor`).
"""
