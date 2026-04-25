"""Retriever: multi-signal memory retrieval (PPR + spreading + vector + FTS)."""

import logging
import os
from collections import defaultdict
from datetime import datetime

import networkx as nx

from yadgar.config import Settings
from yadgar.embeddings import EmbeddingEngine
from yadgar.knowledge_graph import KnowledgeGraph
from yadgar.retrieval.entities import _QUERY_STOP_WORDS, _extract_query_entities
from yadgar.retrieval.fusion import PROFILES, _convex_fuse
from yadgar.retrieval.query_analysis import (
    _build_boosted_fts_query,
    _build_open_domain_subqueries,
    _pseudo_hyde_expand,
    analyze_query,
)
from yadgar.retrieval.reranking import Reranker
from yadgar.retrieval.temporal import parse_temporal_expression
from yadgar.storage import StorageEngine

# Lazy import to avoid circular dependency
_RulesEngine = None


def _get_rules_engine_class():
    global _RulesEngine
    if _RulesEngine is None:
        from yadgar.rules_engine import RulesEngine

        _RulesEngine = RulesEngine
    return _RulesEngine


# Lazy import to avoid circular dependency
_EngramAllocator = None


def _get_engram_class():
    global _EngramAllocator
    if _EngramAllocator is None:
        from yadgar.engram import EngramAllocator

        _EngramAllocator = EngramAllocator
    return _EngramAllocator


logger = logging.getLogger(__name__)


