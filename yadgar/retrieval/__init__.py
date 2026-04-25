"""Retrieval package — re-exports public API for backward compatibility."""

from yadgar.retrieval.core import Retriever
from yadgar.retrieval.entities import _extract_query_entities
from yadgar.retrieval.query_analysis import (
    _derive_implied_fact_passages,
    _pseudo_hyde_expand,
    _question_to_statement,
    analyze_query,
)

__all__ = [
    "Retriever",
    "_derive_implied_fact_passages",
    "_extract_query_entities",
    "_pseudo_hyde_expand",
    "_question_to_statement",
    "analyze_query",
]
