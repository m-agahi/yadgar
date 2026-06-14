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


def _build_expanded_pairs(
    memories_to_score: list[dict],
    open_domain_mode: bool,
) -> tuple[list[str], dict[int, int]]:
    """Build the flat text list and variant→memory index for CE scoring.

    Returns (expanded_texts, variant_to_memory) where variant_to_memory[j]
    is the index into memories_to_score for the j-th text in expanded_texts.
    In open_domain_mode, each memory may contribute a second implied-facts
    variant alongside the base text.
    """
    expanded_texts: list[str] = []
    variant_to_memory: dict[int, int] = {}
    for i, mem in enumerate(memories_to_score):
        base_text = mem.get("content", "")
        variant_to_memory[len(expanded_texts)] = i
        expanded_texts.append(base_text)
        if open_domain_mode:
            implied_facts = _derive_implied_fact_passages(base_text)
            if implied_facts:
                variant_to_memory[len(expanded_texts)] = i
                expanded_texts.append(" ".join(implied_facts))
    return expanded_texts, variant_to_memory


def _aggregate_max_scores(
    all_scores: list[float],
    variant_to_memory: dict[int, int],
    n_memories: int,
) -> list[float]:
    """Aggregate per-variant scores into one max score per memory.

    Returns a list of length n_memories where each entry is the maximum
    score seen across all variants for that memory index.
    """
    memory_raw_scores: dict[int, float] = defaultdict(float)
    for j, score in enumerate(all_scores):
        mem_idx = variant_to_memory.get(j)
        if mem_idx is not None:
            memory_raw_scores[mem_idx] = max(memory_raw_scores.get(mem_idx, float("-inf")), score)
    return [memory_raw_scores.get(i, 0.0) for i in range(n_memories)]


def _apply_ce_weights(
    memories_to_score: list[dict],
    raw_scores: list[float],
    ce_weight: float,
) -> None:
    """Normalise raw CE scores and blend with existing retrieval scores in-place.

    Applies min-max normalisation, a content-length penalty for short texts,
    and blends the result with the existing _retrieval_score using ce_weight.
    Mutates each memory dict in memories_to_score directly.
    """
    max_score = max(raw_scores)
    min_score = min(raw_scores)
    score_range = max_score - min_score
    ret_weight = 1.0 - ce_weight
    for i, mem in enumerate(memories_to_score):
        ce_norm = (raw_scores[i] - min_score) / score_range if score_range > 0 else 0.5
        content_len = len(mem.get("content", ""))
        if content_len < 80:
            ce_norm *= 0.5
        elif content_len < 150:
            ce_norm *= 0.8
        retrieval_score = mem.get("_retrieval_score", 0.0)
        mem["_cross_encoder_score"] = round(ce_norm, 4)
        mem["_retrieval_score"] = round(ret_weight * retrieval_score + ce_weight * ce_norm, 4)


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

        # v5.6.6 B: cap input candidates BEFORE expansion to bound CE inference time.
        # On CPU, each pair is ~400ms; 50 memories × 2 variants = 46s burst.
        # Slicing to top_k here limits to top_k×2 pairs max (base + one variant each).
        memories_to_score = memories[:top_k]

        expanded_texts, variant_to_memory = _build_expanded_pairs(
            memories_to_score, open_domain_mode
        )

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

        raw_scores = _aggregate_max_scores(all_scores, variant_to_memory, len(memories_to_score))

        if not raw_scores or all(s == 0.0 for s in raw_scores):
            return memories_to_score[:top_k]

        ce_weight = getattr(self._settings, "CROSS_ENCODER_WEIGHT", 0.6)
        _apply_ce_weights(memories_to_score, raw_scores, ce_weight)

        memories_to_score.sort(key=lambda m: m["_retrieval_score"], reverse=True)
        return memories_to_score[:top_k]

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
