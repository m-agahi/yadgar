"""Retriever: multi-signal memory retrieval (PPR + spreading + vector + FTS)."""

import logging
import os
import time
from collections import defaultdict
from datetime import datetime

import networkx as nx

from yadgar._shared.config import Settings
from yadgar._shared.embeddings import EmbeddingEngine
from yadgar._shared.knowledge_graph import KnowledgeGraph
from yadgar._shared.observability.observe import observe
from yadgar._shared.storage import StorageEngine
from yadgar._shared.storage.directory import build_recall_scope_clause
from yadgar.backend.retrieval.entities import _extract_query_entities
from yadgar.backend.retrieval.fusion import PROFILES, _FusionMixin
from yadgar.backend.retrieval.graph_helpers import _GraphHelpersMixin
from yadgar.backend.retrieval.quality import _QualityMixin
from yadgar.backend.retrieval.query_analysis import (
    _build_open_domain_subqueries,
    analyze_query,
)
from yadgar.backend.retrieval.reranking import RerankContext, Reranker, _RerankingMixin
from yadgar.backend.retrieval.scoring import FTSParams, _ScoringMixin

logger = logging.getLogger(__name__)


@observe(tier="hot", metric="retrieval.recall.deadline_passed", span=False)
def _deadline_passed(deadline: float | None) -> bool:
    """True when the ADR-0077 client deadline is set and already exceeded."""
    return deadline is not None and time.monotonic() >= deadline


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
        ce_cache=None,
    ) -> None:
        self._storage = storage
        self._embeddings = embeddings
        self._graph = knowledge_graph
        self._settings = settings
        self._engram = None  # Set externally via set_engram()
        self._rules_engine = None  # Set externally via set_rules_engine()
        self._metacognition = None  # Set externally via set_metacognition()
        self._comet_expander = None  # Lazy-loaded COMET query expander
        # Car 2 (folder-split #17): ce_cache is threaded from the composition root
        # (lifecycle injects the real process-global `ce` singleton) → Reranker.
        # None default ⇒ Reranker uses a _shared NullCache (no backend edge).
        self._reranker = Reranker(settings, storage, ml_client=ml_client, ce_cache=ce_cache)
        # v5.31.0: lazy-initialised plugin pipeline (None until first use)
        self._pipeline = None

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

    @observe(tier="stage", metric="retrieval.comet_expand_query")
    def _comet_expand_query(self, query: str) -> list[str]:
        """Use COMET-BART to generate commonsense expansions for a query.

        Reformulates the query as an event and generates xWant/xAttr inferences
        to bridge the cue-trigger semantic disconnect at query time.
        """
        if not self._settings.COMET_QUERY_EXPANSION_ENABLED:
            return []
        try:
            if self._comet_expander is None:
                from yadgar._shared.enrichment import CometInferencer

                self._comet_expander = CometInferencer()

            # Reformulate query as a COMET-compatible event
            from yadgar.backend.retrieval.query_analysis import _question_to_statement

            statement = _question_to_statement(query)
            # Generate xWant and xAttr inferences
            inferences = self._comet_expander.infer(statement, self._settings)
            if inferences:
                logger.debug("COMET query expansion: %s -> %s", query[:60], inferences[:3])
            return inferences
        except Exception as e:
            logger.debug("COMET query expansion failed: %s", e)
            return []

    # -- a. Personalized PageRank Retrieval --

    @observe(tier="stage", metric="retrieval.ppr_retrieve")
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

    @observe(tier="hot", metric="retrieval.generate_contextual_prefix")
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

    @observe(tier="stage", metric="retrieval.spreading_activation")
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
            seed_entities.update(self._find_entities_in_content(mem["content"]))

        if not seed_entities:
            return []

        # 2. BFS through entity graph up to max_depth
        activated: dict[int, float] = {}
        seed_memory_set = set(seed_memories)
        visited_entities: set[int] = set(seed_entities)
        frontier: list[tuple[int, int]] = [(eid, 0) for eid in seed_entities]

        while frontier:
            frontier = self._spreading_bfs_step(
                frontier, visited_entities, activated, seed_memory_set, spread_factor, max_depth
            )

        return sorted(activated.items(), key=lambda x: x[1], reverse=True)

    def _spreading_bfs_step(
        self,
        frontier: list[tuple[int, int]],
        visited_entities: set[int],
        activated: dict[int, float],
        seed_memory_set: set[int],
        spread_factor: float,
        max_depth: int,
    ) -> list[tuple[int, int]]:
        """One BFS expansion step for spreading_activation. Returns next frontier.

        v5.99.0: adjacency for the whole frontier is fetched in ONE batched query
        (``_get_adjacent_batch``).

        v5.102.0: the per-activated-entity fetches (``get_entity_by_id`` +
        ``_find_memories_for_entity`` — the recall #2 N+1, ~136 entities × 2 serial
        round-trips ≈ 5 s) are now batched PER BFS DEPTH into ONE entity query
        (``get_entities_by_ids``) + ONE multi-statement memory query
        (``_find_memories_for_entities``). Two-pass over the frontier:
          - pass 1 runs the visited-guard, records each discovered entity's
            ``(nid, current_depth)`` in EXACT discovery order, and builds next_frontier;
          - the two batched fetches happen once for the whole step;
          - pass 2 applies activation in that recorded order.
        BFS is level-synchronous, so every entity discovered in a step shares
        ``activation = spread_factor ** current_depth`` — that uniformity is why
        per-depth batching is exact-parity. Insertion order into ``activated`` is
        byte-identical to the per-entity path (parity gate in
        ``test_spreading_batch_parity.py``); only WHEN the fetches happen changes.
        """
        next_frontier: list[tuple[int, int]] = []
        # Pass 1: expand adjacency, run the visited-guard, record discovery order.
        to_expand = [entity_id for entity_id, depth in frontier if depth < max_depth]
        adjacency = self._graph._get_adjacent_batch(to_expand, None)
        discovered: list[tuple[int, int]] = []  # (nid, current_depth) in discovery order
        for entity_id, depth in frontier:
            if depth >= max_depth:
                continue
            for neighbor in adjacency.get(entity_id, []):
                nid = neighbor["entity_id"]
                if nid in visited_entities:
                    continue
                visited_entities.add(nid)
                current_depth = depth + 1
                discovered.append((nid, current_depth))
                next_frontier.append((nid, current_depth))

        if not discovered:
            return next_frontier

        # Batched fetch #1: all discovered entity rows in one query.
        entity_rows = self._storage.get_entities_by_ids([nid for nid, _ in discovered])
        # Batched fetch #2: memories for every discovered entity NAME in one round-trip.
        names_in_order = [entity_rows[nid]["name"] for nid, _ in discovered if nid in entity_rows]
        memories_by_name = self._find_memories_for_entities(names_in_order)

        # Pass 2: apply activation in the exact discovery order (byte-identical to the
        # per-entity path — same entities, same memories, same `max` accumulation).
        for nid, current_depth in discovered:
            entity_row = entity_rows.get(nid)
            if not entity_row:
                continue
            activation = spread_factor**current_depth
            for mid in memories_by_name.get(entity_row["name"], []):
                if mid not in seed_memory_set:
                    activated[mid] = max(activated.get(mid, 0.0), activation)

        return next_frontier

    def _spreading_bfs_step_pernode(
        self,
        frontier: list[tuple[int, int]],
        visited_entities: set[int],
        activated: dict[int, float],
        seed_memory_set: set[int],
        spread_factor: float,
        max_depth: int,
    ) -> list[tuple[int, int]]:
        """Legacy per-entity BFS step — retained only as the parity baseline (v5.102.0).

        Exact copy of the pre-v5.102 step: adjacency batched (v5.99) but each
        activated entity fetched one-at-a-time via ``_spreading_apply_activation``
        (``get_entity_by_id`` + ``_find_memories_for_entity``). The v5.102 batched
        step MUST produce a byte-identical ``activated`` dict; the parity test
        asserts it. Not used in production.
        """
        next_frontier: list[tuple[int, int]] = []
        to_expand = [entity_id for entity_id, depth in frontier if depth < max_depth]
        adjacency = self._graph._get_adjacent_batch(to_expand, None)
        for entity_id, depth in frontier:
            if depth >= max_depth:
                continue
            for neighbor in adjacency.get(entity_id, []):
                nid = neighbor["entity_id"]
                if nid in visited_entities:
                    continue
                visited_entities.add(nid)
                current_depth = depth + 1
                activation = spread_factor**current_depth
                self._spreading_apply_activation(nid, activation, activated, seed_memory_set)
                next_frontier.append((nid, current_depth))
        return next_frontier

    def _spreading_apply_activation(
        self,
        entity_id: int,
        activation: float,
        activated: dict[int, float],
        seed_memory_set: set[int],
    ) -> None:
        """Update activated scores for memories linked to entity_id (per-entity path).

        Retained as the building block of the ``_spreading_bfs_step_pernode`` parity
        baseline. The production step (``_spreading_bfs_step``) batches these fetches.
        """
        entity_row = self._storage.get_entity_by_id(entity_id)
        if not entity_row:
            return
        for mid in self._find_memories_for_entity(entity_row["name"]):
            if mid not in seed_memory_set:
                activated[mid] = max(activated.get(mid, 0.0), activation)

    # -- d1. Plugin pipeline --

    @observe(tier="hot", metric="retrieval.get_pipeline")
    def _get_pipeline(self):
        """Return the plugin pipeline, initialising it lazily on first call."""
        if self._pipeline is None:
            from yadgar.backend.retrieval.pipeline import RetrievalPipeline  # noqa: PLC0415

            self._pipeline = RetrievalPipeline.from_retriever(self)
        return self._pipeline

    @observe(tier="boundary", metric="retrieval.recall_via_pipeline")
    def recall_via_pipeline(
        self,
        query: str,
        max_results: int = 5,
        min_heat: float = 0.1,
        profile: str = "balanced",
        stage_overrides: dict | None = None,
    ) -> list[dict]:
        """Run recall through the v5.31.0 plugin pipeline.

        Functionally identical to ``recall()`` with profile="balanced" (the default).
        Exposes per-stage timing via ``RetrievalState.stage_stats`` (available via
        ``recall_compare()``).

        Args:
            query: Search query.
            max_results: Maximum results to return.
            min_heat: Minimum heat threshold.
            profile: Profile name ("fast", "balanced", "full", "debug").
            stage_overrides: Per-call disable map, e.g. {"nli": False}.

        Returns:
            List of memory dicts (same format as ``recall()``).
        """
        from collections import defaultdict  # noqa: PLC0415

        from yadgar.backend.retrieval.state import RetrievalState  # noqa: PLC0415

        state = RetrievalState(
            query=query,
            max_results=max_results,
            min_heat=min_heat,
            profile=profile,
            stage_overrides=stage_overrides or {},
            scores=defaultdict(
                lambda: {
                    "vector": 0.0,
                    "fts": 0.0,
                    "ppr": 0.0,
                    "spread": 0.0,
                    "temporal": 0.0,
                }
            ),
        )
        pipeline = self._get_pipeline()
        state = pipeline.run(state)
        return state.result_memories

    # -- d2. Unified Recall (legacy monolithic implementation — kept for compat) --

    @observe(tier="stage", metric="retrieval.resolve_query_and_candidate_k")
    def _resolve_query_and_candidate_k(
        self,
        query: str,
        profile: dict,
        profile_signals: set,
        max_results: int,
    ) -> tuple[dict, object, bool, list, int]:
        """Resolve query analysis, enabled signals, open-domain state, and candidate_k.

        Returns:
            (query_analysis, enabled_signals, open_domain_mode, open_domain_subqueries, candidate_k)

        v5.51.0: extracted from recall() to keep fn_loc under the I13 HARD cap (150).
        When profile has skip_query_analysis=True (fast profile), analysis and routing
        intersection are bypassed entirely to avoid per-call overhead and the empty-signals
        trap when QUERY_ROUTING_ENABLED=True.
        """
        skip_query_analysis = profile.get("skip_query_analysis", False)

        if skip_query_analysis:
            query_analysis: dict = {}
            enabled_signals = profile_signals
            open_domain_mode = False
            open_domain_subqueries: list = []
        else:
            query_analysis = analyze_query(query, self._settings)
            if self._settings.QUERY_ROUTING_ENABLED:
                enabled_signals = set(query_analysis.get("enabled_signals", [])) & profile_signals
            else:
                enabled_signals = None if len(profile_signals) >= 4 else profile_signals
            open_domain_mode = query_analysis.get("is_open_domain_like", False)
            open_domain_subqueries = (
                _build_open_domain_subqueries(query, query_analysis) if open_domain_mode else []
            )

        if profile.get("use_fast_candidate_multiplier", False):
            candidate_k = max_results * getattr(
                self._settings, "FAST_PROFILE_CANDIDATE_MULTIPLIER", 3
            )
        else:
            candidate_k = max_results * self._settings.CANDIDATE_POOL_MULTIPLIER
            if open_domain_mode:
                candidate_k = int(
                    candidate_k * getattr(self._settings, "OPEN_DOMAIN_CANDIDATE_MULTIPLIER", 1.5)
                )

        return (
            query_analysis,
            enabled_signals,
            open_domain_mode,
            open_domain_subqueries,
            candidate_k,
        )

    @observe(tier="stage", metric="retrieval.recall.graph_temporal_scores", span=False)
    def _collect_graph_temporal_scores(
        self,
        fts_params: FTSParams,
        scores: dict,
        vector_memory_ids: list,
        deadline: float | None,
    ) -> tuple[float, bool]:
        """Run PPR + spreading + temporal collections with ADR-0077 deadline checks.

        Extracted from recall() (I13 cap). Reuses the FTSParams bundle recall()
        already built (query / enabled_signals / candidate_k / min_heat /
        candidate_k / min_heat). Returns (w_temporal, deadline_hit); once the deadline is
        exceeded, remaining collections are skipped and deadline_hit=True tells
        recall() to also skip the rerank pipeline.
        """
        p = fts_params
        self._collect_ppr_scores(p.query, scores, p.enabled_signals, p.candidate_k)
        if _deadline_passed(deadline):
            return 0.0, True

        self._collect_spreading_scores(scores, p.enabled_signals, vector_memory_ids)
        if _deadline_passed(deadline):
            return 0.0, True

        w_temporal = self._collect_temporal_scores(p.query, scores, p.min_heat, p.candidate_k)
        return w_temporal, _deadline_passed(deadline)

    @observe(tier="boundary", metric="retrieval.recall")
    def recall(  # noqa: PLR0913
        self,
        query: str,
        max_results: int = 5,
        min_heat: float = 0.1,
        profile: str | None = None,
        deadline: float | None = None,
        project_id: str | None = None,
    ) -> list[dict]:
        """Combine retrieval signals via Weighted Reciprocal Rank Fusion (WRRF).

        Each signal produces a ranked list of memory IDs. Scores are fused as:
          WRRF_score(d) = Σ_i [ w_i / (k + rank_i(d)) ]
        where k is the RRF constant and w_i are per-signal weights from settings.
        An optional heuristic reranker refines the final ordering.

        Args:
            deadline: Monotonic deadline (time.monotonic() epoch) derived from the
                client's deadline_ms budget (ADR-0077). Checked between signal-
                collection stages: once exceeded, remaining collections and the
                rerank pipeline are skipped and the partial fusion result is
                returned. None (default) = no deadline, full pipeline.
            project_id: Car C7 (0047 §5 C7) — the caller's resolved project. Built
                into a stage-1 ``WHERE`` predicate (``project_id = $p OR 'global'
                IN tags``) and pushed into the FTS and vector arms, so those
                signals spend their ``candidate_k`` on in-scope rows instead of
                being post-filtered afterwards.

                SCOPE LIMIT, STATED RATHER THAN IMPLIED: the predicate covers the
                two SQL-driven signals. The PPR graph walk and spreading
                activation traverse EDGES and return memory ids the clause never
                saw, so they can still surface out-of-scope rows.
                ``MemoryProvider`` therefore keeps a residual row guard
                (``is_project_eligible``) for exactly those. That guard is not
                the retired post-filter in new clothes — it agrees with the SQL
                arm by construction (same two arms, same treatment of unstamped
                rows) rather than carrying a wider sentinel set.
        """
        # P11: set dynamic span attributes on the active retrieval.recall span.
        try:
            from opentelemetry import trace as _otel_trace  # noqa: PLC0415

            _span = _otel_trace.get_current_span()
            if _span and _span.is_recording():
                _span.set_attribute("query_len", len(query))
                _span.set_attribute("max_results", max_results)
                _span.set_attribute("profile", profile or "")
        except Exception:
            pass

        # Determine active retrieval profile.
        # Caller-supplied `profile` kwarg overrides RETRIEVAL_PROFILE setting.
        # Hook handlers pass profile="fast" to skip CE/NLI/MP for lightweight context injection.
        profile_name = profile if profile is not None else self._settings.RETRIEVAL_PROFILE
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

        # v5.51.0: query analysis + candidate_k resolution extracted to helper (I13 HARD cap).
        query_analysis, enabled_signals, open_domain_mode, open_domain_subqueries, candidate_k = (
            self._resolve_query_and_candidate_k(query, profile, profile_signals, max_results)
        )

        # ADR-0077: between-stage deadline checks. Once the client's budget is
        # exceeded, remaining signal collections are skipped (partial fusion of
        # whatever was gathered) — a hook client that already gave up at 2.0s
        # must not keep the backend collecting.
        vector_memory_ids: list = []
        query_embedding = None
        w_temporal = 0.0
        # Car C7: ONE predicate, built once, pushed into both SQL signals.
        # ``page_types=False`` — the ``memory`` table has no ``page_type`` column.
        _scope_sql, _scope_params = build_recall_scope_clause(project_id, page_types=False)
        fts_params = FTSParams(
            query=query,
            enabled_signals=enabled_signals,
            open_domain_subqueries=open_domain_subqueries,
            open_domain_mode=open_domain_mode,
            candidate_k=candidate_k,
            min_heat=min_heat,
            scope_sql=_scope_sql,
            scope_params=_scope_params,
        )
        _deadline_hit = _deadline_passed(deadline)

        # 1 + 1b + 1c. FTS, entity-FTS, and COMET expansion scores
        if not _deadline_hit:
            self._collect_fts_scores(scores, fts_params)
            _deadline_hit = _deadline_passed(deadline)

        # 2. Vector similarity via SurrealDB KNN
        if not _deadline_hit:
            vector_memory_ids, query_embedding = self._collect_vector_scores(
                query,
                scores,
                enabled_signals,
                open_domain_subqueries,
                candidate_k,
                min_heat,
                (_scope_sql, _scope_params),
            )
            _deadline_hit = _deadline_passed(deadline)

        # 3 + 4 + 5. PPR graph, spreading activation, temporal boost
        if not _deadline_hit:
            w_temporal, _deadline_hit = self._collect_graph_temporal_scores(
                fts_params, scores, vector_memory_ids, deadline
            )

        # Fusion: confidence gating + WRRF/convex combination
        fused, fused_scores = self._fuse_scores(scores, w_temporal, open_domain_mode)

        # Build result memories + CE diversity injection
        result_memories, seen_ids, use_cross_encoder = self._build_initial_results(
            fused, fused_scores, scores, profile, open_domain_mode, max_results, min_heat
        )

        # ADR-0077: deadline exceeded → skip the rerank pipeline, return the
        # partial fusion result (embedding-free per the retriever contract).
        if _deadline_hit:
            logger.debug("recall deadline exceeded — returning partial result (rerank skipped)")
            return self._strip_embeddings(result_memories[:max_results])

        # Post-fusion reranking pipeline
        ctx = RerankContext(
            query=query,
            query_analysis=query_analysis,
            query_embedding=query_embedding,
            profile=profile,
            profile_name=profile_name,
            open_domain_mode=open_domain_mode,
            use_cross_encoder=use_cross_encoder,
            max_results=max_results,
            project_id=project_id,
        )
        return self._apply_rerank_pipeline(result_memories, seen_ids, ctx)
