"""WRRF fusion functions, retrieval profile definitions, and _FusionMixin."""

from __future__ import annotations

import logging
from collections import defaultdict

from yadgar._shared.observability.observe import observe

logger = logging.getLogger(__name__)


def _tiebreak_key(item: tuple[int, float]) -> tuple[float, int]:
    """Deterministic fusion sort key: ``(score, id)`` used with ``reverse=True``.

    C4.0 / ADR-0108 A: equal fused scores must break by id DESCENDING
    (newer-wins) so ranking is a deterministic total order. Score-only sorts
    left equal-score rows in ``set``-iteration / insertion order, which varies
    across runs (notably the ``set[int]`` union in :func:`_convex_fuse`) and
    let a target tied at the top-N boundary cross the cutoff nondeterministically.

    ``item`` is a ``(memory_id, score)`` pair; the key is ``(score, memory_id)``
    so that under ``reverse=True`` both score and id sort descending.
    """
    return (item[1], item[0])


# Retrieval profiles: fast < balanced < full
# fast:     vector + FTS only, no reranking — lowest latency
# balanced: all 4 signals + cross-encoder — default
# full:     all 4 signals + cross-encoder + NLI — maximum quality
PROFILES: dict[str, dict] = {
    # fast: BM25+HNSW only, no reranking, small candidate pool, no query analysis.
    # Used by all hook handlers (prompt-recall, instructions-loaded, subagent-start).
    # Candidate pool multiplier comes from FAST_PROFILE_CANDIDATE_MULTIPLIER (default 3)
    # instead of the global CANDIDATE_POOL_MULTIPLIER (default 20).
    # skip_query_analysis=True skips _pseudo_hyde_expand + _extract_query_entities +
    # query routing intersection, reducing per-call overhead for short hook queries.
    "fast": {
        "signals": ["vector", "fts"],
        "cross_encoder": False,
        "nli": False,
        "multi_passage": False,
        "skip_query_analysis": True,
        "use_fast_candidate_multiplier": True,
        # ADR-0077 hotfix: fast must actually be fast — memory-only fanout
        # (skip WikiProvider, ~450ms) + no engram-link rerank (250-560ms/call:
        # one get_temporally_linked DB query PER result row). Keep in sync with
        # profiles.py PROFILES["fast"] (two dicts until the pipeline port lands).
        "wiki": False,
        "engram_links": False,
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


@observe(tier="hot", metric="retrieval.fusion.wrrf_fuse_fn")
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

    return sorted(scores.items(), key=_tiebreak_key, reverse=True)


@observe(tier="hot", metric="retrieval.fusion.convex_fuse_fn")
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

    # C4.0: iterate ids in a deterministic order — `set[int]` iteration order
    # varies across runs, and although the final sort re-orders by score, equal
    # scores would otherwise inherit this nondeterministic insertion order.
    combined: dict[int, float] = {}
    for mid in sorted(all_mids):
        combined[mid] = sum(
            norm_weights.get(sig, 0) * normalized.get(sig, {}).get(mid, 0) for sig in normalized
        )
    return sorted(combined.items(), key=_tiebreak_key, reverse=True)


class _FusionMixin:
    """Score-fusion and result-building helpers for Retriever.

    Methods here read ``self._storage``, ``self._settings``, ``self._embeddings``,
    and ``self._reranker`` — all available on the Retriever instance via MRO.
    """

    @staticmethod
    def _normalize_signal(
        sig: str,
        sig_vals: list,
        vals: list,
        fusion_norm: str,
        normalized: dict,
    ) -> None:
        """Normalize one signal's scores into `normalized` dict (in-place)."""
        if fusion_norm == "minmax":
            min_v, max_v = min(vals), max(vals)
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

    @observe(tier="hot", metric="retrieval.fusion.wrrf")
    def _wrrf_fuse(self, scores: dict, signal_weights: dict) -> tuple[dict, list]:
        """WRRF-style normalized weighted sum fusion.

        Returns (fused_scores_dict, sorted_list).
        """
        signal_names = list(
            {sig for mid, sigs in scores.items() for sig, v in sigs.items() if v > 0}
        )
        normalized: dict[int, dict[str, float]] = defaultdict(lambda: defaultdict(float))
        fusion_norm = self._settings.FUSION_NORM

        for sig in signal_names:
            sig_vals = [(mid, s[sig]) for mid, s in scores.items() if s[sig] > 0]
            if not sig_vals:
                continue
            self._normalize_signal(sig, sig_vals, [v for _, v in sig_vals], fusion_norm, normalized)

        combmnz = self._settings.COMBMNZ_ENABLED

        fused_scores: dict = {}
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

        return fused_scores, sorted(fused_scores.items(), key=_tiebreak_key, reverse=True)

    @staticmethod
    @observe(tier="hot", metric="retrieval.fusion.apply_prior_boost")
    def _apply_prior_boost(fused_scores: dict, weight: float, priors: dict) -> list:
        """Apply a precomputed prior boost (additive, O(1)) and return re-sorted list."""
        for mid, prior_val in priors.items():
            if prior_val and mid in fused_scores:
                fused_scores[mid] = fused_scores[mid] + weight * prior_val
        return sorted(fused_scores.items(), key=_tiebreak_key, reverse=True)

    @observe(tier="stage", metric="retrieval.fusion")
    def _fuse_scores(
        self,
        scores: dict,
        w_temporal: float,
        open_domain_mode: bool,
    ) -> tuple[list, dict]:
        """Build signal weights and fuse scores.

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

        fusion_method = self._settings.FUSION_METHOD

        if fusion_method == "convex":
            signal_scores_for_convex: dict[str, dict[int, float]] = {}
            for sig in signal_weights:
                sig_dict = {mid: s[sig] for mid, s in scores.items() if s[sig] > 0}
                if sig_dict:
                    signal_scores_for_convex[sig] = sig_dict
            fused = _convex_fuse(signal_scores_for_convex, signal_weights)
            fused_scores = dict(fused)
        else:
            fused_scores, fused = self._wrrf_fuse(scores, signal_weights)

        # v5.54.1: Apply precomputed graph_prior boost — additive, ALL profiles, O(1).
        # Prior is stored on each memory row during consolidation — NO graph traversal.
        # Confidence gating intentionally bypassed (prior is additive, not a signal).
        try:
            gp_weight = float(self._settings.WRRF_GRAPH_PRIOR_WEIGHT)
        except TypeError:
            gp_weight = 0.0
        if gp_weight > 0 and fused_scores:
            priors = self._storage.get_memory_graph_priors(list(fused_scores.keys()))
            fused = self._apply_prior_boost(fused_scores, gp_weight, priors)

        # v5.54.2: Apply precomputed cofire_prior boost — additive, ALL profiles, O(1).
        # Co-recall (transition-edge) prior — NO transition/graph traversal.
        # Confidence gating intentionally bypassed (additive, not a signal weight).
        try:
            cf_weight = float(self._settings.WRRF_COFIRE_PRIOR_WEIGHT)
        except (TypeError, ValueError):  # fmt: skip
            cf_weight = 0.0
        if cf_weight > 0 and fused_scores:
            cofire_priors = self._storage.get_memory_cofire_priors(list(fused_scores.keys()))
            fused = self._apply_prior_boost(fused_scores, cf_weight, cofire_priors)

        return fused, fused_scores

    @observe(tier="stage", metric="retrieval.inject_ce_diversity")
    def _inject_ce_diversity(
        self,
        result_memories: list[dict],
        seen_ids: set[int],
        scores: dict,
        fused_scores: dict,
        open_domain_mode: bool,
        min_heat: float,
    ) -> None:
        """Inject cross-encoder diversity candidates into result_memories (in-place)."""
        diversity_k = getattr(self._settings, "CE_DIVERSITY_INJECT_K", 10)
        if open_domain_mode:
            diversity_k = max(diversity_k, 15)
        for sig in ["fts", "vector"]:
            # C4.0: tie-break applied at the truncation sort — equal signal
            # scores must break by id deterministically so the SAME candidates
            # survive the `[:diversity_k]` slice across runs (a clean final sort
            # cannot recover a candidate dropped nondeterministically here).
            top_sig = sorted(
                [(mid, s[sig]) for mid, s in scores.items() if s[sig] > 0],
                key=_tiebreak_key,
                reverse=True,
            )[:diversity_k]
            for mid, _ in top_sig:
                if mid in seen_ids:
                    continue
                mem = self._storage.get_memory(mid)
                if mem and mem.get("id") is not None and mem["heat"] >= min_heat:
                    mem["_retrieval_score"] = round(fused_scores.get(mid, 0.0), 4)
                    mem.pop("embedding", None)
                    result_memories.append(mem)
                    seen_ids.add(mid)

    @observe(tier="stage", metric="retrieval.build_results")
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
            self._settings.CROSS_ENCODER_TOP_K,
        )
        result_memories: list[dict] = []
        seen_ids: set[int] = set()
        # v5.97.0: batch-hydrate all fused candidates in ONE query instead of a
        # per-id get_memory loop (the fusion N+1 — 52-55 serial round-trips, ~1100 ms
        # warm; see docs/plans/recall-warm-profile-2026-07-02.md). We then replay the
        # fused order + heat filter + rerank_pool break in Python, so the result is
        # identical to the old loop. NOTE: the `embedding` bytes are intentionally
        # kept on the row here (the pre-v5.97 `mem.pop("embedding")` is removed) so
        # MMR (_reranking_mmr._collect_candidate_embeddings) can read it in-place
        # instead of re-fetching per candidate (Fix 2). The MCP tool boundary strips
        # `embedding` before returning to callers (server/tools/recall.py).
        fused_by_id = {mid: total for mid, total in fused}
        hydrated = {m["id"]: m for m in self._storage.get_memories_by_ids(list(fused_by_id))}
        for mid, total_score in fused:
            mem = hydrated.get(mid)
            if mem and mem.get("id") is not None and mem["heat"] >= min_heat:
                mem["_retrieval_score"] = round(total_score, 4)
                result_memories.append(mem)
                seen_ids.add(mid)
            if len(result_memories) >= rerank_pool:
                break

        use_cross_encoder = profile["cross_encoder"] and getattr(
            self._settings, "CROSS_ENCODER_ENABLED", False
        )
        if use_cross_encoder:
            self._inject_ce_diversity(
                result_memories, seen_ids, scores, fused_scores, open_domain_mode, min_heat
            )

        return result_memories, seen_ids, use_cross_encoder

    @observe(tier="stage", metric="retrieval.comparison_dual_search")
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

    @observe(tier="stage", metric="retrieval.search_profiles_and_beliefs")
    def _search_profiles_and_beliefs(
        self,
        query: str,
        directory: str | None,
        max_results: int,
    ) -> list[dict]:
        """Search structured profiles and derived beliefs."""
        extra_results: list[dict] = []

        # Search profiles
        if self._settings.PROFILE_EXTRACTION_ENABLED:
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
            except (KeyError, TypeError, ValueError):  # fmt: skip
                # Tolerate malformed profile rows; do NOT catch AttributeError —
                # that signals a missing config key (v5.68 fix #38).
                pass

        # Search beliefs
        if self._settings.DERIVED_BELIEFS_ENABLED:
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
            except (KeyError, TypeError, ValueError):  # fmt: skip
                # Tolerate malformed belief rows; do NOT catch AttributeError —
                # that signals a missing config key (mirrors the profile branch
                # above; v5.68 fix #38). A blanket ``except Exception`` here
                # silently dropped ALL beliefs on any storage/config error.
                pass

        return extra_results
