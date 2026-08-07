"""Temporal stage — date/time expression detection and score collection."""

from __future__ import annotations

from yadgar._shared.observability.observe import observe
from yadgar.backend.retrieval.stages.base import RetrievalStage
from yadgar.backend.retrieval.state import RetrievalState


class TemporalStage(RetrievalStage):
    """Detect temporal expressions in the query and collect temporal scores.

    Delegates to ``retriever._collect_temporal_scores()``.
    Signal key: ``"temporal"`` in state.scores.
    Also sets state.w_temporal (0.0 if no temporal signal found).
    """

    name = "temporal"

    def __init__(self, retriever) -> None:
        self._retriever = retriever

    @observe(tier="stage", metric="retrieval.pipeline.temporal")
    def apply(self, state: RetrievalState) -> RetrievalState:
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
        )
        return state
