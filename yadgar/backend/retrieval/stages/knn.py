"""KNN stage — vector similarity search signal collection."""

from __future__ import annotations

from yadgar._shared.observability.observe import observe
from yadgar.backend.retrieval.stages.base import RetrievalStage
from yadgar.backend.retrieval.state import RetrievalState


class KNNStage(RetrievalStage):
    """Collect vector KNN scores and set query_embedding on state.

    Delegates to ``retriever._collect_vector_scores()``.
    Signal key: ``"vector"`` in state.scores.
    Also populates state.vector_memory_ids and state.query_embedding.
    """

    name = "knn"

    def __init__(self, retriever) -> None:
        self._retriever = retriever

    @observe(tier="stage", metric="retrieval.pipeline.knn")
    def apply(self, state: RetrievalState) -> RetrievalState:
        from yadgar._shared.storage import BranchFilter  # noqa: PLC0415

        branch_filter = None
        if state.default_branch is not None:
            branch_filter = BranchFilter(
                current_branch=state.current_branch,
                default_branch=state.default_branch,
            )

        settings = self._retriever._settings
        profile_dict = state.query_analysis.get("_profile_dict", {})
        profile_signals = set(profile_dict.get("signals", []))

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

        vector_memory_ids, query_embedding = self._retriever._collect_vector_scores(
            state.query,
            state.scores,
            enabled_signals,
            open_domain_subqueries,
            candidate_k,
            state.min_heat,
            branch_filter=branch_filter,
        )
        state.vector_memory_ids = vector_memory_ids
        state.query_embedding = query_embedding
        return state
