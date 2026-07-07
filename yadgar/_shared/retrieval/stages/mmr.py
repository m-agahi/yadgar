"""MMR diversity stage — no-op in v5.31.0; handled by composite rerank pipeline."""

from __future__ import annotations

from yadgar._shared.observability.observe import observe
from yadgar._shared.retrieval.stages.base import RetrievalStage
from yadgar._shared.retrieval.state import RetrievalState


class MMRStage(RetrievalStage):
    """Maximal Marginal Relevance diversity reranking.

    In v5.31.0, MMR runs inside ``_apply_rerank_pipeline`` (via CEReRankStage).
    This class exists for profile configuration and A/B test targeting.
    Its ``apply`` is a no-op; the pipeline runner routes all post-fusion stage
    names to CEReRankStage.apply() once.
    """

    name = "mmr"

    def __init__(self, retriever) -> None:
        self._retriever = retriever

    @observe(tier="stage", name="retrieval.pipeline.mmr")
    def apply(self, state: RetrievalState) -> RetrievalState:
        # No-op: MMR is executed inside CEReRankStage via _apply_rerank_pipeline.
        return state
