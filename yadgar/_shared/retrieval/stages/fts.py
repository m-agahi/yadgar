"""FTS stage — full-text search signal collection."""

from __future__ import annotations

from yadgar._shared.observability.observe import observe
from yadgar._shared.retrieval.scoring import FTSParams
from yadgar._shared.retrieval.stages.base import RetrievalStage
from yadgar._shared.retrieval.state import RetrievalState


class FTSStage(RetrievalStage):
    """Collect BM25 / FTS5 scores (including entity-FTS and COMET expansion).

    Delegates to ``retriever._collect_fts_scores()``.
    Signal key: ``"fts"`` in state.scores.
    """

    name = "fts"

    def __init__(self, retriever) -> None:
        self._retriever = retriever

    @observe(tier="stage", name="retrieval.pipeline.fts")
    def apply(self, state: RetrievalState) -> RetrievalState:
        from collections import defaultdict  # noqa: PLC0415

        from yadgar._shared.storage import BranchFilter  # noqa: PLC0415

        branch_filter = None
        if state.default_branch is not None:
            branch_filter = BranchFilter(
                current_branch=state.current_branch,
                default_branch=state.default_branch,
            )

        # Ensure scores defaultdict is set up
        if not state.scores:
            state.scores = defaultdict(
                lambda: {
                    "vector": 0.0,
                    "fts": 0.0,
                    "ppr": 0.0,
                    "spread": 0.0,
                    "temporal": 0.0,
                }
            )

        settings = self._retriever._settings
        profile_dict = state.query_analysis.get("_profile_dict", {})
        profile_signals = set(profile_dict.get("signals", []))

        # enabled_signals mirrors logic from core.recall()
        if settings.QUERY_ROUTING_ENABLED:
            enabled_signals = set(state.query_analysis.get("enabled_signals", [])) & profile_signals
        else:
            enabled_signals = None if len(profile_signals) >= 4 else profile_signals

        candidate_k = state.max_results * settings.CANDIDATE_POOL_MULTIPLIER
        if state.open_domain_mode:
            candidate_k = int(
                candidate_k * getattr(settings, "OPEN_DOMAIN_CANDIDATE_MULTIPLIER", 1.5)
            )

        open_domain_subqueries = state.query_analysis.get("_open_domain_subqueries", [])

        self._retriever._collect_fts_scores(
            state.scores,
            FTSParams(
                query=state.query,
                enabled_signals=enabled_signals,
                open_domain_subqueries=open_domain_subqueries,
                open_domain_mode=state.open_domain_mode,
                candidate_k=candidate_k,
                min_heat=state.min_heat,
                branch_filter=branch_filter,
            ),
        )
        return state
