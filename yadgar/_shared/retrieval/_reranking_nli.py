"""NLI reranking mixin — entailment-based scoring."""

from __future__ import annotations

import logging

from yadgar._shared.observability.observe import observe
from yadgar._shared.retrieval.query_analysis import _question_to_statement

logger = logging.getLogger(__name__)


class _NLIMixin:
    """Provides nli_rerank using NLI entailment probability."""

    @observe(tier="stage", metric="retrieval.nli_rerank")
    def nli_rerank(self, query: str, memories: list[dict]) -> list[dict]:
        """Score memories by NLI entailment probability."""
        if not getattr(self._settings, "NLI_RERANKING_ENABLED", False):
            return memories

        hypothesis = _question_to_statement(query)
        texts = [m.get("content", "")[:512] for m in memories]

        try:
            raw_scores = self._ml.score_nli(hypothesis, texts)
        except Exception as e:
            logger.warning("NLI reranking failed: %s", e)
            for mem in memories:
                mem["_nli_entailment_score"] = 0.0
            return memories

        # N4: circuit breaker open → skip NLI scoring
        if raw_scores is None:
            logger.warning("NLI reranking: circuit breaker open — skipping NLI stage")
            for mem in memories:
                mem["_nli_entailment_score"] = 0.0
            return memories

        for i, mem in enumerate(memories):
            mem["_nli_entailment_score"] = float(raw_scores[i])

        return memories
