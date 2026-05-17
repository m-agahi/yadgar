"""Confidence and adversarial detection mixin for reranking."""

from __future__ import annotations


class _ConfidenceMixin:
    """Provides detect_adversarial and compute_signal_confidence."""

    def detect_adversarial(self, result_memories: list[dict]) -> dict:
        """Z-score gap analysis for adversarial/low-confidence detection.

        Uses statistical analysis of score distributions to detect:
        1. Flat distributions (all scores similar = no clear winner)
        2. Low absolute scores (nothing really matches)
        3. High diversity of sources needed

        Returns dict with:
        - "is_uncertain": bool — whether the results look unreliable
        - "confidence": float — overall confidence in the result set (0-1)
        - "score_gap": float — z-score normalized gap between top-1 and top-2
        - "abstain": bool — whether retrieval should abstain (very low confidence)
        """
        if len(result_memories) == 0:
            return {"is_uncertain": True, "confidence": 0.0, "score_gap": 0.0, "abstain": True}
        if len(result_memories) == 1:
            score = result_memories[0].get("_retrieval_score", 0.0)
            conf = min(1.0, score * 2)
            return {
                "is_uncertain": conf < 0.3,
                "confidence": conf,
                "score_gap": 0.0,
                "abstain": conf < 0.1,
            }

        scores = [mem.get("_retrieval_score", 0.0) for mem in result_memories]

        # Z-score analysis
        mean_s = sum(scores) / len(scores)
        std_s = (sum((s - mean_s) ** 2 for s in scores) / len(scores)) ** 0.5

        # Q8: deleted dead z-score (computed but never used — z_gap is the wired signal)

        # Score gap between top-1 and top-2
        raw_gap = scores[0] - scores[1]
        z_gap = raw_gap / std_s if std_s > 1e-9 else 0.0

        # Coefficient of variation: low CV = flat distribution = uncertain
        cv = std_s / mean_s if mean_s > 1e-9 else 0.0

        # Confidence from multiple signals:
        # 1. Z-gap: clear winner has high z-gap
        gap_conf = min(1.0, z_gap / 2.0) if z_gap > 0 else 0.0
        # 2. Top-1 absolute score: very low = nothing matches
        abs_conf = min(1.0, scores[0] * 2)
        # 3. Distribution shape: high CV = clear separation
        dist_conf = min(1.0, cv * 2)

        confidence = 0.4 * gap_conf + 0.4 * abs_conf + 0.2 * dist_conf

        is_uncertain = confidence < self._settings.ADVERSARIAL_MIN_CONFIDENCE
        abstain = confidence < 0.15 or (scores[0] < 0.1 and z_gap < 0.5)

        return {
            "is_uncertain": is_uncertain,
            "confidence": round(confidence, 4),
            "score_gap": round(z_gap, 4),
            "abstain": abstain,
        }

    def compute_signal_confidence(
        self,
        signal_name: str,
        ranked_list: list[tuple[int, float]],
    ) -> float:
        """Compute confidence score for a retrieval signal's results.

        Returns a value in [0.0, 1.0] indicating how confident we are
        that this signal produced meaningful results. Used by confidence
        gating to zero out unreliable signals before fusion.
        """
        if signal_name == "vector":
            if not ranked_list:
                return 0.0
            top_score = ranked_list[0][1]
            if len(ranked_list) > 1:
                gap = ranked_list[0][1] - ranked_list[1][1]
            else:
                gap = top_score
            return min(1.0, top_score * (1 + gap))

        elif signal_name == "fts":
            if not ranked_list:
                return 0.0
            return min(1.0, len(ranked_list) / 5.0)

        elif signal_name in ("ppr", "spreading"):
            if not ranked_list:
                return 0.0
            scores = [s for _, s in ranked_list]
            if len(scores) < 2:
                return scores[0] if scores else 0.0
            max_score = max(scores)
            mean_score = sum(scores) / len(scores)
            return (max_score - mean_score) / max_score if max_score > 0 else 0.0

        elif signal_name == "temporal":
            if not ranked_list:
                return 0.0
            return min(1.0, len(ranked_list) / 3.0)

        return 0.5
