"""WRRF fusion functions, retrieval profile definitions, and _FusionMixin."""

from __future__ import annotations

import logging
from collections import defaultdict

logger = logging.getLogger(__name__)

# Retrieval profiles: fast < balanced < full
# fast:     vector + FTS only, no reranking — lowest latency
# balanced: all 4 signals + cross-encoder — default
# full:     all 4 signals + cross-encoder + NLI — maximum quality
PROFILES: dict[str, dict] = {
    "fast": {
        "signals": ["vector", "fts"],
        "cross_encoder": False,
        "nli": False,
        "multi_passage": False,
    },
    "balanced": {
        "signals": ["vector", "fts", "ppr", "spreading"],
        "cross_encoder": True,
        "nli": False,
        "multi_passage": True,
    },
    "full": {
        "signals": ["vector", "fts", "ppr", "spreading"],
        "cross_encoder": True,
        "nli": True,
        "multi_passage": True,
    },
}


def _wrrf_fuse(
    ranked_lists: dict[str, list[int]],
    wrrf_weights: dict[str, float],
    k: int = 60,
) -> list[tuple[int, float]]:
    """Weighted Reciprocal Rank Fusion across multiple ranked lists.

    Args:
        ranked_lists: signal_name -> list of memory IDs (sorted by signal score desc)
        wrrf_weights: signal_name -> weight
        k: RRF constant (default 60)

    Returns:
        List of (memory_id, wrrf_score) sorted by score descending.
    """
    scores: dict[int, float] = {}
    for signal_name, mem_ids in ranked_lists.items():
        w = wrrf_weights.get(signal_name, 0.0)
        if w <= 0:
            continue
        for rank, mem_id in enumerate(mem_ids):
            scores[mem_id] = scores.get(mem_id, 0.0) + w / (k + rank + 1)

    return sorted(scores.items(), key=lambda x: x[1], reverse=True)


def _convex_fuse(
    signal_scores: dict[str, dict[int, float]],
    weights: dict[str, float],
) -> list[tuple[int, float]]:
    """Convex combination of normalized signal scores.

    Unlike RRF which uses ranks, this uses actual scores with min-max normalization.
    Proven to outperform RRF (Bruch et al., ACM TOIS 2023).
    """
    total_w = sum(weights.values())
    if total_w == 0:
        return []
    norm_weights = {k: v / total_w for k, v in weights.items()}

    normalized: dict[str, dict[int, float]] = {}
    for signal, scores in signal_scores.items():
        if not scores:
            continue
        vals = list(scores.values())
        min_s, max_s = min(vals), max(vals)
        range_s = max_s - min_s
        if range_s > 0:
            normalized[signal] = {mid: (s - min_s) / range_s for mid, s in scores.items()}
        else:
            normalized[signal] = {mid: 0.5 for mid in scores}

    all_mids: set[int] = set()
    for scores in normalized.values():
        all_mids.update(scores.keys())

    combined: dict[int, float] = {}
    for mid in all_mids:
        combined[mid] = sum(
            norm_weights.get(sig, 0) * normalized.get(sig, {}).get(mid, 0) for sig in normalized
        )
    return sorted(combined.items(), key=lambda x: x[1], reverse=True)


