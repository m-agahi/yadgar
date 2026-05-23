"""Cross-encoder reranking mixin — CE scoring, pair scoring, clustering."""

from __future__ import annotations

import logging
from collections import defaultdict

from yadgar.retrieval.query_analysis import (
    _derive_implied_fact_passages,
    analyze_query,
)
from yadgar.storage import _FTS_STOP_WORDS
from yadgar.tracing import trace_span

logger = logging.getLogger(__name__)


class _CrossEncoderMixin:
    """Provides cross_encoder_rerank, score_single_pair, and cluster_memories."""

    @trace_span("retrieval.cross_encoder_rerank")
    def cross_encoder_rerank(
        self,
        memories: list[dict],
        query: str,
        top_k: int | None = None,
    ) -> list[dict]:
        """Rerank memories using the ML client cross-encoder.

        Delegates model scoring to self._ml (LocalMLClient or RemoteMLClient).
        Keeps all bookkeeping (normalization, weighting, sorting) here.
        """
        if top_k is None:
            top_k = self._settings.CROSS_ENCODER_TOP_K

        # §13 skip: gate behind enabled flag — ML model is optional
        if not getattr(self._settings, "CROSS_ENCODER_ENABLED", True):
            return memories[:top_k] if memories else []

        if not memories or not query:
            return memories[:top_k] if memories else []

        query_analysis = analyze_query(query, self._settings)
        open_domain_mode = query_analysis.get("is_open_domain_like", False)

        # Build expanded text list: one entry per memory, plus implied-fact variants
        # in open_domain_mode (mirrors the original FlashRank variant expansion).
        expanded_texts: list[str] = []
        variant_to_memory: dict[int, int] = {}
        for i, mem in enumerate(memories):
            base_text = mem.get("content", "")
            variant_to_memory[len(expanded_texts)] = i
            expanded_texts.append(base_text)
            if open_domain_mode:
                implied_facts = _derive_implied_fact_passages(base_text)
                if implied_facts:
                    variant_to_memory[len(expanded_texts)] = i
                    expanded_texts.append(" ".join(implied_facts))

        try:
            all_scores = self._ml.score_cross_encoder(query, expanded_texts)
        except Exception as e:
            logger.warning("cross_encoder_rerank: ML client failed: %s", e)
            return memories[:top_k]

        # N4: circuit breaker open → skip rerank, return BM25+HNSW results as-is
        if all_scores is None:
            logger.warning(
                "cross_encoder_rerank: circuit breaker open — returning pre-rerank order"
            )
            return memories[:top_k]

        # Aggregate: take max score per memory across all its variants
        memory_raw_scores: dict[int, float] = defaultdict(float)
        for j, score in enumerate(all_scores):
            mem_idx = variant_to_memory.get(j)
            if mem_idx is not None:
                memory_raw_scores[mem_idx] = max(
                    memory_raw_scores.get(mem_idx, float("-inf")), score
                )

        raw_scores = [memory_raw_scores.get(i, 0.0) for i in range(len(memories))]

        if not raw_scores or all(s == 0.0 for s in raw_scores):
            return memories[:top_k]

        max_score = max(raw_scores)
        min_score = min(raw_scores)
        score_range = max_score - min_score

        ce_weight = getattr(self._settings, "CROSS_ENCODER_WEIGHT", 0.6)
        ret_weight = 1.0 - ce_weight
        for i, mem in enumerate(memories):
            ce_norm = (raw_scores[i] - min_score) / score_range if score_range > 0 else 0.5

            content = mem.get("content", "")
            content_len = len(content)
            if content_len < 80:
                ce_norm *= 0.5
            elif content_len < 150:
                ce_norm *= 0.8

            retrieval_score = mem.get("_retrieval_score", 0.0)
            mem["_cross_encoder_score"] = round(ce_norm, 4)
            mem["_retrieval_score"] = round(ret_weight * retrieval_score + ce_weight * ce_norm, 4)

        memories.sort(key=lambda m: m["_retrieval_score"], reverse=True)
        return memories[:top_k]

    @trace_span("retrieval.score_pair")
    def score_single_pair(self, query: str, document: str) -> float:
        """Score a single query-document pair using the ML client."""
        try:
            result = self._ml.score_pair(query, document)
            # N4: circuit breaker open returns None — treat as 0.0 (graceful degrade)
            return result if result is not None else 0.0
        except Exception:
            return 0.0

    def cluster_memories(self, memories: list[dict]) -> list[list[dict]]:
        """Cluster memories by entity/topic overlap using Jaccard similarity."""
        threshold = getattr(self._settings, "MULTI_PASSAGE_CLUSTER_OVERLAP_THRESHOLD", 0.3)
        max_size = getattr(self._settings, "MULTI_PASSAGE_MAX_CLUSTER_SIZE", 3)

        # Tokenize each memory
        tokenized = []
        for m in memories:
            tokens = set(m.get("content", "").lower().split())
            tokens -= _FTS_STOP_WORDS
            tokenized.append(tokens)

        clusters = []
        used = set()

        for i, m in enumerate(memories):
            if i in used:
                continue
            cluster = [m]
            used.add(i)
            for j in range(i + 1, len(memories)):
                if j in used or len(cluster) >= max_size:
                    break
                # Jaccard similarity
                intersection = len(tokenized[i] & tokenized[j])
                union = len(tokenized[i] | tokenized[j])
                if union > 0 and intersection / union >= threshold:
                    cluster.append(memories[j])
                    used.add(j)
            clusters.append(cluster)

        return clusters
