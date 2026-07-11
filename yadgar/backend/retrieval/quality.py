"""_QualityMixin: backward-compat delegators for Retriever extracted from core.py."""


class _QualityMixin:
    """Backward-compat delegator methods for Retriever.

    These were previously concrete methods on Retriever that delegated to
    ``self._reranker``. They are preserved here so any external caller that
    was using them via an instantiated Retriever continues to work.
    """

    def _compute_signal_confidence(
        self,
        signal_name: str,
        ranked_list: list[tuple[int, float]],
    ) -> float:
        """Delegate to Reranker.compute_signal_confidence (kept for backward compatibility)."""
        return self._reranker.compute_signal_confidence(signal_name, ranked_list)

    def _detect_adversarial(self, result_memories: list[dict]) -> dict:
        """Delegate to Reranker.detect_adversarial (kept for backward compatibility)."""
        return self._reranker.detect_adversarial(result_memories)

    def _cluster_memories(self, memories: list[dict]) -> list[list[dict]]:
        """Delegate to Reranker.cluster_memories (kept for backward compatibility)."""
        return self._reranker.cluster_memories(memories)
