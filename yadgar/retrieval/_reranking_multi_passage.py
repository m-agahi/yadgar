"""Multi-passage evidence aggregation reranking mixin."""

from __future__ import annotations


class _MultiPassageMixin:
    """Provides multi_passage_rerank for evidence aggregation across related memories.

    Requires _CrossEncoderMixin earlier in MRO (uses self.score_single_pair and
    self.cluster_memories).
    """

    def multi_passage_rerank(self, query: str, memories: list[dict], top_k: int) -> list[dict]:
        """Multi-passage evidence aggregation reranking.

        Groups related memories and re-scores clusters to detect when multiple
        weak pieces of evidence combine into strong evidence.
        """
        if not getattr(self._settings, "MULTI_PASSAGE_RERANKING_ENABLED", False):
            return memories[:top_k]

        # Cluster top-20 candidates
        clusters = self.cluster_memories(memories[:20])

        for cluster_mems in clusters:
            if len(cluster_mems) < 2:
                continue
            # Concatenate cluster texts
            combined = " | ".join(m.get("content", "")[:200] for m in cluster_mems[:3])

            # Score combined text using CE
            combined_score = self.score_single_pair(query, combined)

            # If combined evidence is stronger, boost individual members
            max_individual = max(
                m.get("_cross_encoder_score", m.get("_retrieval_score", 0)) for m in cluster_mems
            )
            if combined_score > max_individual:
                boost = (combined_score - max_individual) * 0.5
                for m in cluster_mems:
                    m["_retrieval_score"] = m.get("_retrieval_score", 0) + boost

        memories.sort(key=lambda m: m.get("_retrieval_score", 0), reverse=True)
        return memories[:top_k]
