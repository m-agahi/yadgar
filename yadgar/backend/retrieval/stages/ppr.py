"""PPR stage — Personalized PageRank graph retrieval."""

from __future__ import annotations

from yadgar._shared.observability.observe import observe
from yadgar.backend.retrieval.stages.base import RetrievalStage
from yadgar.backend.retrieval.state import RetrievalState


class PPRStage(RetrievalStage):
    """Collect PPR scores from the entity knowledge graph.

    Delegates to ``retriever._collect_ppr_scores()``.
    Signal key: ``"ppr"`` in state.scores.
    """

    name = "ppr"

    def __init__(self, retriever) -> None:
        self._retriever = retriever

    @observe(tier="stage", metric="retrieval.pipeline.ppr")
    def apply(self, state: RetrievalState) -> RetrievalState:
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

        self._retriever._collect_ppr_scores(
            state.query,
            state.scores,
            enabled_signals,
            candidate_k,
        )
        return state
