"""WRRF fusion functions and retrieval profile definitions."""

from __future__ import annotations

# Retrieval profiles: fast < balanced < full
# fast:     vector + FTS only, no reranking — lowest latency
# balanced: all 4 signals + cross-encoder — default
# full:     all 4 signals + cross-encoder + NLI — maximum quality
PROFILES: dict[str, dict] = {
    "fast": {
        "signals": ["vector", "fts"],
        "cross_encoder": False,
        "nli": False,
    },
    "balanced": {
        "signals": ["vector", "fts", "ppr", "spreading"],
        "cross_encoder": True,
        "nli": False,
    },
    "full": {
        "signals": ["vector", "fts", "ppr", "spreading"],
        "cross_encoder": True,
        "nli": True,
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
