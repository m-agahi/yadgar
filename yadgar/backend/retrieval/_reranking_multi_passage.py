"""Multi-passage evidence aggregation reranking mixin."""

from __future__ import annotations

from yadgar._shared.observability.observe import observe


class _MultiPassageMixin:
    """Provides multi_passage_rerank for evidence aggregation across related memories.

    Requires _CrossEncoderMixin earlier in MRO (uses self.score_documents and
    self.cluster_memories).
    """

    @observe(tier="stage", metric="retrieval.multi_passage_rerank")
    def multi_passage_rerank(self, query: str, memories: list[dict], top_k: int) -> list[dict]:
        """Multi-passage evidence aggregation reranking.

        Groups related memories and re-scores clusters to detect when multiple
        weak pieces of evidence combine into strong evidence.

        v5.98 Lever 1: all qualifying clusters' combined texts are scored in ONE
        batched, LRU-cached CE call (self.score_documents → backend mode=ce)
        instead of per-cluster mode=pair RPCs (uncached). Score-identical; the
        only change is where/whether the identical scores are cached.
        """
        if not self._settings.MULTI_PASSAGE_RERANKING_ENABLED:
            return memories[:top_k]

        # Cluster top-20 candidates
        clusters = self.cluster_memories(memories[:20])

        # Build the combined text for every qualifying cluster (≥2 members) up-front,
        # preserving cluster order so scores map back by index.
        qualifying: list[list[dict]] = []
        combined_texts: list[str] = []
        for cluster_mems in clusters:
            if len(cluster_mems) < 2:
                continue
            combined_texts.append(" | ".join(m.get("content", "")[:200] for m in cluster_mems[:3]))
            qualifying.append(cluster_mems)

        # Single batched CE call for all cluster combined-texts (cached on repeat).
        combined_scores = self.score_documents(query, combined_texts)

        for cluster_mems, combined_score in zip(qualifying, combined_scores, strict=True):
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
