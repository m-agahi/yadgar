"""Retriever: multi-signal memory retrieval (PPR + spreading + vector + FTS)."""

import logging
import os
from collections import defaultdict
from datetime import datetime

import networkx as nx

from yadgar.config import Settings
from yadgar.embeddings import EmbeddingEngine
from yadgar.knowledge_graph import KnowledgeGraph
from yadgar.retrieval.entities import _extract_query_entities
from yadgar.retrieval.fusion import PROFILES, _FusionMixin
from yadgar.retrieval.graph_helpers import _GraphHelpersMixin
from yadgar.retrieval.quality import _QualityMixin
from yadgar.retrieval.query_analysis import (
    _build_open_domain_subqueries,
    analyze_query,
)
from yadgar.retrieval.reranking import Reranker, _RerankingMixin
from yadgar.retrieval.scoring import _ScoringMixin
from yadgar.storage import BranchFilter, StorageEngine

logger = logging.getLogger(__name__)


class Retriever(_ScoringMixin, _FusionMixin, _RerankingMixin, _GraphHelpersMixin, _QualityMixin):
    """HippoRAG-style retrieval combining PPR, spreading activation,
    vector similarity, and FTS5 keyword search."""

    def __init__(
        self,
        storage: StorageEngine,
        embeddings: EmbeddingEngine,
        knowledge_graph: KnowledgeGraph,
        settings: Settings,
        ml_client=None,
    ) -> None:
        self._storage = storage
        self._embeddings = embeddings
        self._graph = knowledge_graph
        self._settings = settings
        self._engram = None  # Set externally via set_engram()
        self._rules_engine = None  # Set externally via set_rules_engine()
        self._metacognition = None  # Set externally via set_metacognition()
        self._comet_expander = None  # Lazy-loaded COMET query expander
        self._reranker = Reranker(settings, storage, ml_client=ml_client)

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

    def recall(
        self,
        query: str,
        max_results: int = 5,
        min_heat: float = 0.1,
        current_branch: str | None = None,
        default_branch: str | None = None,
        profile: str | None = None,
    ) -> list[dict]:
        """Combine retrieval signals via Weighted Reciprocal Rank Fusion (WRRF).

        Each signal produces a ranked list of memory IDs. Scores are fused as:
          WRRF_score(d) = Σ_i [ w_i / (k + rank_i(d)) ]
        where k = WRRF_K (default 60) and w_i are per-signal weights from settings.
        An optional heuristic reranker refines the final ordering.

        Args:
            current_branch: Active git branch, or None for non-git/unknown contexts.
                When set (along with default_branch), candidate queries are filtered
                to (NULL | default | current) branch in SurrealQL — avoids fetching
                rows that will be discarded (C2).
            default_branch: Repository default branch (e.g. 'master', 'main').
                Must be provided alongside current_branch to enable filtering.
                When None (default), no branch filter is applied — all rows pass.
        """
        # Build branch filter for storage-level predicate injection (C2).
        # Only created when default_branch is explicitly provided by caller.
        # Without it, behavior is backward-compatible: no filtering.
        branch_filter: BranchFilter | None = None
        if default_branch is not None:
            branch_filter = BranchFilter(
                current_branch=current_branch,
                default_branch=default_branch,
            )

        # Determine active retrieval profile.
        # Caller-supplied `profile` kwarg overrides RETRIEVAL_PROFILE setting.
        # Hook handlers pass profile="fast" to skip CE/NLI/MP for lightweight context injection.
        profile_name = (
            profile
            if profile is not None
            else getattr(self._settings, "RETRIEVAL_PROFILE", "balanced")
        )
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

        # 1 + 1b + 1c. FTS, entity-FTS, and COMET expansion scores
        self._collect_fts_scores(
            query,
            scores,
            enabled_signals,
            open_domain_subqueries,
            open_domain_mode,
            candidate_k,
            min_heat,
            branch_filter=branch_filter,
        )

        # 2. Vector similarity via SurrealDB KNN
        vector_memory_ids, query_embedding = self._collect_vector_scores(
            query,
            scores,
            enabled_signals,
            open_domain_subqueries,
            candidate_k,
            min_heat,
            branch_filter=branch_filter,
        )

        # 3. PPR graph retrieval
        self._collect_ppr_scores(query, scores, enabled_signals, candidate_k)

        # 4. Spreading activation from top vector results
        self._collect_spreading_scores(scores, enabled_signals, vector_memory_ids)

        # 5. Temporal retrieval boost
        w_temporal = self._collect_temporal_scores(
            query, scores, min_heat, candidate_k, branch_filter=branch_filter
        )

        # Fusion: confidence gating + WRRF/convex combination
        fused, fused_scores = self._fuse_scores(scores, w_temporal, open_domain_mode)

        # Build result memories + CE diversity injection
        result_memories, seen_ids, use_cross_encoder = self._build_initial_results(
            fused, fused_scores, scores, profile, open_domain_mode, max_results, min_heat
        )

        # Post-fusion reranking pipeline
        return self._apply_rerank_pipeline(
            result_memories,
            seen_ids,
            query,
            query_analysis,
            query_embedding,
            profile,
            profile_name,
            open_domain_mode,
            use_cross_encoder,
            max_results,
        )
