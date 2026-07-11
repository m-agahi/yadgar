"""Query analysis stage — runs analyze_query() and populates state metadata."""

from __future__ import annotations

from yadgar._shared.observability.observe import observe
from yadgar.backend.retrieval.stages.base import RetrievalStage
from yadgar.backend.retrieval.state import RetrievalState


class QueryAnalysisStage(RetrievalStage):
    """Pre-stage: analyze query and set up shared metadata on state.

    Populates:
    - state.query_analysis: full analyze_query() result dict
    - state.open_domain_mode: bool
    - state.query_analysis["_open_domain_subqueries"]: list of sub-queries
    - state.query_analysis["_profile_dict"]: the active profile config dict

    This stage always runs first regardless of profile because all other stages
    depend on the query_analysis context.
    """

    name = "query_analysis"

    def __init__(self, retriever) -> None:
        self._retriever = retriever

    @observe(tier="stage", metric="retrieval.pipeline.query_analysis")
    def apply(self, state: RetrievalState) -> RetrievalState:
        from yadgar._shared.retrieval.profiles import get_profile  # noqa: PLC0415
        from yadgar.backend.retrieval.query_analysis import (  # noqa: PLC0415
            _build_open_domain_subqueries,
            analyze_query,
        )

        settings = self._retriever._settings
        profile_dict = get_profile(state.profile)

        query_analysis = analyze_query(state.query, settings)
        open_domain_mode = query_analysis.get("is_open_domain_like", False)
        open_domain_subqueries = (
            _build_open_domain_subqueries(state.query, query_analysis) if open_domain_mode else []
        )

        # Stash helpers in query_analysis so downstream stages can access them
        query_analysis["_open_domain_subqueries"] = open_domain_subqueries
        query_analysis["_profile_dict"] = profile_dict

        state.query_analysis = query_analysis
        state.open_domain_mode = open_domain_mode
        return state
