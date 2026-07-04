"""Cross-encoder rerank stage — wraps existing _apply_rerank_pipeline.

In v5.31.0 the post-fusion pipeline (CE + NLI + MMR + adversarial + rules)
runs as a single composite stage via the existing ``_apply_rerank_pipeline``
method. Individual stages (ce_rerank, nli, mmr, adversarial, rules) are
represented by separate classes for profile configuration and A/B testing
purposes, but they share the same delegating apply() implementation.

This design keeps the characterization-test fixture green while exposing
the plugin interface required for v5.31.x stage-by-stage decomposition.
"""

from __future__ import annotations

from yadgar.observability.observe import observe
from yadgar.retrieval.reranking import RerankContext
from yadgar.retrieval.stages.base import RetrievalStage
from yadgar.retrieval.state import RetrievalState

_COMPOSITE_STAGE_NAME = "_rerank_pipeline"


class _ReRankPipelineStage(RetrievalStage):
    """Composite stage: runs the full post-fusion reranking pipeline.

    This is the single delegate for all post-fusion stages in v5.31.0.
    The ``name`` is deliberately private (prefixed ``_``) to indicate it
    is not a user-selectable profile stage.  Profile stage names
    (ce_rerank, nli, mmr, adversarial, rules) map to this stage via the
    pipeline's ``_composite_delegate`` registry.
    """

    name = _COMPOSITE_STAGE_NAME

    def __init__(self, retriever) -> None:
        self._retriever = retriever

    @observe(tier="stage", name="retrieval.pipeline.ce_rerank")
    def apply(self, state: RetrievalState) -> RetrievalState:
        profile_name = state.profile
        profile_dict = state.query_analysis.get("_profile_dict", {})

        ctx = RerankContext(
            query=state.query,
            query_analysis=state.query_analysis,
            query_embedding=state.query_embedding,
            profile=profile_dict,
            profile_name=profile_name,
            open_domain_mode=state.open_domain_mode,
            use_cross_encoder=state.use_cross_encoder,
            max_results=state.max_results,
        )
        state.result_memories = self._retriever._apply_rerank_pipeline(
            state.result_memories,
            state.seen_ids,
            ctx,
        )
        return state


class CEReRankStage(RetrievalStage):
    """Cross-encoder rerank — delegates to composite rerank pipeline stage."""

    name = "ce_rerank"

    def __init__(self, retriever) -> None:
        self._retriever = retriever
        self._delegate = _ReRankPipelineStage(retriever)

    @observe(tier="stage", name="retrieval.pipeline.ce_rerank")
    def apply(self, state: RetrievalState) -> RetrievalState:
        return self._delegate.apply(state)
