"""_ScoringMixin: signal-collection helpers extracted from Retriever.core."""

import logging
import time as _time
from dataclasses import dataclass

from yadgar.observability.observe import observe
from yadgar.retrieval.entities import _QUERY_STOP_WORDS
from yadgar.retrieval.query_analysis import _build_boosted_fts_query, _pseudo_hyde_expand
from yadgar.retrieval.temporal import parse_temporal_expression
from yadgar.storage import BranchFilter


def _observe_stage(stage: str, elapsed_ms: float) -> None:
    """Observe a recall stage duration. No-op on import error."""
    try:
        from yadgar.metrics import yadgar_recall_stage_ms  # noqa: PLC0415

        yadgar_recall_stage_ms.labels(stage=stage).observe(elapsed_ms)
    except Exception:
        pass


def _set_stage_attrs(**attrs: int) -> None:
    """Set small COUNT attributes on the current OTel stage span (v5.100).

    HARD CONSTRAINT: counts only (ints), never per-item spans or large payloads.
    No-op when no recording span is active or OTel is absent.
    """
    try:
        from opentelemetry import trace as _ot  # noqa: PLC0415

        span = _ot.get_current_span()
        if span is not None and span.is_recording():
            for k, v in attrs.items():
                span.set_attribute(k, v)
    except Exception:
        pass


logger = logging.getLogger(__name__)


@dataclass
class FTSParams:
    """Cohesive parameter object for FTS signal collection."""

    query: str
    enabled_signals: object  # set | None
    open_domain_subqueries: list
    open_domain_mode: bool
    candidate_k: int
    min_heat: float
    branch_filter: BranchFilter | None = None


def _normalize_fts_hits(
    hits: list,
    scores: dict,
    strength: float,
) -> None:
    """Normalize BM25 hit scores and update scores[mid]['fts'] in-place."""
    vals = [s for _, s in hits]
    lo, hi = min(vals), max(vals)
    rng = hi - lo
    for mid, raw in hits:
        normalized = (raw - lo) / rng if rng > 1e-9 else 0.5
        scores[mid]["fts"] = max(scores[mid].get("fts", 0.0), normalized * strength)