class Retriever:
    """HippoRAG-style retrieval combining PPR, spreading activation,
    vector similarity, and FTS5 keyword search."""

    def __init__(
        self,
        storage: StorageEngine,
        embeddings: EmbeddingEngine,
        knowledge_graph: KnowledgeGraph,
        settings: Settings,
    ) -> None:
        self._storage = storage
        self._embeddings = embeddings
        self._graph = knowledge_graph
        self._settings = settings
        self._engram = None  # Set externally via set_engram()
        self._rules_engine = None  # Set externally via set_rules_engine()
        self._metacognition = None  # Set externally via set_metacognition()
        self._comet_expander = None  # Lazy-loaded COMET query expander
        self._reranker = Reranker(settings, storage)

    def set_engram(self, engram) -> None:
        """Attach an EngramAllocator for temporal linking in recall results."""
        self._engram = engram

    def set_rules_engine(self, rules_engine) -> None:
        """Attach a RulesEngine for neuro-symbolic filtering/re-ranking."""
        self._rules_engine = rules_engine

    def set_metacognition(self, metacognition) -> None:
        """Attach a MetaCognition engine for cognitive load management."""
        self._metacognition = metacognition

    def unload_rerankers_if_idle(self, idle_seconds: float = 600.0) -> None:
        """Unload all reranker models if unused for `idle_seconds`. Frees ~500MB RSS."""
        self._reranker.unload_if_idle(idle_seconds)

    # -- COMET query expansion --

    def _comet_expand_query(self, query: str) -> list[str]:
        """Use COMET-BART to generate commonsense expansions for a query.

        Reformulates the query as an event and generates xWant/xAttr inferences
        to bridge the cue-trigger semantic disconnect at query time.
        """
        if not getattr(self._settings, "COMET_QUERY_EXPANSION_ENABLED", False):
            return []
        try:
            if self._comet_expander is None:
                from yadgar.enrichment import CometInferencer

                self._comet_expander = CometInferencer()

            # Reformulate query as a COMET-compatible event
            from yadgar.retrieval.query_analysis import _question_to_statement

            statement = _question_to_statement(query)
            # Generate xWant and xAttr inferences
            inferences = self._comet_expander.infer(statement, self._settings)
            if inferences:
                logger.debug("COMET query expansion: %s -> %s", query[:60], inferences[:3])
            return inferences
        except Exception as e:
            logger.debug("COMET query expansion failed: %s", e)
            return []

    # -- Vector search --

    def _dual_vector_search(self, query_embedding, top_k: int) -> list[tuple[int, float]]:
        """Search both explicit and implicit vector spaces."""
        if not getattr(self._settings, "DUAL_VECTORS_ENABLED", False):
            return []

        explicit_results = self._storage.search_vectors(query_embedding, top_k)
        implicit_results = self._storage.search_implicit_vectors(query_embedding, top_k)

        # Merge with weighted combination
        explicit_weight = 1 - self._settings.IMPLICIT_VECTOR_WEIGHT
        implicit_weight = self._settings.IMPLICIT_VECTOR_WEIGHT

        scores = {}
        for mid, score in explicit_results:
            scores[mid] = explicit_weight * score
        for mid, score in implicit_results:
            scores[mid] = scores.get(mid, 0) + implicit_weight * score

        return sorted(scores.items(), key=lambda x: x[1], reverse=True)[:top_k]

    # -- a. Personalized PageRank Retrieval --

    def ppr_retrieve(self, query: str, top_k: int = 10) -> list[tuple[int, float]]:
        """Run Personalized PageRank seeded by query entities.

        Returns (memory_id, ppr_score) sorted by score descending.
        """
        # 1. Extract entities from query
        query_terms = _extract_query_entities(query)
        if not query_terms:
            return []

        # 2. Find matching entities in the knowledge graph
        seed_entity_ids: list[int] = []
        for term in query_terms:
            if len(term) < self._settings.GRAPH_ENTITY_MIN_LENGTH:
                continue
            entity = self._storage.get_entity_by_name(term)
            if entity:
                seed_entity_ids.append(entity["id"])

        if not seed_entity_ids:
            return []

        # 3. Build a networkx graph from entity-relationship data
        G = self._build_networkx_graph(seed_entity_ids)
        if len(G) == 0:
            return []

        # 4. Run Personalized PageRank
        personalization = {eid: 1.0 / len(seed_entity_ids) for eid in seed_entity_ids if eid in G}
        if not personalization:
            return []

        try:
            ppr_scores = nx.pagerank(
                G,
                alpha=self._settings.PPR_DAMPING,
                personalization=personalization,
                max_iter=self._settings.PPR_ITERATIONS,
            )
        except nx.PowerIterationFailedConvergence:
            ppr_scores = nx.pagerank(
                G,
                alpha=self._settings.PPR_DAMPING,
                personalization=personalization,
                max_iter=self._settings.PPR_ITERATIONS * 2,
                tol=1e-4,
            )

        # 5. Map high-PPR entities back to their associated memories
        entity_scores = sorted(ppr_scores.items(), key=lambda x: x[1], reverse=True)

        memory_scores: dict[int, float] = defaultdict(float)
        for entity_id, score in entity_scores:
            entity = self._storage.get_entity_by_id(entity_id)
            if not entity:
                continue
            entity_name = entity["name"]
            # Find memories containing this entity name via FTS5
            associated = self._find_memories_for_entity(entity_name)
            for mid in associated:
                memory_scores[mid] = max(memory_scores[mid], score)

        # Sort by score descending, return top_k
        ranked = sorted(memory_scores.items(), key=lambda x: x[1], reverse=True)
        return ranked[:top_k]

    # -- b. Contextual Prefix Generation --

    def generate_contextual_prefix(
        self,
        content: str,
        directory: str,
        tags: list[str],
        timestamp: datetime,
    ) -> str:
        """Generate a contextual prefix for richer embedding semantics."""
        dir_basename = os.path.basename(directory.rstrip("/")) or directory
        tags_joined = ", ".join(tags) if tags else "none"
        timestamp_human = timestamp.strftime("%Y-%m-%d %H:%M")

        # Find top co-occurring entities for context enrichment
        top_entities = self._get_top_cooccurring_entities(content, limit=5)
        entities_str = ", ".join(top_entities) if top_entities else "none"

        return (
            f"[Project: {dir_basename}] [Directory: {directory}] "
            f"[Tags: {tags_joined}] [Recorded: {timestamp_human}] "
            f"[Related entities: {entities_str}] "
        )

    # -- c. Spreading Activation --

    def spreading_activation(
        self,
        seed_memories: list[int],
        spread_factor: float | None = None,
        max_depth: int | None = None,
    ) -> list[tuple[int, float]]:
        """Activate related memories by spreading through the entity graph.

        Returns (memory_id, activation_score) for discovered memories
        (excludes seed memories).
        """
        if spread_factor is None:
            spread_factor = self._settings.GRAPH_SPREADING_DECAY
        if max_depth is None:
            max_depth = self._settings.GRAPH_SPREADING_MAX_DEPTH

        if not seed_memories:
            return []

        # 1. Find entities associated with seed memories
        seed_entities: set[int] = set()
        for mid in seed_memories:
            mem = self._storage.get_memory(mid)
            if not mem:
                continue
            entities = self._find_entities_in_content(mem["content"])
            seed_entities.update(entities)

        if not seed_entities:
            return []

        # 2. BFS through entity graph up to max_depth
        activated: dict[int, float] = {}  # memory_id -> activation score
        seed_memory_set = set(seed_memories)

        visited_entities: set[int] = set(seed_entities)
        frontier: list[tuple[int, int]] = [(eid, 0) for eid in seed_entities]

        while frontier:
            next_frontier: list[tuple[int, int]] = []
            for entity_id, depth in frontier:
                if depth >= max_depth:
                    continue
                # Get connected entities
                neighbors = self._graph._get_adjacent(entity_id, None)
                for neighbor in neighbors:
                    nid = neighbor["entity_id"]
                    if nid in visited_entities:
                        continue
                    visited_entities.add(nid)
                    current_depth = depth + 1
                    activation = spread_factor**current_depth

                    # Find memories for this neighbor entity
                    entity_row = self._storage.get_entity_by_id(nid)
                    if entity_row:
                        mids = self._find_memories_for_entity(entity_row["name"])
                        for mid in mids:
                            if mid not in seed_memory_set:
                                activated[mid] = max(activated.get(mid, 0.0), activation)

                    next_frontier.append((nid, current_depth))
            frontier = next_frontier

        # Sort by activation score descending
        return sorted(activated.items(), key=lambda x: x[1], reverse=True)

    # -- d. Unified Recall --

    def recall(self, query: str, max_results: int = 5, min_heat: float = 0.1) -> list[dict]:
        """Combine retrieval signals via Weighted Reciprocal Rank Fusion (WRRF).

        Each signal produces a ranked list of memory IDs. Scores are fused as:
          WRRF_score(d) = Σ_i [ w_i / (k + rank_i(d)) ]
        where k = WRRF_K (default 60) and w_i are per-signal weights from settings.
        An optional heuristic reranker refines the final ordering.
        """
        # Determine active retrieval profile
        profile_name = getattr(self._settings, "RETRIEVAL_PROFILE", "balanced")
        profile = PROFILES.get(profile_name, PROFILES["balanced"])
        profile_signals = set(profile["signals"])

        w_temporal = 0.0  # Dynamically set if temporal markers found

        scores: dict[int, dict] = defaultdict(
            lambda: {
                "vector": 0.0,
                "fts": 0.0,
                "ppr": 0.0,
                "spread": 0.0,
                "temporal": 0.0,
            }
        )

        query_analysis = analyze_query(query, self._settings)

        # Query-dependent signal routing (intersected with profile signals)
        if self._settings.QUERY_ROUTING_ENABLED:
            enabled_signals = set(query_analysis.get("enabled_signals", [])) & profile_signals
        else:
            # Without routing, use all profile signals
            # None means all signals — preserve that behavior for balanced/full (all 4)
            enabled_signals = None if len(profile_signals) >= 4 else profile_signals

        open_domain_mode = query_analysis.get("is_open_domain_like", False)
        open_domain_subqueries = (
            _build_open_domain_subqueries(query, query_analysis) if open_domain_mode else []
        )

        candidate_k = max_results * self._settings.CANDIDATE_POOL_MULTIPLIER
        if open_domain_mode:
            candidate_k = int(
                candidate_k * getattr(self._settings, "OPEN_DOMAIN_CANDIDATE_MULTIPLIER", 1.5)
            )

        # 1. FTS5 keyword search with actual BM25 scores
        #    Boost: duplicate entities 2x and content words 1x in FTS query
        if enabled_signals is None or "fts" in enabled_signals:
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
                    )
                    if not fts_scored:
                        continue
                    # SurrealDB returns negative BM25 scores — use min-max normalization
                    bm25_vals = [s for _, s in fts_scored]
                    bm25_min, bm25_max = min(bm25_vals), max(bm25_vals)
                    bm25_range = bm25_max - bm25_min
                    for mid, bm25_score in fts_scored:
                        normalized = (
                            (bm25_score - bm25_min) / bm25_range if bm25_range > 1e-9 else 0.5
                        )
                        scores[mid]["fts"] = max(
                            scores[mid].get("fts", 0.0),
                            normalized * strength,
                        )
            except Exception:
                pass

        # 1b. Entity-focused FTS: search for just person names to ensure
        #     all memories about mentioned people reach the CE pool.
        #     Critical for open_domain questions where inference depends on
        #     knowing all facts about a person, not just keyword matches.
        if enabled_signals is None or "fts" in enabled_signals:
            try:
                entity_names = [
                    w.strip(".,;:!?()[]{}\"'")
                    for w in query.split()
                    if w[0:1].isupper()
                    and len(w.strip(".,;:!?()[]{}\"'")) >= 2
                    and w.strip(".,;:!?()[]{}\"'").lower() not in _QUERY_STOP_WORDS
                ]
                if entity_names:
                    # Just space-separate; _preprocess_fts_query will OR them
                    entity_query = " ".join(entity_names)
                    entity_hits = self._storage.search_memories_fts_scored(
                        entity_query, min_heat=min_heat, limit=candidate_k
                    )
                    if entity_hits:
                        # SurrealDB returns negative BM25 scores — use min-max normalization
                        ent_vals = [s for _, s in entity_hits]
                        ent_min, ent_max = min(ent_vals), max(ent_vals)
                        ent_range = ent_max - ent_min
                        for mid, ent_score in entity_hits:
                            normalized = (
                                (ent_score - ent_min) / ent_range if ent_range > 1e-9 else 0.5
                            )
                            # Use max to not overwrite a better FTS score
                            scores[mid]["fts"] = max(
                                scores[mid].get("fts", 0.0),
                                normalized * (0.7 if open_domain_mode else 0.5),
                            )
            except Exception:
                pass

        # 1c. COMET query expansion: generate commonsense inferences from query
        #     to bridge the cue-trigger semantic disconnect for open_domain queries.
        #     E.g., "Would Caroline be considered religious?" → xWant: "go to church"
        if open_domain_mode and (enabled_signals is None or "fts" in enabled_signals):
            comet_terms = self._comet_expand_query(query)
            if comet_terms:
                try:
                    comet_query = " ".join(comet_terms[:6])
                    comet_hits = self._storage.search_memories_fts_scored(
                        comet_query,
                        min_heat=min_heat,
                        limit=candidate_k,
                    )
                    if comet_hits:
                        # SurrealDB returns negative BM25 scores — use min-max normalization
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
                                normalized * 0.6,  # Lower weight than direct FTS match
                            )
                except Exception:
                    pass

        # 2. Vector similarity via sqlite-vec KNN
        #    Dual vector search: search with both original AND HyDE-expanded queries
        #    to maximize recall. Union candidates, keep max similarity per memory.
        vector_memory_ids: list[int] = []
        query_embedding = None
        if enabled_signals is None or "vector" in enabled_signals:
            vector_searches = [(query, 1.0)]

            if self._settings.QUERY_EXPANSION_ENABLED:
                expanded_query = _pseudo_hyde_expand(query)
                if expanded_query and expanded_query != query:
                    vector_searches.append((expanded_query, 0.95))

            if open_domain_subqueries:
                for subquery in open_domain_subqueries[:2]:
                    vector_searches.append((subquery, 0.85))

            seen_vector_queries: set[str] = set()
            for vector_query, strength in vector_searches:
                lowered = vector_query.lower()
                if lowered in seen_vector_queries:
                    continue
                seen_vector_queries.add(lowered)

                encoded = self._embeddings.encode_query(vector_query)
                if encoded is None:
                    continue
                if vector_query == query:
                    query_embedding = encoded

                vec_hits = self._storage.search_vectors(
                    encoded, top_k=candidate_k, min_heat=min_heat
                )
                for mid, distance in vec_hits:
                    similarity = (1.0 / (1.0 + distance)) * strength
                    scores[mid]["vector"] = max(scores[mid].get("vector", 0.0), similarity)
                    if mid not in vector_memory_ids:
                        vector_memory_ids.append(mid)

        # 3. PPR graph retrieval
        if enabled_signals is None or "ppr" in enabled_signals:
            ppr_results = self.ppr_retrieve(query, top_k=candidate_k)
            if ppr_results:
                max_ppr = max(s for _, s in ppr_results) if ppr_results else 1.0
                for mid, ppr_score in ppr_results:
                    # Normalize PPR scores to 0-1 range
                    normalized = ppr_score / max_ppr if max_ppr > 0 else 0.0
                    scores[mid]["ppr"] = normalized

        # 4. Spreading activation from top vector results
        if enabled_signals is None or "spreading" in enabled_signals:
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

        # 5. Temporal retrieval boost — temporal_retrieval signal
        if getattr(self._settings, "TEMPORAL_RETRIEVAL_ENABLED", False):
            temporal_info = parse_temporal_expression(query)
            if temporal_info["has_temporal"]:
                try:
                    # A) Content-based temporal matching (FTS on dates in content)
                    temporal_memories = self._storage.search_memories_by_content_date(
                        date_hints=temporal_info["date_hints"],
                        month_hints=temporal_info["month_hints"],
                        session_hints=temporal_info["session_hints"],
                        min_heat=min_heat,
                        limit=candidate_k,
                    )
                    if temporal_memories:
                        for i, mem in enumerate(temporal_memories):
                            scores[mem["id"]]["temporal"] = 1.0 / (1 + i)
                        w_temporal = 0.8

                    # B) Timestamp-based temporal matching (created_at proximity)
                    if temporal_info["month_hints"]:
                        month_matches = self._storage.search_memories_by_month(
                            temporal_info["month_hints"],
                            min_heat=min_heat,
                            limit=candidate_k,
                        )
                        if month_matches:
                            for mid in month_matches:
                                # Add temporal score (lower than FTS match)
                                if scores[mid]["temporal"] == 0.0:
                                    scores[mid]["temporal"] = 0.5
                            if w_temporal == 0.0:
                                w_temporal = 0.6
                except Exception:
                    logger.debug("Temporal retrieval failed, skipping signal")

        # Score-weighted fusion: use actual signal scores (not rank-based)
        # Each memory's final score = Σ (w_i * score_i) for each signal
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

        # --- Fusion: convex combination vs WRRF (existing) ---
        fusion_method = getattr(self._settings, "FUSION_METHOD", "wrrf")

        if fusion_method == "convex":
            # Build signal_scores: signal_name -> {memory_id: raw_score}
            signal_scores_for_convex: dict[str, dict[int, float]] = {}
            for sig in signal_weights:
                sig_dict = {mid: s[sig] for mid, s in scores.items() if s[sig] > 0}
                if sig_dict:
                    signal_scores_for_convex[sig] = sig_dict
            fused = _convex_fuse(signal_scores_for_convex, signal_weights)
            fused_scores = dict(fused)
        else:
            # Existing WRRF-style normalized weighted sum
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

        # Build result memories — pull more candidates for reranker
        rerank_pool = max(
            max_results,
            self._settings.RERANKER_TOP_K,
            getattr(self._settings, "CROSS_ENCODER_TOP_K", 0),
        )
        result_memories: list[dict] = []
        seen_ids: set[int] = set()
        for mid, total_score in fused:
            mem = self._storage.get_memory(mid)
            if mem and mem["heat"] >= min_heat:
                mem["_retrieval_score"] = round(total_score, 4)
                mem.pop("embedding", None)
                result_memories.append(mem)
                seen_ids.add(mid)
            if len(result_memories) >= rerank_pool:
                break

        # Inject top signal-specific results for CE pool diversity
        # This ensures CE sees the best FTS/vector candidates even if
        # they didn't rank in the fused top-K
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
                        if mem and mem["heat"] >= min_heat:
                            mem["_retrieval_score"] = round(fused_scores.get(mid, 0.0), 4)
                            mem.pop("embedding", None)
                            result_memories.append(mem)
                            seen_ids.add(mid)

        # Heuristic reranker (skipped for 'fast' profile)
        if self._settings.RERANKER_ENABLED and profile_name != "fast":
            # When CE follows, don't clip yet — let CE see the full pool
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

        # Cross-encoder reranker (FlashRank ONNX — fast CPU inference)
        # Feed the raw query directly — CE performs best with the original question.
        # CE query augmentation (concatenating HyDE expansion) was tested and HURTS MRR.
        if use_cross_encoder:
            result_memories = self._reranker.cross_encoder_rerank(result_memories, query)

        # NLI entailment scoring: complementary signal to CE for open-domain queries
        use_nli = profile["nli"] or getattr(self._settings, "NLI_RERANKING_ENABLED", False)
        if use_nli and (not self._settings.NLI_ONLY_FOR_OPEN_DOMAIN or open_domain_mode):
            result_memories = self._reranker.nli_rerank(query, result_memories)
            # Blend NLI with CE score
            nli_weight = self._settings.NLI_WEIGHT
            for mem in result_memories:
                ce = mem.get("_cross_encoder_score", 0)
                nli = mem.get("_nli_entailment_score", 0)
                mem["_retrieval_score"] = (1 - nli_weight) * ce + nli_weight * nli
            result_memories.sort(key=lambda m: m.get("_retrieval_score", 0), reverse=True)

        # Multi-passage evidence aggregation: boost scattered evidence clusters
        if getattr(self._settings, "MULTI_PASSAGE_RERANKING_ENABLED", False):
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

        # MMR diversity reranking — avoid all top-K from same conversation segment
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

        # Apply neuro-symbolic rules (hard filter + soft re-rank) as final step
        if self._rules_engine is not None and result_memories:
            # Infer directory from first memory or use empty string
            directory = ""
            for mem in result_memories:
                if mem.get("directory_context"):
                    directory = mem["directory_context"]
                    break
            result_memories = self._rules_engine.apply_rules(result_memories, directory)
            # Re-trim to max_results after filtering
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

    # -- Internal helpers --

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

    def _build_networkx_graph(
        self, seed_entity_ids: list[int], max_hops: int | None = None
    ) -> nx.DiGraph:
        """Build a networkx DiGraph around the seed entities."""
        if max_hops is None:
            max_hops = self._settings.GRAPH_MAX_HOPS
        G = nx.DiGraph()
        visited: set[int] = set()
        frontier = list(seed_entity_ids)

        for _ in range(max_hops):
            next_frontier: list[int] = []
            for eid in frontier:
                if eid in visited:
                    continue
                visited.add(eid)
                G.add_node(eid)
                neighbors = self._graph._get_adjacent(eid, None)
                for n in neighbors:
                    nid = n["entity_id"]
                    weight = n["weight"]
                    if weight < self._settings.GRAPH_MIN_EDGE_WEIGHT:
                        continue
                    G.add_node(nid)
                    G.add_edge(eid, nid, weight=weight)
                    G.add_edge(nid, eid, weight=weight)
                    if nid not in visited:
                        next_frontier.append(nid)
            frontier = next_frontier

        return G

    def _find_memories_for_entity(self, entity_name: str) -> list[int]:
        """Find memory IDs whose content contains the entity name."""
        return self._storage.find_memory_ids_by_entity_name(entity_name)

    def _find_entities_in_content(self, content: str) -> set[int]:
        """Find entity IDs that appear in the given content."""
        entity_ids: set[int] = set()
        # Get all active entities and check which ones appear in the content
        entities = self._storage.get_all_entities(min_heat=0.0, include_archived=True)
        for entity in entities:
            if entity["name"] in content:
                entity_ids.add(entity["id"])
        return entity_ids

    def _compute_signal_confidence(
        self,
        signal_name: str,
        ranked_list: list[tuple[int, float]],
    ) -> float:
        """Delegate to Reranker.compute_signal_confidence (kept for backward compatibility)."""
        return self._reranker.compute_signal_confidence(signal_name, ranked_list)

    def _detect_adversarial(self, result_memories: list[dict]) -> dict:
        """Delegate to Reranker.detect_adversarial (kept for backward compatibility)."""
        return self._reranker.detect_adversarial(result_memories)

    def _cluster_memories(self, memories: list[dict]) -> list[list[dict]]:
        """Delegate to Reranker.cluster_memories (kept for backward compatibility)."""
        return self._reranker.cluster_memories(memories)

    def _score_single_pair(self, query: str, document: str) -> float:
        """Delegate to Reranker.score_single_pair (kept for backward compatibility)."""
        return self._reranker.score_single_pair(query, document)

    def _multi_passage_rerank(self, query: str, memories: list[dict], top_k: int) -> list[dict]:
        """Delegate to Reranker.multi_passage_rerank (kept for backward compatibility)."""
        return self._reranker.multi_passage_rerank(query, memories, top_k)

    @property
    def _gte_reranker(self):
        """Delegate to Reranker._gte_reranker (kept for backward compatibility)."""
        return self._reranker._gte_reranker

    def _get_top_cooccurring_entities(self, content: str, limit: int = 5) -> list[str]:
        """Find entities that co-occur with entities mentioned in this content."""
        # Find entities mentioned in the content
        content_entities = self._find_entities_in_content(content)
        if not content_entities:
            return []

        # Count co-occurrence partners
        partner_counts: dict[str, float] = defaultdict(float)
        for eid in content_entities:
            neighbors = self._graph._get_adjacent(eid, None)
            for n in neighbors:
                entity_row = self._storage.get_entity_by_id(n["entity_id"])
                if entity_row and n["entity_id"] not in content_entities:
                    partner_counts[entity_row["name"]] += n["weight"]

        # Sort by weight and return top
        sorted_partners = sorted(partner_counts.items(), key=lambda x: x[1], reverse=True)
        return [name for name, _ in sorted_partners[:limit]]
