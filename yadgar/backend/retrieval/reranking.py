"""Reranker: cross-encoder, NLI, heuristic, MMR, and multi-passage reranking.

Assembly module: Reranker is composed from per-strategy mixin classes (mixin-of-mixins
pattern). _RerankingMixin is the thin pipeline orchestrator for Retriever's MRO.
"""

import logging
import time as _time
from dataclasses import dataclass

from yadgar._shared.contracts.protocols import (
    CacheProtocol,
    MLClientProtocol,
    NullCache,
    NullMLClient,
)
from yadgar._shared.observability.observe import observe

# Per-strategy mixin classes (sibling private modules)
from yadgar.backend.retrieval._reranking_confidence import _ConfidenceMixin
from yadgar.backend.retrieval._reranking_cross_encoder import _CrossEncoderMixin
from yadgar.backend.retrieval._reranking_heuristic import _HeuristicMixin
from yadgar.backend.retrieval._reranking_mmr import _MMRMixin
from yadgar.backend.retrieval._reranking_multi_passage import _MultiPassageMixin
from yadgar.backend.retrieval._reranking_nli import _NLIMixin

logger = logging.getLogger(__name__)


def _observe_recall_stage(stage: str, elapsed_ms: float) -> None:
    """Observe a recall stage duration. No-op on import error."""
    try:
        from yadgar._shared.observability.metrics import yadgar_recall_stage_ms  # noqa: PLC0415

        yadgar_recall_stage_ms.labels(stage=stage).observe(elapsed_ms)
    except ImportError:
        pass


@dataclass
class RerankContext:
    """Query-context bundle for the rerank pipeline.

    Groups the 8 query-scoped parameters that flow unchanged through every
    pipeline stage, so each stage helper receives a single context object
    rather than a wide parameter list.
    """

    query: str
    query_analysis: dict
    query_embedding: object  # raw embedding bytes or None
    profile: dict
    profile_name: str
    open_domain_mode: bool
    use_cross_encoder: bool
    max_results: int
    # Car C7 (0047 §5 C7, plan item 10): the CALLER's project. Two stages below
    # used to read their scope from the FIRST RESULT's own row — see
    # ``_rerank_rules`` / ``_rerank_profile_belief_merge``. Defaulted so the
    # second construction site (stages/ce_rerank.py, whose RetrievalState
    # carries no project) keeps working with the pre-C7 "no scope" behaviour
    # rather than silently inheriting a wrong one.
    project_id: str | None = None


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

    def __init__(
        self,
        settings,
        storage,
        ml_client: MLClientProtocol | None = None,
        ce_cache: CacheProtocol | None = None,
    ) -> None:
        self._settings = settings
        self._storage = storage
        # Car 2 (folder-split #17): the lazy `backend.ml_client.LocalMLClient`
        # fallback is deleted (it was a _shared→backend edge). The composition root
        # (lifecycle._init_embedding_client) selects Local vs Remote and threads it
        # Retriever → Reranker; the default here is a _shared NullMLClient (every
        # score returns None ≡ the circuit-open sentinel every CE/NLI call site
        # already handles). Production always injects the real client, so the null
        # default is never reached in the daemon/backend.
        self._ml: MLClientProtocol = ml_client if ml_client is not None else NullMLClient()
        # Car 1 (#41): the `ce` cache DI seam, typed via CacheProtocol. Car 2:
        # the lazy `backend.cache.get_ce_cache()` fallback is deleted (it was a
        # _shared→backend edge); the default is a _shared NullCache (all-miss ≡
        # pre-Car-1 no-dedup). The composition root injects the REAL process-global
        # `ce` singleton for the production hot path (byte-identical, live dedup).
        self._ce_cache: CacheProtocol = ce_cache if ce_cache is not None else NullCache()

    def unload_if_idle(self, idle_seconds: float = 600.0) -> None:
        """Unload all reranker models if unused for `idle_seconds`. Frees ~500MB RSS."""
        self._ml.unload_if_idle(idle_seconds)


