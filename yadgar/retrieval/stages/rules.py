"""Rules engine stage — no-op in v5.31.0; handled by composite rerank pipeline."""

from __future__ import annotations

from yadgar.retrieval.stages.base import RetrievalStage
from yadgar.retrieval.state import RetrievalState


class RulesStage(RetrievalStage):
    """Neuro-symbolic rules engine filter.

    In v5.31.0, rules application runs inside ``_apply_rerank_pipeline``
    (via CEReRankStage).  This class exists for profile configuration and
    A/B test targeting.  Its ``apply`` is a no-op.
    """

    name = "rules"

    def __init__(self, retriever) -> None:
        self._retriever = retriever

    def apply(self, state: RetrievalState) -> RetrievalState:
        # No-op: executed inside CEReRankStage via _apply_rerank_pipeline.
        return state
