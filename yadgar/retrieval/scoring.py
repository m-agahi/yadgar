"""_ScoringMixin: signal-collection helpers extracted from Retriever.core."""

import logging
import time as _time

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


logger = logging.getLogger(__name__)


class _ScoringMixin:
    """Score-collector helpers for Retriever.

    Methods here read ``self._storage``, ``self._embeddings``, ``self._settings``,
    and ``self._comet_expand_query`` — all available on the Retriever instance via
    the normal MRO.
    """

    # -- Signal collection helpers --

    def _collect_fts_scores(
        self,
        query: str,
        scores: dict,
        enabled_signals,
        open_domain_subqueries: list,
        open_domain_mode: bool,
        candidate_k: int,
        min_heat: float,
        branch_filter: BranchFilter | None = None,
    ) -> None:
        """Collect FTS BM25 scores (including entity-FTS and COMET expansion) into scores."""
        if enabled_signals is not None and "fts" not in enabled_signals:
            return
        _bm25_t0 = _time.perf_counter()

        # 1. FTS5 keyword search with actual BM25 scores
        try:
            fts_searches = [(query, 1.0)]
            if open_domain_subqueries:
                for subquery in open_domain_subqueries:
                    fts_searches.append((subquery, 0.8))

            for fts_query, strength in fts_searches:
                fts_scored = self._storage.search_memories_fts_scored(
                    _build_boosted_fts_query(fts_query),
                    min_heat=min_heat,
                    limit=candidate_k,
                    branch_filter=branch_filter,
                )
                if not fts_scored:
                    continue
                bm25_vals = [s for _, s in fts_scored]
                bm25_min, bm25_max = min(bm25_vals), max(bm25_vals)
                bm25_range = bm25_max - bm25_min
                for mid, bm25_score in fts_scored:
                    normalized = (bm25_score - bm25_min) / bm25_range if bm25_range > 1e-9 else 0.5
                    scores[mid]["fts"] = max(
                        scores[mid].get("fts", 0.0),
                        normalized * strength,
                    )
        except Exception:
            pass

        # 1b. Entity-focused FTS: search for just person names to ensure
        #     all memories about mentioned people reach the CE pool.
        try:
            entity_names = [
                w.strip(".,;:!?()[]{}\"'")
                for w in query.split()
                if w[0:1].isupper()
                and len(w.strip(".,;:!?()[]{}\"'")) >= 2
                and w.strip(".,;:!?()[]{}\"'").lower() not in _QUERY_STOP_WORDS
            ]
            if entity_names:
                entity_query = " ".join(entity_names)
                entity_hits = self._storage.search_memories_fts_scored(
                    entity_query,
                    min_heat=min_heat,
                    limit=candidate_k,
                    branch_filter=branch_filter,
                )
                if entity_hits:
                    ent_vals = [s for _, s in entity_hits]
                    ent_min, ent_max = min(ent_vals), max(ent_vals)
                    ent_range = ent_max - ent_min
                    for mid, ent_score in entity_hits:
                        normalized = (ent_score - ent_min) / ent_range if ent_range > 1e-9 else 0.5
                        scores[mid]["fts"] = max(
                            scores[mid].get("fts", 0.0),
                            normalized * (0.7 if open_domain_mode else 0.5),
                        )
        except Exception:
            pass

        # 1c. COMET query expansion
        if open_domain_mode:
            comet_terms = self._comet_expand_query(query)
            if comet_terms:
                try:
                    comet_query = " ".join(comet_terms[:6])
                    comet_hits = self._storage.search_memories_fts_scored(
                        comet_query,
                        min_heat=min_heat,
                        limit=candidate_k,
                        branch_filter=branch_filter,
                    )
                    if comet_hits:
                        comet_vals = [s for _, s in comet_hits]
                        comet_min, comet_max = min(comet_vals), max(comet_vals)
                        comet_range = comet_max - comet_min
                        for mid, comet_score in comet_hits:
                            normalized = (
                                (comet_score - comet_min) / comet_range
                                if comet_range > 1e-9
                                else 0.5
                            )
                            scores[mid]["fts"] = max(
                                scores[mid].get("fts", 0.0),
                                normalized * 0.6,
                            )
                except Exception:
                    pass

        _observe_stage("bm25", (_time.perf_counter() - _bm25_t0) * 1000)

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

        vector_searches = [(query, 1.0)]

        if self._settings.QUERY_EXPANSION_ENABLED:
            expanded_query = _pseudo_hyde_expand(query)
            if expanded_query and expanded_query != query:
                vector_searches.append((expanded_query, 0.95))

        if open_domain_subqueries:
            for subquery in open_domain_subqueries[:2]:
                vector_searches.append((subquery, 0.85))

        seen_vector_queries: set[str] = set()
        _embed_query_observed = False
        _hnsw_total_ms = 0.0
        for vector_query, strength in vector_searches:
            lowered = vector_query.lower()
            if lowered in seen_vector_queries:
                continue
            seen_vector_queries.add(lowered)

            _enc_t0 = _time.perf_counter()
            encoded = self._embeddings.encode_query(vector_query)
            _enc_elapsed = (_time.perf_counter() - _enc_t0) * 1000
            if not _embed_query_observed:
                # Observe once for the canonical query; subsequent queries use the
                # same model so timing is dominated by the first call.
                _observe_stage("embed_query", _enc_elapsed)
                _embed_query_observed = True
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

        return vector_memory_ids, query_embedding

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
        _observe_stage("ppr", (_time.perf_counter() - _ppr_t0) * 1000)

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
        _observe_stage("spreading_activation", (_time.perf_counter() - _spread_t0) * 1000)

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
                for i, mem in enumerate(temporal_memories):
                    if mem.get("id") is not None:
                        scores[mem["id"]]["temporal"] = 1.0 / (1 + i)
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
                    for mid in month_matches:
                        if scores[mid]["temporal"] == 0.0:
                            scores[mid]["temporal"] = 0.5
                    if w_temporal == 0.0:
                        w_temporal = 0.6
        except Exception:
            logger.debug("Temporal retrieval failed, skipping signal")
        return w_temporal
