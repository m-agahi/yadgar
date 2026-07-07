"""Temporal stage — date/time expression detection and score collection."""

from __future__ import annotations

from yadgar._shared.observability.observe import observe
from yadgar._shared.retrieval.stages.base import RetrievalStage
from yadgar._shared.retrieval.state import RetrievalState


class TemporalStage(RetrievalStage):
    """Detect temporal expressions in the query and collect temporal scores.

    Delegates to ``retriever._collect_temporal_scores()``.
    Signal key: ``"temporal"`` in state.scores.
    Also sets state.w_temporal (0.0 if no temporal signal found).
    """

    name = "temporal"

    def __init__(self, retriever) -> None:
        self._retriever = retriever

    @observe(tier="stage", name="retrieval.pipeline.temporal")
    def apply(self, state: RetrievalState) -> RetrievalState:
        from yadgar._shared.storage import BranchFilter  # noqa: PLC0415

        branch_filter = None
        if state.default_branch is not None:
            branch_filter = BranchFilter(
                current_branch=state.current_branch,
                default_branch=state.default_branch,
            )

        settings = self._retriever._settings
        candidate_k = state.max_results * settings.CANDIDATE_POOL_MULTIPLIER
        if state.open_domain_mode:
            candidate_k = int(
                candidate_k * getattr(settings, "OPEN_DOMAIN_CANDIDATE_MULTIPLIER", 1.5)
            )

        state.w_temporal = self._retriever._collect_temporal_scores(
            state.query,
            state.scores,
            state.min_heat,
            candidate_k,
            branch_filter=branch_filter,
        )
        return state
