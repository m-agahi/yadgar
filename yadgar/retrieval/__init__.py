"""Retrieval package — re-exports public API for backward compatibility.

v5.31.0 additions:
- ``RetrievalPipeline`` — plugin pipeline orchestrator
- ``RetrievalState``   — inter-stage state carrier
- ``recall_compare``   — A/B comparison harness
- ``get_profile``      — profile dict lookup with validation
"""

from yadgar.retrieval.compare import recall_compare
from yadgar.retrieval.core import Retriever
from yadgar.retrieval.entities import _extract_query_entities
from yadgar.retrieval.pipeline import RetrievalPipeline
from yadgar.retrieval.profiles import get_profile
from yadgar.retrieval.query_analysis import (
    _derive_implied_fact_passages,
    _pseudo_hyde_expand,
    _question_to_statement,
    analyze_query,
)
from yadgar.retrieval.state import RetrievalState

__all__ = [
    "Retriever",
    "RetrievalPipeline",
    "RetrievalState",
    "get_profile",
    "recall_compare",
    "_derive_implied_fact_passages",
    "_extract_query_entities",
    "_pseudo_hyde_expand",
    "_question_to_statement",
    "analyze_query",
]