class _ScoringMixin:
    """Score-collector helpers for Retriever.

    Methods here read ``self._storage``, ``self._embeddings``, ``self._settings``,
    and ``self._comet_expand_query`` — all available on the Retriever instance via
    the normal MRO.
    """

    # -- Signal collection helpers --

    @observe(tier="hot", name="retrieval.fts.bm25")
    def _run_fts_bm25(self, params: FTSParams, scores: dict) -> None:
        """Section 1: FTS5 keyword search with BM25 scores (main query + subqueries)."""
        fts_searches = [(params.query, 1.0)]
        for subquery in params.open_domain_subqueries:
            fts_searches.append((subquery, 0.8))
        try:
            for fts_query, strength in fts_searches:
                hits = self._storage.search_memories_fts_scored(
                    _build_boosted_fts_query(fts_query),
                    min_heat=params.min_heat,
                    limit=params.candidate_k,
                    branch_filter=params.branch_filter,
                )
                if hits:
                    _normalize_fts_hits(hits, scores, strength)
        except Exception:
            pass

    @observe(tier="hot", name="retrieval.fts.entity")
    def _run_entity_fts(self, params: FTSParams, scores: dict) -> None:
        """Section 1b: Entity-focused FTS for person names mentioned in the query."""
        entity_names = [
            w.strip(".,;:!?()[]{}\"'")
            for w in params.query.split()
            if w[0:1].isupper()
            and len(w.strip(".,;:!?()[]{}\"'")) >= 2
            and w.strip(".,;:!?()[]{}\"'").lower() not in _QUERY_STOP_WORDS
        ]
        if not entity_names:
            return
        try:
            hits = self._storage.search_memories_fts_scored(
                " ".join(entity_names),
                min_heat=params.min_heat,
                limit=params.candidate_k,
                branch_filter=params.branch_filter,
            )
            if hits:
                strength = 0.7 if params.open_domain_mode else 0.5
                _normalize_fts_hits(hits, scores, strength)
        except Exception:
            pass

    @observe(tier="hot", name="retrieval.fts.comet")
    def _run_comet_fts(self, params: FTSParams, scores: dict) -> None:
        """Section 1c: COMET query expansion FTS (open-domain mode only)."""
        if not params.open_domain_mode:
            return
        comet_terms = self._comet_expand_query(params.query)
        if not comet_terms:
            return
        try:
            hits = self._storage.search_memories_fts_scored(
                " ".join(comet_terms[:6]),
                min_heat=params.min_heat,
                limit=params.candidate_k,
                branch_filter=params.branch_filter,
            )
            if hits:
                _normalize_fts_hits(hits, scores, 0.6)
        except Exception:
            pass

    @observe(tier="stage", name="retrieval.fts")
    def _collect_fts_scores(self, scores: dict, params: FTSParams) -> None:
        """Collect FTS BM25 scores (including entity-FTS and COMET expansion) into scores."""
        if params.enabled_signals is not None and "fts" not in params.enabled_signals:
            return
        _bm25_t0 = _time.perf_counter()
        self._run_fts_bm25(params, scores)
        self._run_entity_fts(params, scores)
        self._run_comet_fts(params, scores)
        _observe_stage("bm25", (_time.perf_counter() - _bm25_t0) * 1000)

    @observe(tier="hot", name="retrieval.vector.build_search_list")
    def _build_vector_search_list(
        self,
        query: str,
        open_domain_subqueries: list,
    ) -> list[tuple[str, float]]:
        """Build the ordered list of (vector_query, strength) pairs to search."""
        searches = [(query, 1.0)]
        if self._settings.QUERY_EXPANSION_ENABLED:
            expanded = _pseudo_hyde_expand(query)
            if expanded and expanded != query:
                searches.append((expanded, 0.95))
        for subquery in open_domain_subqueries[:2]:
            searches.append((subquery, 0.85))
        return searches

    @observe(tier="hot", name="retrieval.vector.encode_query")
    def _encode_vector_query(
        self,
        vector_query: str,
        embed_query_observed: bool,
    ) -> tuple[object | None, float, bool]:
        """Encode one query string. Returns (encoded, enc_elapsed_ms, observed_flag)."""
        _enc_t0 = _time.perf_counter()
        encoded = self._embeddings.encode_query(vector_query)
        _enc_elapsed = (_time.perf_counter() - _enc_t0) * 1000
        if not embed_query_observed:
            _observe_stage("embed_query", _enc_elapsed)
            embed_query_observed = True
        return encoded, _enc_elapsed, embed_query_observed

    @observe(tier="stage", name="retrieval.vector")
    def _collect_vector_scores(
        self,
        query: str,
        scores: dict,
        enabled_signals,
        open_domain_subqueries: list,
        candidate_k: int,
        min_heat: float,
        branch_filter: BranchFilter | None = None,
    ) -> tuple[list[int], object]:
        """Collect vector KNN scores into scores. Returns (vector_memory_ids, query_embedding)."""
        vector_memory_ids: list[int] = []
        query_embedding = None
        if enabled_signals is not None and "vector" not in enabled_signals:
            return vector_memory_ids, query_embedding

        vector_searches = self._build_vector_search_list(query, open_domain_subqueries)

        seen_vector_queries: set[str] = set()
        _embed_query_observed = False
        _hnsw_total_ms = 0.0
        for vector_query, strength in vector_searches:
            lowered = vector_query.lower()
            if lowered in seen_vector_queries:
                continue
            seen_vector_queries.add(lowered)

            encoded, _enc_elapsed, _embed_query_observed = self._encode_vector_query(
                vector_query, _embed_query_observed
            )
            if encoded is None:
                continue
            if vector_query == query:
                query_embedding = encoded

            _hnsw_t0 = _time.perf_counter()
            vec_hits = self._storage.search_vectors(
                encoded,
                top_k=candidate_k,
                min_heat=min_heat,
                branch_filter=branch_filter,
            )
            _hnsw_total_ms += (_time.perf_counter() - _hnsw_t0) * 1000
            for mid, distance in vec_hits:
                similarity = (1.0 / (1.0 + distance)) * strength
                scores[mid]["vector"] = max(scores[mid].get("vector", 0.0), similarity)
                if mid not in vector_memory_ids:
                    vector_memory_ids.append(mid)

        if _hnsw_total_ms > 0:
            _observe_stage("hnsw", _hnsw_total_ms)

        _set_stage_attrs(candidates=len(vector_memory_ids))
        return vector_memory_ids, query_embedding

    @observe(tier="stage", name="retrieval.ppr")
    def _collect_ppr_scores(
        self,
        query: str,
        scores: dict,
        enabled_signals,
        candidate_k: int,
    ) -> None:
        """Collect PPR graph retrieval scores into scores."""
        if enabled_signals is not None and "ppr" not in enabled_signals:
            return
        _ppr_t0 = _time.perf_counter()
        ppr_results = self.ppr_retrieve(query, top_k=candidate_k)
        if ppr_results:
            max_ppr = max(s for _, s in ppr_results) if ppr_results else 1.0
            for mid, ppr_score in ppr_results:
                normalized = ppr_score / max_ppr if max_ppr > 0 else 0.0
                scores[mid]["ppr"] = normalized
        _set_stage_attrs(candidates=len(ppr_results) if ppr_results else 0)
        _observe_stage("ppr", (_time.perf_counter() - _ppr_t0) * 1000)

    @observe(tier="stage", name="retrieval.spreading")
    def _collect_spreading_scores(
        self,
        scores: dict,
        enabled_signals,
        vector_memory_ids: list[int],
    ) -> None:
        """Collect spreading activation scores from top vector seeds into scores."""
        if enabled_signals is not None and "spreading" not in enabled_signals:
            return
        _spread_t0 = _time.perf_counter()
        top_vector_seeds = vector_memory_ids[:5]
        if top_vector_seeds:
            spread_results = self.spreading_activation(
                top_vector_seeds, spread_factor=0.5, max_depth=2
            )
            if spread_results:
                max_spread = max(s for _, s in spread_results) if spread_results else 1.0
                for mid, spread_score in spread_results:
                    normalized = spread_score / max_spread if max_spread > 0 else 0.0
                    scores[mid]["spread"] = normalized
            _set_stage_attrs(seeds=len(top_vector_seeds), activated=len(spread_results or []))
        _observe_stage("spreading_activation", (_time.perf_counter() - _spread_t0) * 1000)

    @observe(tier="hot", name="retrieval.temporal.content_scores")
    def _apply_temporal_content_scores(self, temporal_memories: list, scores: dict) -> None:
        """Write content-date temporal scores for each returned memory."""
        for i, mem in enumerate(temporal_memories):
            if mem.get("id") is not None:
                scores[mem["id"]]["temporal"] = 1.0 / (1 + i)

    @observe(tier="hot", name="retrieval.temporal.month_scores")
    def _apply_temporal_month_scores(self, month_matches: list, scores: dict) -> None:
        """Write month-proximity temporal scores for memory IDs not already scored."""
        for mid in month_matches:
            if scores[mid]["temporal"] == 0.0:
                scores[mid]["temporal"] = 0.5

    @observe(tier="stage", name="retrieval.temporal")
    def _collect_temporal_scores(
        self,
        query: str,
        scores: dict,
        min_heat: float,
        candidate_k: int,
        branch_filter: BranchFilter | None = None,
    ) -> float:
        """Collect temporal retrieval scores into scores. Returns w_temporal weight (0.0 if unused)."""
        w_temporal = 0.0
        if not getattr(self._settings, "TEMPORAL_RETRIEVAL_ENABLED", False):
            return w_temporal
        temporal_info = parse_temporal_expression(query)
        if not temporal_info["has_temporal"]:
            return w_temporal
        try:
            # A) Content-based temporal matching (FTS on dates in content)
            temporal_memories = self._storage.search_memories_by_content_date(
                date_hints=temporal_info["date_hints"],
                month_hints=temporal_info["month_hints"],
                session_hints=temporal_info["session_hints"],
                min_heat=min_heat,
                limit=candidate_k,
                branch_filter=branch_filter,
            )
            if temporal_memories:
                self._apply_temporal_content_scores(temporal_memories, scores)
                w_temporal = 0.8

            # B) Timestamp-based temporal matching (created_at proximity)
            if temporal_info["month_hints"]:
                month_matches = self._storage.search_memories_by_month(
                    temporal_info["month_hints"],
                    min_heat=min_heat,
                    limit=candidate_k,
                    branch_filter=branch_filter,
                )
                if month_matches:
                    self._apply_temporal_month_scores(month_matches, scores)
                    if w_temporal == 0.0:
                        w_temporal = 0.6
        except Exception:
            logger.debug("Temporal retrieval failed, skipping signal")
        return w_temporal
