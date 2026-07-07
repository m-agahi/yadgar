"""NLI diversity stage — no-op in v5.31.0; handled by composite rerank pipeline."""

from __future__ import annotations

from yadgar._shared.observability.observe import observe
from yadgar._shared.retrieval.stages.base import RetrievalStage
from yadgar._shared.retrieval.state import RetrievalState


class NLIStage(RetrievalStage):
    """NLI entailment diversity filter.

    In v5.31.0, NLI runs inside ``_apply_rerank_pipeline`` (via CEReRankStage).
    This class exists for profile configuration, A/B test targeting, and
    per-stage metrics.  Its ``apply`` is a no-op because the pipeline runner
    detects the composite-stage pattern and routes all post-fusion stage names
    (nli, mmr, adversarial, rules) to CEReRankStage.apply() once.
    """

    name = "nli"

    def __init__(self, retriever) -> None:
        self._retriever = retriever

    @observe(tier="stage", name="retrieval.pipeline.nli")
    def apply(self, state: RetrievalState) -> RetrievalState:
        # No-op: NLI is executed inside CEReRankStage via _apply_rerank_pipeline.
        return state

    def is_enabled(self, profile: str, config: dict) -> bool:
        """NLI is profile-gated via the "nli" key in the profile dict."""
        return True