class _FusionMixin:
    """Score-fusion and result-building helpers for Retriever.

    Methods here read ``self._storage``, ``self._settings``, ``self._embeddings``,
    and ``self._reranker`` — all available on the Retriever instance via MRO.
    """

    def _fuse_scores(
        self,
        scores: dict,
        w_temporal: float,
        open_domain_mode: bool,
    ) -> tuple[list, dict]:
        """Build signal weights, apply confidence gating, and fuse scores.

        Returns (fused_sorted_list, fused_scores_dict).
        """
        signal_weights = {
            "vector": self._settings.WRRF_VECTOR_WEIGHT,
            "fts": self._settings.WRRF_FTS_WEIGHT,
            "ppr": self._settings.WRRF_PPR_WEIGHT,
            "spread": self._settings.WRRF_SPREADING_WEIGHT,
        }
        if w_temporal > 0:
            signal_weights["temporal"] = w_temporal
        if open_domain_mode:
            signal_weights["fts"] *= getattr(self._settings, "OPEN_DOMAIN_FTS_BOOST", 1.6)

        # Apply confidence gating
        if getattr(self._settings, "CONFIDENCE_GATING_ENABLED", False):
            _conf_name_map = {"spread": "spreading"}
            thresholds = {
                "vector": getattr(self._settings, "CONFIDENCE_THRESHOLD_VECTOR", 0.1),
                "fts": getattr(self._settings, "CONFIDENCE_THRESHOLD_FTS", 0.1),
                "ppr": getattr(self._settings, "CONFIDENCE_THRESHOLD_PPR", 0.1),
                "spread": getattr(self._settings, "CONFIDENCE_THRESHOLD_SPREADING", 0.1),
                "temporal": getattr(self._settings, "CONFIDENCE_THRESHOLD_TEMPORAL", 0.1),
            }
            for sig in list(signal_weights.keys()):
                ranked = sorted(
                    [(mid, s[sig]) for mid, s in scores.items() if s[sig] > 0],
                    key=lambda x: x[1],
                    reverse=True,
                )
                conf_name = _conf_name_map.get(sig, sig)
                confidence = self._reranker.compute_signal_confidence(conf_name, ranked)
                threshold = thresholds.get(sig, 0.1)
                if confidence < threshold:
                    signal_weights[sig] = 0.0

        fusion_method = getattr(self._settings, "FUSION_METHOD", "wrrf")

        if fusion_method == "convex":
            signal_scores_for_convex: dict[str, dict[int, float]] = {}
            for sig in signal_weights:
                sig_dict = {mid: s[sig] for mid, s in scores.items() if s[sig] > 0}
                if sig_dict:
                    signal_scores_for_convex[sig] = sig_dict
            fused = _convex_fuse(signal_scores_for_convex, signal_weights)
            fused_scores = dict(fused)
        else:
            # WRRF-style normalized weighted sum
            signal_names = list(
                {sig for mid, sigs in scores.items() for sig, v in sigs.items() if v > 0}
            )
            normalized: dict[int, dict[str, float]] = defaultdict(lambda: defaultdict(float))
            fusion_norm = getattr(self._settings, "FUSION_NORM", "zscore")

            for sig in signal_names:
                sig_vals = [(mid, s[sig]) for mid, s in scores.items() if s[sig] > 0]
                if not sig_vals:
                    continue
                vals = [v for _, v in sig_vals]

                if fusion_norm == "minmax":
                    min_v = min(vals)
                    max_v = max(vals)
                    rng = max_v - min_v
                    for mid, v in sig_vals:
                        normalized[mid][sig] = (v - min_v) / rng if rng > 1e-9 else 0.5
                elif fusion_norm == "raw":
                    for mid, v in sig_vals:
                        normalized[mid][sig] = v
                else:  # zscore (default)
                    mean_v = sum(vals) / len(vals)
                    std_v = (sum((v - mean_v) ** 2 for v in vals) / len(vals)) ** 0.5
                    if std_v > 1e-9:
                        z_scores = [(mid, (v - mean_v) / std_v) for mid, v in sig_vals]
                        z_vals = [z for _, z in z_scores]
                        z_min, z_max = min(z_vals), max(z_vals)
                        z_rng = z_max - z_min
                        for mid, z in z_scores:
                            normalized[mid][sig] = (z - z_min) / z_rng if z_rng > 1e-9 else 0.5
                    else:
                        for mid, _v in sig_vals:
                            normalized[mid][sig] = 0.5

            combmnz = getattr(self._settings, "COMBMNZ_ENABLED", False)

            fused_scores = {}
            for mid, norm_sigs in normalized.items():
                total = 0.0
                signal_count = 0
                for signal, norm_score in norm_sigs.items():
                    w = signal_weights.get(signal, 0.0)
                    if w > 0:
                        total += w * norm_score
                        signal_count += 1
                if total > 0:
                    if combmnz and signal_count > 1:
                        total *= signal_count
                    fused_scores[mid] = total

            fused = sorted(fused_scores.items(), key=lambda x: x[1], reverse=True)

        return fused, fused_scores

    def _build_initial_results(
        self,
        fused: list,
        fused_scores: dict,
        scores: dict,
        profile: dict,
        open_domain_mode: bool,
        max_results: int,
        min_heat: float,
    ) -> tuple[list[dict], set[int], bool]:
        """Build result_memories from fused scores and inject CE diversity candidates.

        Returns (result_memories, seen_ids, use_cross_encoder).
        """
        rerank_pool = max(
            max_results,
            self._settings.RERANKER_TOP_K,
            getattr(self._settings, "CROSS_ENCODER_TOP_K", 0),
        )
        result_memories: list[dict] = []
        seen_ids: set[int] = set()
        for mid, total_score in fused:
            mem = self._storage.get_memory(mid)
            if mem and mem.get("id") is not None and mem["heat"] >= min_heat:
                mem["_retrieval_score"] = round(total_score, 4)
                mem.pop("embedding", None)
                result_memories.append(mem)
                seen_ids.add(mid)
            if len(result_memories) >= rerank_pool:
                break

        use_cross_encoder = profile["cross_encoder"] and getattr(
            self._settings, "CROSS_ENCODER_ENABLED", False
        )
        if use_cross_encoder:
            diversity_k = getattr(self._settings, "CE_DIVERSITY_INJECT_K", 10)
            if open_domain_mode:
                diversity_k = max(diversity_k, 15)
            for sig in ["fts", "vector"]:
                top_sig = sorted(
                    [(mid, s[sig]) for mid, s in scores.items() if s[sig] > 0],
                    key=lambda x: x[1],
                    reverse=True,
                )[:diversity_k]
                for mid, _ in top_sig:
                    if mid not in seen_ids:
                        mem = self._storage.get_memory(mid)
                        if mem and mem.get("id") is not None and mem["heat"] >= min_heat:
                            mem["_retrieval_score"] = round(fused_scores.get(mid, 0.0), 4)
                            mem.pop("embedding", None)
                            result_memories.append(mem)
                            seen_ids.add(mid)

        return result_memories, seen_ids, use_cross_encoder

    def _comparison_dual_search(
        self,
        query: str,
        options: list[str],
        subject: str | None,
        max_results: int,
    ) -> list[dict]:
        """Dual-search for comparison queries like 'A or B?'"""
        all_results: list[dict] = []

        for option in options[:2]:  # Max 2 options
            sub_query = f"{subject} {option}" if subject else option
            # Vector search
            encoded = self._embeddings.encode_query(sub_query)
            vec_results: list[dict] = []
            if encoded is not None:
                vec_hits = self._storage.search_vectors(
                    encoded,
                    top_k=self._settings.COMPARISON_TOP_K_PER_OPTION,
                    min_heat=0.1,
                )
                for mid, _distance in vec_hits:
                    mem = self._storage.get_memory(mid)
                    if mem:
                        mem.pop("embedding", None)
                        vec_results.append(mem)
            # Also do FTS
            fts_results = self._storage.search_memories_fts(
                sub_query,
                limit=self._settings.COMPARISON_TOP_K_PER_OPTION,
            )
            # Merge
            seen: set[int] = set()
            merged: list[dict] = []
            for r in vec_results + fts_results:
                mid = r.get("id", r.get("memory_id", -1))
                if mid not in seen:
                    seen.add(mid)
                    r["_comparison_option"] = option
                    merged.append(r)
            all_results.extend(merged)

        # Deduplicate
        seen_final: set[int] = set()
        unique: list[dict] = []
        for r in all_results:
            mid = r.get("id", r.get("memory_id", -1))
            if mid not in seen_final:
                seen_final.add(mid)
                unique.append(r)

        return unique[: max_results * 2]

    def _search_profiles_and_beliefs(
        self,
        query: str,
        directory: str | None,
        max_results: int,
    ) -> list[dict]:
        """Search structured profiles and derived beliefs."""
        extra_results: list[dict] = []

        # Search profiles
        if getattr(self._settings, "PROFILE_EXTRACTION_ENABLED", False):
            try:
                profiles = self._storage.search_profiles_fts(query, limit=max_results)
                for p in profiles:
                    extra_results.append(
                        {
                            "id": -p.get("id", 0),  # Negative to distinguish from memories
                            "content": f"{p['entity_name']}: {p['attribute_type']} = {p['attribute_value']}",
                            "_source": "profile",
                            "_retrieval_score": self._settings.PROFILE_SEARCH_WEIGHT,
                        }
                    )
            except Exception:
                pass

        # Search beliefs
        if getattr(self._settings, "DERIVED_BELIEFS_ENABLED", False):
            try:
                beliefs = self._storage.search_beliefs_fts(query, limit=max_results)
                boost = self._settings.BELIEF_HIGH_CONFIDENCE_BOOST
                for b in beliefs:
                    score = (
                        b.get("confidence", 0.5) * boost
                        if b.get("confidence", 0) > 0.7
                        else b.get("confidence", 0.5)
                    )
                    extra_results.append(
                        {
                            "id": -b.get("id", 0) - 100000,  # Negative offset to distinguish
                            "content": b["content"],
                            "_source": "belief",
                            "_retrieval_score": score,
                        }
                    )
            except Exception:
                pass

        return extra_results
