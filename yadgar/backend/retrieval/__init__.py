"""Retrieval implementation package (T2 Car E2 sink, ADR-0078).

Moved from ``yadgar._shared.retrieval``: after the landscape-recall forward,
both retrieval executors are backend-side (the /recall fan-out pipeline in
``recall_pipeline`` + ``predictive_coding``) — the dual-import law puts the
implementation here, next to the DB and the ML engines. The contract half
(``profiles``) stays in ``yadgar._shared.retrieval``.

Composition: ``compose.ensure_retrieval_engine`` builds the process-global
``_st._retriever`` lazily (Car B ``ensure_restoration_engines`` precedent) —
the shared composition root no longer constructs it.
"""

# Contract re-export: profiles stay in _shared (dual); re-exported here so the
# backend package presents the full retrieval API surface.
from yadgar._shared.retrieval.profiles import get_profile
from yadgar.backend.retrieval.compare import recall_compare
from yadgar.backend.retrieval.compose import ensure_retrieval_engine
from yadgar.backend.retrieval.core import Retriever
from yadgar.backend.retrieval.entities import _extract_query_entities
from yadgar.backend.retrieval.pipeline import RetrievalPipeline
from yadgar.backend.retrieval.query_analysis import (
    _derive_implied_fact_passages,
    _pseudo_hyde_expand,
    _question_to_statement,
    analyze_query,
)
from yadgar.backend.retrieval.state import RetrievalState

__all__ = [
    "Retriever",
    "get_profile",
    "RetrievalPipeline",
    "RetrievalState",
    "ensure_retrieval_engine",
    "recall_compare",
    "_derive_implied_fact_passages",
    "_extract_query_entities",
    "_pseudo_hyde_expand",
    "_question_to_statement",
    "analyze_query",
]