class _RerankingMixin:
    """Reranking pipeline helpers for Retriever.

    Methods here read ``self._reranker``, ``self._rules_engine``, ``self._engram``,
    ``self._metacognition``, and ``self._settings`` — all available on the Retriever
    instance via MRO.
    """

    # ------------------------------------------------------------------
    # Pipeline stage helpers
    # ------------------------------------------------------------------

    @observe(tier="stage", metric="retrieval.rerank.heuristic")
    def _rerank_heuristic(self, result_memories: list[dict], ctx: RerankContext) -> list[dict]:
        """Apply heuristic reranker (skipped for 'fast' profile)."""
        if not (self._settings.RERANKER_ENABLED and ctx.profile_name != "fast"):
            return result_memories
        heuristic_k = ctx.max_results
        if ctx.use_cross_encoder:
            heuristic_k = None  # Uses RERANKER_TOP_K (50)
        return self._reranker.heuristic_rerank(result_memories, ctx.query, top_k=heuristic_k)

    @observe(tier="stage", metric="retrieval.rerank.comparison_merge")
    def _rerank_comparison_merge(
        self,
        result_memories: list[dict],
        seen_ids: set[int],
        ctx: RerankContext,
    ) -> list[dict]:
        """Merge extra candidates for comparison queries ("A or B?")."""
        comparison_options = ctx.query_analysis.get("comparison_options", [])
        if not (self._settings.COMPARISON_DUAL_SEARCH_ENABLED and comparison_options):
            return result_memories
        subject = (
            ctx.query_analysis.get("named_entities", [None])[0]
            if ctx.query_analysis.get("named_entities")
            else None
        )
        comp_results = self._comparison_dual_search(
            ctx.query,
            comparison_options,
            subject,
            ctx.max_results,
        )
        for r in comp_results:
            rid = r.get("id", -1)
            if rid not in seen_ids:
                r.setdefault("_retrieval_score", 0.0)
                r.pop("embedding", None)
                result_memories.append(r)
                seen_ids.add(rid)
        return result_memories

    @observe(tier="stage", metric="retrieval.rerank.cross_encoder")
    def _rerank_cross_encoder(self, result_memories: list[dict], ctx: RerankContext) -> list[dict]:
        """Apply cross-encoder reranker."""
        if not ctx.use_cross_encoder:
            return result_memories
        _ce_t0 = _time.perf_counter()
        result_memories = self._reranker.cross_encoder_rerank(result_memories, ctx.query)
        _observe_recall_stage("cross_encoder", (_time.perf_counter() - _ce_t0) * 1000)
        return result_memories

    @observe(tier="stage", metric="retrieval.rerank.nli")
    def _rerank_nli(self, result_memories: list[dict], ctx: RerankContext) -> list[dict]:
        """Apply NLI entailment scoring.

        v5.6.6: profile["nli"] is the "this tier allows it" gate; setting is
        "globally enabled". AND semantics so fast/hook profile never triggers NLI.
        """
        use_nli = ctx.profile["nli"] and self._settings.NLI_RERANKING_ENABLED
        if not (use_nli and (not self._settings.NLI_ONLY_FOR_OPEN_DOMAIN or ctx.open_domain_mode)):
            return result_memories
        _nli_t0 = _time.perf_counter()
        result_memories = self._reranker.nli_rerank(ctx.query, result_memories)
        nli_weight = self._settings.NLI_WEIGHT
        for mem in result_memories:
            ce = mem.get("_cross_encoder_score", 0)
            nli = mem.get("_nli_entailment_score", 0)
            mem["_retrieval_score"] = (1 - nli_weight) * ce + nli_weight * nli
        result_memories.sort(key=lambda m: m.get("_retrieval_score", 0), reverse=True)
        _observe_recall_stage("nli", (_time.perf_counter() - _nli_t0) * 1000)
        return result_memories

    @observe(tier="stage", metric="retrieval.rerank.multi_passage")
    def _rerank_multi_passage(self, result_memories: list[dict], ctx: RerankContext) -> list[dict]:
        """Apply multi-passage evidence aggregation.

        Profile gate: "multi_passage" key added v5.6.6 — default True for backward compat.
        """
        _mp_allowed = ctx.profile.get("multi_passage", True)
        if not (_mp_allowed and self._settings.MULTI_PASSAGE_RERANKING_ENABLED):
            return result_memories
        return self._reranker.multi_passage_rerank(ctx.query, result_memories, ctx.max_results)

    @observe(tier="stage", metric="retrieval.rerank.profile_belief_merge")
    def _rerank_profile_belief_merge(
        self, result_memories: list[dict], ctx: RerankContext
    ) -> list[dict]:
        """Merge structured knowledge from profile and belief search after CE reranking.

        Car C7 (plan item 10) — MIS-WIRING FIXED. This took its scope from the
        FIRST RESULT's own ``directory_context``, not from the caller: whichever
        row happened to rank first decided which project's profiles and beliefs
        got merged in. Re-keying the read path without fixing this would have
        preserved the bug in new clothes, so the caller's project is threaded
        through ``RerankContext`` instead.
        """
        profile_belief_results = self._search_profiles_and_beliefs(
            ctx.query,
            ctx.project_id,
            ctx.max_results,
        )
        if profile_belief_results:
            result_memories.extend(profile_belief_results)
            result_memories.sort(
                key=lambda m: m.get("_retrieval_score", 0),
                reverse=True,
            )
            result_memories = result_memories[: ctx.max_results * 2]
        return result_memories

    @observe(tier="stage", metric="retrieval.rerank.mmr")
    def _rerank_mmr(self, result_memories: list[dict], ctx: RerankContext) -> list[dict]:
        """Apply MMR diversity reranking."""
        if not self._settings.ADVERSARIAL_DIVERSITY_ENFORCEMENT:
            return result_memories
        return self._reranker.mmr_rerank(
            result_memories,
            ctx.query_embedding,
            top_k=ctx.max_results,
            lambda_param=0.7,
        )

    @observe(tier="stage", metric="retrieval.rerank.adversarial_detect")
    def _rerank_adversarial_detect(self, result_memories: list[dict]) -> list[dict]:
        """Annotate results with retrieval confidence via adversarial detection."""
        if not (self._settings.ADVERSARIAL_DETECTION_ENABLED and result_memories):
            return result_memories
        adv_info = self._reranker.detect_adversarial(result_memories)
        for mem in result_memories:
            mem["_retrieval_confidence"] = adv_info["confidence"]
        if adv_info["is_uncertain"]:
            logger.debug(
                "Low retrieval confidence (%.3f), score_gap=%.3f",
                adv_info["confidence"],
                adv_info["score_gap"],
            )
        return result_memories

    @observe(tier="stage", metric="retrieval.rerank.rules")
    def _rerank_rules(self, result_memories: list[dict], ctx: RerankContext) -> list[dict]:
        """Apply neuro-symbolic rules.

        Car C7 (plan item 10) — MIS-WIRING FIXED, same shape as
        ``_rerank_profile_belief_merge`` above: the rule scope came from the
        first result's own row rather than from the caller, so a single
        out-of-scope row ranking first selected another project's rules.
        """
        if self._rules_engine is None or not result_memories:
            return result_memories
        result_memories = self._rules_engine.apply_rules(result_memories, ctx.project_id or "")
        return result_memories[: ctx.max_results]

    @observe(tier="stage", metric="retrieval.rerank.engram_links")
    def _rerank_engram_links(self, result_memories: list[dict]) -> list[dict]:
        """Enrich with temporal links from engram allocation."""
        if self._engram is None:
            return result_memories
        for mem in result_memories:
            try:
                linked = self._engram.get_temporally_linked(mem["id"])
                if linked:
                    mem["temporal_links"] = linked
            except Exception:  # BLE001-KEEP: per-memory enrichment: the engram lookup reaches storage with no common base, and one memory without temporal links must not drop the whole result set
                pass
        return result_memories

    @observe(tier="stage", metric="retrieval.rerank.metacognition")
    def _rerank_metacognition(self, result_memories: list[dict]) -> list[dict]:
        """Apply cognitive load management via metacognition."""
        if self._metacognition is None or not result_memories:
            return result_memories
        try:
            result_memories = self._metacognition.manage_context(result_memories)
        except Exception:  # BLE001-KEEP: optional rerank stage: metacognition drives the embedding engine and storage, which share no common base, and the documented degrade is 'return the unoptimized list'
            logger.debug("Metacognition manage_context failed, returning unoptimized")
        return result_memories

    # ------------------------------------------------------------------
    # Pipeline orchestrator
    # ------------------------------------------------------------------

    @observe(tier="stage", metric="retrieval.rerank")
    def _apply_rerank_pipeline(
        self,
        result_memories: list[dict],
        seen_ids: set[int],
        ctx: RerankContext,
    ) -> list[dict]:
        """Apply full post-fusion reranking pipeline and return final result list."""
        _rerank_t0 = _time.perf_counter()

        # v5.6.6 E: HEAVY_RERANK_ENABLED=False kill switch — bypass CE/NLI/MP entirely.
        # Useful for CPU-only hosts where every rerank call causes 8-46s saturation.
        if not self._settings.HEAVY_RERANK_ENABLED:
            _observe_recall_stage("rerank_final", (_time.perf_counter() - _rerank_t0) * 1000)
            # v5.97.0: fusion now keeps `embedding` on rows so MMR can read it in-place
            # (avoids the per-candidate re-fetch). Strip it here so the retriever's
            # contract — embedding-free rows — holds on this early-return branch too.
            return self._strip_embeddings(result_memories[: ctx.max_results])

        result_memories = self._rerank_heuristic(result_memories, ctx)
        result_memories = self._rerank_comparison_merge(result_memories, seen_ids, ctx)
        result_memories = self._rerank_cross_encoder(result_memories, ctx)
        result_memories = self._rerank_nli(result_memories, ctx)
        result_memories = self._rerank_multi_passage(result_memories, ctx)
        result_memories = self._rerank_profile_belief_merge(result_memories, ctx)
        result_memories = self._rerank_mmr(result_memories, ctx)

        # Trim to max_results after reranking
        result_memories = result_memories[: ctx.max_results]

        result_memories = self._rerank_adversarial_detect(result_memories)
        result_memories = self._rerank_rules(result_memories, ctx)
        # ADR-0077 hotfix: engram-link enrichment is one get_temporally_linked
        # DB query PER result row (measured 250-560ms) — profiles may disable it
        # (fast does; see profiles.py). Default True preserves legacy behavior.
        if ctx.profile.get("engram_links", True):
            result_memories = self._rerank_engram_links(result_memories)
        result_memories = self._rerank_metacognition(result_memories)

        # P11: observe rerank_final covering the full post-fusion pipeline duration.
        _observe_recall_stage("rerank_final", (_time.perf_counter() - _rerank_t0) * 1000)

        # v5.97.0: strip the `embedding` bytes fusion left on the rows for MMR — the
        # retriever contract returns embedding-free rows (test_pipeline_strips_embeddings).
        return self._strip_embeddings(result_memories)

    @staticmethod
    @observe(tier="hot", metric="retrieval.rerank.strip_embeddings")
    def _strip_embeddings(memories: list[dict]) -> list[dict]:
        """Remove the raw `embedding` bytes from result rows in-place (v5.97.0).

        Fusion keeps `embedding` on fused rows so MMR reads it without re-fetching
        (Fix 2); this is the single retriever-level strip that restores the
        embedding-free output contract. Idempotent — safe on rows that never
        carried an embedding.
        """
        for mem in memories:
            mem.pop("embedding", None)
        return memories

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
