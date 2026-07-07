"""Fusion stage — WRRF/convex signal fusion and initial result building."""

from __future__ import annotations

from yadgar._shared.observability.observe import observe
from yadgar._shared.retrieval.stages.base import RetrievalStage
from yadgar._shared.retrieval.state import RetrievalState


class FusionStage(RetrievalStage):
    """Fuse per-signal scores via WRRF or convex combination.

    Delegates to:
    - ``retriever._fuse_scores()``
    - ``retriever._build_initial_results()``

    Reads: state.scores, state.w_temporal, state.open_domain_mode,
           state.query_analysis (profile_dict, cross_encoder key).
    Writes: state.fused, state.fused_scores, state.result_memories,
            state.seen_ids, state.use_cross_encoder.
    """

    name = "fusion"

    def __init__(self, retriever) -> None:
        self._retriever = retriever

    @observe(tier="stage", name="retrieval.pipeline.fusion")
    def apply(self, state: RetrievalState) -> RetrievalState:
        profile_dict = state.query_analysis.get("_profile_dict", {})

        fused, fused_scores = self._retriever._fuse_scores(
            state.scores,
            state.w_temporal,
            state.open_domain_mode,
        )
        state.fused = fused
        state.fused_scores = fused_scores

        result_memories, seen_ids, use_cross_encoder = self._retriever._build_initial_results(
            fused,
            fused_scores,
            state.scores,
            profile_dict,
            state.open_domain_mode,
            state.max_results,
            state.min_heat,
        )
        state.result_memories = result_memories
        state.seen_ids = seen_ids
        state.use_cross_encoder = use_cross_encoder
        return state
