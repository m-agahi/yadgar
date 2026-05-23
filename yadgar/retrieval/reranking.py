"""Reranker: cross-encoder, NLI, heuristic, MMR, and multi-passage reranking.

Assembly module: Reranker is composed from per-strategy mixin classes (mixin-of-mixins
pattern). _RerankingMixin is the thin pipeline orchestrator for Retriever's MRO.
"""

import logging

# Per-strategy mixin classes (sibling private modules)
from yadgar.retrieval._reranking_confidence import _ConfidenceMixin
from yadgar.retrieval._reranking_cross_encoder import _CrossEncoderMixin
from yadgar.retrieval._reranking_heuristic import _HeuristicMixin
from yadgar.retrieval._reranking_mmr import _MMRMixin
from yadgar.retrieval._reranking_multi_passage import _MultiPassageMixin
from yadgar.retrieval._reranking_nli import _NLIMixin
from yadgar.tracing import trace_span

logger = logging.getLogger(__name__)


class Reranker(
    _HeuristicMixin,
    _CrossEncoderMixin,  # must precede _MultiPassageMixin (provides score_single_pair, cluster_memories)
    _MultiPassageMixin,
    _NLIMixin,
    _MMRMixin,
    _ConfidenceMixin,
):
    """Holds all reranker state and methods, extracted from Retriever.

    ML scoring is delegated to an MLClient (LocalMLClient or RemoteMLClient),
    so no sentence_transformers import occurs in this module.
    """

    def __init__(self, settings, storage, ml_client=None) -> None:
        self._settings = settings
        self._storage = storage
        if ml_client is None:
            from yadgar.ml_client import LocalMLClient

            ml_client = LocalMLClient(settings)
        self._ml = ml_client

    def unload_if_idle(self, idle_seconds: float = 600.0) -> None:
        """Unload all reranker models if unused for `idle_seconds`. Frees ~500MB RSS."""
        self._ml.unload_if_idle(idle_seconds)


