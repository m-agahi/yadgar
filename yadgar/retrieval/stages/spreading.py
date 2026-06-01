"""Spreading activation stage — BFS through entity graph from vector seeds."""

from __future__ import annotations

from yadgar.retrieval.stages.base import RetrievalStage
from yadgar.retrieval.state import RetrievalState


class SpreadingStage(RetrievalStage):
    """Collect spreading activation scores from top vector search results.

    Delegates to ``retriever._collect_spreading_scores()``.
    Signal key: ``"spread"`` in state.scores.
    Requires KNNStage to have run first (reads state.vector_memory_ids).
    """

    name = "spreading"

    def __init__(self, retriever) -> None:
        self._retriever = retriever

    def apply(self, state: RetrievalState) -> RetrievalState:
        settings = self._retriever._settings
        profile_dict = state.query_analysis.get("_profile_dict", {})
        profile_signals = set(profile_dict.get("signals", []))

        if settings.QUERY_ROUTING_ENABLED:
            enabled_signals = set(state.query_analysis.get("enabled_signals", [])) & profile_signals
        else:
            enabled_signals = None if len(profile_signals) >= 4 else profile_signals

        self._retriever._collect_spreading_scores(
            state.scores,
            enabled_signals,
            state.vector_memory_ids,
        )
        return state
