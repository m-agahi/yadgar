"""Dual-store Complementary Learning Systems — fast episodic capture + slow semantic abstraction.

Based on:
- McClelland, McNaughton, O'Reilly (1995): Original CLS theory
- Sun et al. (Nature Neuroscience 26:1438, 2023): Go-CLS — only predictable,
  generalizable patterns transfer to semantic storage
- Tadros et al. (Nature Communications, 2022): Random replay strengthens important memories
"""

import logging
import time

from yadgar._shared.config import Settings
from yadgar._shared.embeddings import EmbeddingEngine
from yadgar._shared.observability.observe import observe
from yadgar._shared.secrets import SecretLeakBlocked
from yadgar._shared.storage import StorageEngine
from yadgar.core.cls_store.clustering import _ClusteringMixin
from yadgar.core.cls_store.patterns import (
    _ARCHITECTURE_KEYWORDS,
    _DECISION_KEYWORDS,
    _SPECIFIC_INDICATORS,
    _PatternsMixin,
)
from yadgar.core.cls_store.patterns import (
    _has_ascii_identifier_token as _has_ascii_identifier_token,
)
from yadgar.core.cls_store.patterns import (
    _has_meaningful_token as _has_meaningful_token,
)
from yadgar.core.cls_store.patterns import (
    _is_degenerate_auto_abstracted as _is_degenerate_auto_abstracted,
)
from yadgar.core.cls_store.promotion import _PromotionMixin

logger = logging.getLogger(__name__)


class DualStoreCLS(_ClusteringMixin, _PatternsMixin, _PromotionMixin):
    """Complementary Learning Systems: episodic (hippocampal) + semantic (neocortical).

    Episodic memories are raw, high-fidelity recordings.
    Semantic memories are abstracted schemas derived from recurring patterns.
    Go-CLS consolidation only promotes patterns that appear CONSISTENTLY
    across multiple sessions — one-off workarounds stay episodic.
    """

    def __init__(
        self,
        storage: StorageEngine,
        embeddings: EmbeddingEngine,
        settings: Settings,
    ) -> None:
        self._storage = storage
        self._embeddings = embeddings
        self._settings = settings

    # ── Go-CLS Consolidation Cycle ────────────────────────────────────────

    @observe(tier="boundary", metric="consolidation.cls.cycle")
    def consolidation_cycle(self) -> dict:
        """Run Go-CLS consolidation: promote recurring episodic patterns to semantic.

        Steps:
        1. find_recurring_patterns() across all directories
        2. For each qualifying cluster:
           a. check_consistency() — skip if contradictions found
           b. abstract_to_schema() — generate semantic summary
           c. Create new semantic memory
           d. Link episodic memories to semantic memory (derived_from)
           e. Do NOT delete episodic memories
        3. Return stats
        """
        logger.info("phase: cls_consolidation starting")
        _cycle_start = time.monotonic()
        stats = {
            "patterns_found": 0,
            "promoted": 0,
            "skipped_inconsistent": 0,
            "skipped_secret": 0,
            "total_episodic": 0,
            "total_semantic": 0,
        }

        _t = time.monotonic()
        logger.info("phase: find_recurring_patterns starting")
        patterns = self.find_recurring_patterns()
        stats["patterns_found"] = len(patterns)
        logger.info(
            "phase: find_recurring_patterns complete in %dms, found %d patterns",
            int((time.monotonic() - _t) * 1000),
            len(patterns),
        )

        for pattern in patterns:
            # Check consistency separately to track skipped_inconsistent
            consistency = self.check_consistency(pattern["memories"])
            if not consistency["consistent"]:
                stats["skipped_inconsistent"] += 1
                continue

            try:
                promoted = self._promote_pattern(pattern)
            except SecretLeakBlocked as exc:
                logger.warning("CLS: pattern skipped — secret-gate blocked promotion: %s", exc)
                stats["skipped_secret"] += 1
                continue
            if promoted:
                stats["promoted"] += 1

        # Count totals
        stats["total_episodic"] = self._storage.count_memories_by_store_type("episodic")
        stats["total_semantic"] = self._storage.count_memories_by_store_type("semantic")

        logger.info(
            "CLS consolidation cycle complete in %dms: %s",
            int((time.monotonic() - _cycle_start) * 1000),
            stats,
        )
        return stats

    # ── Dual-Store Query ──────────────────────────────────────────────────

    @observe(tier="stage", metric="consolidation.cls.query_dual")
    def query_dual(self, query: str, directory: str, prefer: str = "auto") -> list[dict]:
        """Query both episodic and semantic stores, merge results.

        prefer: "auto" (query analysis), "episodic", or "semantic"
        """
        # Determine weighting
        if prefer == "auto":
            episodic_weight, semantic_weight = self._auto_weight(query)
        elif prefer == "episodic":
            episodic_weight, semantic_weight = 2.0, 1.0
        elif prefer == "semantic":
            episodic_weight, semantic_weight = 1.0, 2.0
        else:
            episodic_weight, semantic_weight = 1.0, 1.0

        query_embedding = self._embeddings.encode(query)
        if query_embedding is None:
            return []

        # Search both stores
        episodic_results = self._search_store(query, query_embedding, "episodic", directory)
        semantic_results = self._search_store(query, query_embedding, "semantic", directory)

        # Score and merge
        scored: dict[int, dict] = {}

        for mem, sim in episodic_results:
            scored[mem["id"]] = {
                "memory": mem,
                "score": sim * episodic_weight,
            }

        for mem, sim in semantic_results:
            if mem["id"] in scored:
                scored[mem["id"]]["score"] += sim * semantic_weight
            else:
                scored[mem["id"]] = {
                    "memory": mem,
                    "score": sim * semantic_weight,
                }

        # Sort by combined score
        ranked = sorted(scored.values(), key=lambda x: x["score"], reverse=True)

        results = []
        for item in ranked:
            mem = item["memory"]
            mem["_dual_score"] = round(item["score"], 4)
            mem.pop("embedding", None)
            results.append(mem)

        return results

    # ── Internal Helpers ──────────────────────────────────────────────────

    @observe(tier="hot", metric="consolidation.cls.auto_weight")
    def _auto_weight(self, query: str) -> tuple[float, float]:
        """Analyze query to determine episodic vs semantic weighting.

        Specific queries (file names, error messages) → episodic
        General queries (patterns, conventions) → semantic
        """
        has_specific = bool(_SPECIFIC_INDICATORS.search(query))
        has_semantic_kw = bool(
            _DECISION_KEYWORDS.search(query) or _ARCHITECTURE_KEYWORDS.search(query)
        )

        if has_specific and not has_semantic_kw:
            return 2.0, 1.0  # episodic bias
        elif has_semantic_kw and not has_specific:
            return 1.0, 2.0  # semantic bias
        else:
            return 1.0, 1.0  # balanced