class _RerankingMixin:
    """Reranking pipeline helpers for Retriever.

    Methods here read ``self._reranker``, ``self._rules_engine``, ``self._engram``,
    ``self._metacognition``, and ``self._settings`` — all available on the Retriever
    instance via MRO.
    """

    @trace_span("retrieval.rerank")
    def _apply_rerank_pipeline(
        self,
        result_memories: list[dict],
        seen_ids: set[int],
        query: str,
        query_analysis: dict,
        query_embedding,
        profile: dict,
        profile_name: str,
        open_domain_mode: bool,
        use_cross_encoder: bool,
        max_results: int,
    ) -> list[dict]:
        """Apply full post-fusion reranking pipeline and return final result list."""
        # v5.6.6 E: HEAVY_RERANK_ENABLED=False kill switch — bypass CE/NLI/MP entirely.
        # Useful for CPU-only hosts where every rerank call causes 8-46s saturation.
        if not getattr(self._settings, "HEAVY_RERANK_ENABLED", True):
            return result_memories[:max_results]

        # Heuristic reranker (skipped for 'fast' profile)
        if self._settings.RERANKER_ENABLED and profile_name != "fast":
            heuristic_k = max_results
            if use_cross_encoder:
                heuristic_k = None  # Uses RERANKER_TOP_K (50)
            result_memories = self._reranker.heuristic_rerank(
                result_memories, query, top_k=heuristic_k
            )

        # Comparison dual search: merge extra candidates for "A or B?" queries
        comparison_options = query_analysis.get("comparison_options", [])
        if getattr(self._settings, "COMPARISON_DUAL_SEARCH_ENABLED", False) and comparison_options:
            subject = (
                query_analysis.get("named_entities", [None])[0]
                if query_analysis.get("named_entities")
                else None
            )
            comp_results = self._comparison_dual_search(
                query,
                comparison_options,
                subject,
                max_results,
            )
            for r in comp_results:
                rid = r.get("id", -1)
                if rid not in seen_ids:
                    r.setdefault("_retrieval_score", 0.0)
                    r.pop("embedding", None)
                    result_memories.append(r)
                    seen_ids.add(rid)

        # Cross-encoder reranker
        if use_cross_encoder:
            result_memories = self._reranker.cross_encoder_rerank(result_memories, query)

        # NLI entailment scoring
        # v5.6.6: profile["nli"] is the "this tier allows it" gate; setting is "globally enabled".
        # Use AND semantics so fast/hook profile never triggers NLI even when setting is on.
        use_nli = profile["nli"] and getattr(self._settings, "NLI_RERANKING_ENABLED", False)
        if use_nli and (not self._settings.NLI_ONLY_FOR_OPEN_DOMAIN or open_domain_mode):
            result_memories = self._reranker.nli_rerank(query, result_memories)
            nli_weight = self._settings.NLI_WEIGHT
            for mem in result_memories:
                ce = mem.get("_cross_encoder_score", 0)
                nli = mem.get("_nli_entailment_score", 0)
                mem["_retrieval_score"] = (1 - nli_weight) * ce + nli_weight * nli
            result_memories.sort(key=lambda m: m.get("_retrieval_score", 0), reverse=True)

        # Multi-passage evidence aggregation
        # Profile gate: "multi_passage" key added v5.6.6 — default True for backward compat.
        _mp_allowed = profile.get("multi_passage", True)
        if _mp_allowed and getattr(self._settings, "MULTI_PASSAGE_RERANKING_ENABLED", False):
            result_memories = self._reranker.multi_passage_rerank(
                query, result_memories, max_results
            )

        # Profile and belief search: merge structured knowledge after CE reranking
        directory = ""
        for mem in result_memories:
            if mem.get("directory_context"):
                directory = mem["directory_context"]
                break
        profile_belief_results = self._search_profiles_and_beliefs(
            query,
            directory,
            max_results,
        )
        if profile_belief_results:
            result_memories.extend(profile_belief_results)
            result_memories.sort(
                key=lambda m: m.get("_retrieval_score", 0),
                reverse=True,
            )
            result_memories = result_memories[: max_results * 2]

        # MMR diversity reranking
        if getattr(self._settings, "ADVERSARIAL_DIVERSITY_ENFORCEMENT", False):
            result_memories = self._reranker.mmr_rerank(
                result_memories,
                query_embedding,
                top_k=max_results,
                lambda_param=0.7,
            )

        # Trim to max_results after reranking
        result_memories = result_memories[:max_results]

        # Adversarial detection
        if self._settings.ADVERSARIAL_DETECTION_ENABLED and result_memories:
            adv_info = self._reranker.detect_adversarial(result_memories)
            for mem in result_memories:
                mem["_retrieval_confidence"] = adv_info["confidence"]
            if adv_info["is_uncertain"]:
                logger.debug(
                    "Low retrieval confidence (%.3f), score_gap=%.3f",
                    adv_info["confidence"],
                    adv_info["score_gap"],
                )

        # Apply neuro-symbolic rules
        if self._rules_engine is not None and result_memories:
            directory = ""
            for mem in result_memories:
                if mem.get("directory_context"):
                    directory = mem["directory_context"]
                    break
            result_memories = self._rules_engine.apply_rules(result_memories, directory)
            result_memories = result_memories[:max_results]

        # Enrich with temporal links from engram allocation
        if self._engram is not None:
            for mem in result_memories:
                try:
                    linked = self._engram.get_temporally_linked(mem["id"])
                    if linked:
                        mem["temporal_links"] = linked
                except Exception:
                    pass

        # Apply cognitive load management via metacognition
        if self._metacognition is not None and result_memories:
            try:
                result_memories = self._metacognition.manage_context(result_memories)
            except Exception:
                logger.debug("Metacognition manage_context failed, returning unoptimized")

        return result_memories

    def _score_single_pair(self, query: str, document: str) -> float:
        """Delegate to Reranker.score_single_pair (kept for backward compatibility)."""
        return self._reranker.score_single_pair(query, document)

    def _multi_passage_rerank(self, query: str, memories: list[dict], top_k: int) -> list[dict]:
        """Delegate to Reranker.multi_passage_rerank (kept for backward compatibility)."""
        return self._reranker.multi_passage_rerank(query, memories, top_k)

    @property
    def _gte_reranker(self):
        """Delegate to Reranker ML client's _gte_reranker (kept for backward compatibility)."""
        return self._reranker._ml._gte_reranker
