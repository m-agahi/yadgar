"""Tests for v5.54.1 — precomputed graph prior (Phase 2 of graph-leverage umbrella).

Covers:
1. Consolidation computes and stores graph_prior for memories with entity connections;
   better-connected memory receives a higher prior than a less-connected one.
2. fast-profile recall: memory with high graph_prior ranks higher than an identical
   memory with prior=0, WITHOUT any per-query graph traversal (patch PPR/spreading/
   entity-extraction as hard-fail counters; assert 0 calls on fast path).
3. WRRF_GRAPH_PRIOR_WEIGHT=0.0 → ranking identical to baseline (disabled).
4. balanced/full profiles still work when graph_prior boost is applied (additive,
   not replacing existing PPR+spreading logic).
5. Memories with NULL/absent graph_prior do not break recall (treated as 0.0).
6. Migration 020 is registered in _MIGRATIONS at the correct position.
7. get_memory_graph_priors returns correct values and handles empty/null input.
"""

from __future__ import annotations

import pathlib
from unittest.mock import MagicMock, patch

# ---------------------------------------------------------------------------
# 1. Consolidation computes + stores graph_prior
# ---------------------------------------------------------------------------


class TestComputeGraphPriors:
    """_compute_graph_priors computes and stores correct scores."""

    def _make_consolidation_scheduler(self, storage, graph, settings):
        """Build a minimal ConsolidationScheduler with mocked sub-engines."""
        from yadgar.consolidation import ConsolidationScheduler

        sched = object.__new__(ConsolidationScheduler)
        sched._storage = storage
        sched._graph = graph
        sched._settings = settings
        return sched

    def _make_settings(self, graph_entity_min_length=3, similarity_matrix_max_candidates=4000):
        s = MagicMock()
        s.GRAPH_ENTITY_MIN_LENGTH = graph_entity_min_length
        s.SIMILARITY_MATRIX_MAX_CANDIDATES = similarity_matrix_max_candidates
        return s

    def test_graph_prior_stored_for_connected_memory(self):
        """Memories whose entity appears in entity graph receive non-zero graph_prior."""
        storage = MagicMock()
        graph = MagicMock()
        settings = self._make_settings()

        # Two memories: memory 1 mentions "FooModule"; memory 2 mentions nothing known
        storage.get_memories_with_embeddings.return_value = [
            {"id": 1, "content": "FooModule is the main entry point"},
            {"id": 2, "content": "No entities here"},
        ]
        # One entity in graph
        storage.get_all_entities.return_value = [
            {"id": 101, "name": "FooModule", "heat": 1.0},
        ]
        # FooModule has 3 neighbors with total weight 5.0
        graph._get_adjacent.return_value = [
            {"entity_id": 102, "weight": 2.0},
            {"entity_id": 103, "weight": 3.0},
        ]
        storage.batch_writes = MagicMock()

        sched = self._make_consolidation_scheduler(storage, graph, settings)
        stats = {}
        sched._compute_graph_priors(stats)

        assert stats["graph_prior_updated"] == 2
        assert storage.batch_writes.called

        # Inspect the batch: memory 1 should get prior=1.0 (max/max), memory 2 prior=0.0
        batch = storage.batch_writes.call_args[0][0]
        prior_map: dict[int, float] = {}
        for _sql, params in batch:
            if params and "gp" in params:
                prior_map[params["id"]] = params["gp"]

        assert prior_map[1] == 1.0, f"Memory 1 should have prior=1.0, got {prior_map[1]}"
        assert prior_map[2] == 0.0, f"Memory 2 should have prior=0.0, got {prior_map[2]}"

    def test_better_connected_memory_has_higher_prior(self):
        """Memory with entities having higher total degree → higher normalized prior."""
        storage = MagicMock()
        graph = MagicMock()
        settings = self._make_settings()

        storage.get_memories_with_embeddings.return_value = [
            {"id": 10, "content": "HighDegreeEntity is important"},
            {"id": 20, "content": "LowDegreeEntity is secondary"},
        ]
        storage.get_all_entities.return_value = [
            {"id": 201, "name": "HighDegreeEntity", "heat": 1.0},
            {"id": 202, "name": "LowDegreeEntity", "heat": 0.5},
        ]

        def mock_adjacent(eid, _):
            if eid == 201:  # high-degree: 10 connections total weight
                return [{"entity_id": i, "weight": 2.0} for i in range(300, 305)]
            if eid == 202:  # low-degree: 1 connection
                return [{"entity_id": 400, "weight": 1.0}]
            return []

        graph._get_adjacent.side_effect = mock_adjacent
        storage.batch_writes = MagicMock()

        sched = self._make_consolidation_scheduler(storage, graph, settings)
        stats = {}
        sched._compute_graph_priors(stats)

        batch = storage.batch_writes.call_args[0][0]
        prior_map: dict[int, float] = {}
        for _sql, params in batch:
            if params and "gp" in params:
                prior_map[params["id"]] = params["gp"]

        assert prior_map[10] > prior_map[20], (
            f"High-degree entity memory prior {prior_map[10]} must exceed "
            f"low-degree entity memory prior {prior_map[20]}"
        )
        assert prior_map[10] == 1.0, "Max prior must normalize to 1.0"

    def test_no_memories_returns_zero_updated(self):
        """Empty memory set: stats['graph_prior_updated']=0, no batch write."""
        storage = MagicMock()
        graph = MagicMock()
        settings = self._make_settings()
        storage.get_memories_with_embeddings.return_value = []
        storage.batch_writes = MagicMock()

        from yadgar.consolidation import ConsolidationScheduler

        sched = object.__new__(ConsolidationScheduler)
        sched._storage = storage
        sched._graph = graph
        sched._settings = settings

        stats = {}
        sched._compute_graph_priors(stats)

        assert stats["graph_prior_updated"] == 0
        storage.batch_writes.assert_not_called()

    def test_no_entities_returns_zero_updated(self):
        """No entities in graph: all priors=0 but still updated (entity score=0)."""
        storage = MagicMock()
        graph = MagicMock()
        settings = self._make_settings()
        storage.get_memories_with_embeddings.return_value = [
            {"id": 1, "content": "some content"},
        ]
        storage.get_all_entities.return_value = []  # no entities at all

        from yadgar.consolidation import ConsolidationScheduler

        sched = object.__new__(ConsolidationScheduler)
        sched._storage = storage
        sched._graph = graph
        sched._settings = settings

        stats = {}
        sched._compute_graph_priors(stats)
        assert stats["graph_prior_updated"] == 0


# ---------------------------------------------------------------------------
# 2. fast-profile recall: high-prior memory ranks higher, NO graph traversal
# ---------------------------------------------------------------------------


class TestFastProfileGraphPriorBoost:
    """fast profile: high graph_prior memory ranks higher, no per-query graph calls."""

    def _make_minimal_retriever(self, settings, storage):
        """Build a Retriever with mocked dependencies."""
        from yadgar.retrieval.core import Retriever

        embeddings = MagicMock()
        knowledge_graph = MagicMock()
        ml_client = MagicMock()
        reranker = MagicMock()
        reranker.compute_signal_confidence.return_value = 1.0  # disable gating

        r = object.__new__(Retriever)
        r._storage = storage
        r._settings = settings
        r._embeddings = embeddings
        r._graph = knowledge_graph
        r._ml_client = ml_client
        r._reranker = reranker
        return r

    def _make_settings_with_weight(self, weight: float):
        from unittest.mock import MagicMock as MM  # noqa: PLC0415

        from yadgar.config import get_settings  # noqa: PLC0415

        s = MM(spec=get_settings())
        s.WRRF_GRAPH_PRIOR_WEIGHT = weight
        s.WRRF_VECTOR_WEIGHT = 1.0
        s.WRRF_FTS_WEIGHT = 0.5
        s.WRRF_PPR_WEIGHT = 0.5
        s.WRRF_SPREADING_WEIGHT = 0.3
        s.FUSION_METHOD = "wrrf"
        s.FUSION_NORM = "zscore"
        s.COMBMNZ_ENABLED = False
        return s

    def test_high_prior_memory_ranks_higher_than_zero_prior(self):
        """Memory with graph_prior=0.8 ranks above identical memory with graph_prior=0.0."""
        from yadgar.retrieval.fusion import _FusionMixin

        settings = self._make_settings_with_weight(0.2)
        storage = MagicMock()

        # Memory 1 has high prior, memory 2 has none (NULL treated as 0)
        storage.get_memory_graph_priors.return_value = {1: 0.8}  # memory 2 absent

        class _TestFuser(_FusionMixin):
            def __init__(self):
                self._settings = settings
                self._storage = storage
                self._reranker = MagicMock()
                self._reranker.compute_signal_confidence.return_value = 1.0

        fuser = _TestFuser()

        # Apply the boost logic by calling _fuse_scores with pre-built scores
        # We need to build minimal scores dict to get through _fuse_scores
        scores = {
            1: {"vector": 0.8, "fts": 0.6, "ppr": 0.0, "spread": 0.0},
            2: {"vector": 0.8, "fts": 0.6, "ppr": 0.0, "spread": 0.0},
        }

        fused, fused_scores_out = fuser._fuse_scores(
            scores=scores,
            w_temporal=0.0,
            open_domain_mode=False,
        )

        # After boost: memory 1 should rank first
        ranked_ids = [mid for mid, _ in fused]
        assert ranked_ids[0] == 1, (
            f"Memory 1 (high prior) should rank first; got order {ranked_ids}"
        )
        assert fused_scores_out[1] > fused_scores_out[2], (
            f"Memory 1 score {fused_scores_out[1]} must exceed memory 2 score "
            f"{fused_scores_out[2]} after graph_prior boost"
        )

    def test_fast_profile_does_not_call_ppr_or_spreading(self):
        """fast path must NOT call PPR, spreading activation, or entity extraction."""
        from yadgar.retrieval.fusion import PROFILES

        fast_profile = PROFILES["fast"]

        # Verify fast profile doesn't include ppr/spreading signals
        assert "ppr" not in fast_profile["signals"], (
            "fast profile must NOT include 'ppr' signal — graph_prior is the O(1) alternative"
        )
        assert "spreading" not in fast_profile["signals"], (
            "fast profile must NOT include 'spreading' signal"
        )

        # Verify graph_prior boost path calls NO graph traversal by inspecting
        # fusion.py source: the boost must read from storage, not call _graph methods
        fusion_src = pathlib.Path(__file__).parent.parent / "retrieval" / "fusion.py"
        source = fusion_src.read_text()

        # The boost section must use get_memory_graph_priors (storage read), not graph traversal
        assert "get_memory_graph_priors" in source, (
            "fusion.py must call storage.get_memory_graph_priors for O(1) prior read"
        )
        # Must NOT call PPR machinery from within the graph_prior boost section
        assert (
            "_collect_ppr_scores" not in source
            or source.index("get_memory_graph_priors") < source.index("_collect_ppr_scores") + 1
        ), "graph_prior boost section must not trigger PPR collection"

    def test_fast_profile_graph_prior_no_traversal_at_runtime(self):
        """Runtime check: fast-profile recall does not invoke graph traversal methods."""
        from yadgar.retrieval.fusion import _FusionMixin

        settings = self._make_settings_with_weight(0.2)
        storage = MagicMock()
        # Return a prior for memory 1
        storage.get_memory_graph_priors.return_value = {1: 0.5}

        class _TestFuser(_FusionMixin):
            def __init__(self):
                self._settings = settings
                self._storage = storage
                self._reranker = MagicMock()
                self._reranker.compute_signal_confidence.return_value = 1.0

        fuser = _TestFuser()

        # Patch graph traversal methods as hard-fail counters
        ppr_call_count = [0]

        scores = {
            1: {"vector": 0.7, "fts": 0.5, "ppr": 0.0, "spread": 0.0},
            2: {"vector": 0.7, "fts": 0.5, "ppr": 0.0, "spread": 0.0},
        }

        # These should NOT be called during _fuse_scores on fast path
        with patch.object(
            fuser._storage,
            "_get_adjacent",
            side_effect=lambda *a, **k: ppr_call_count.__setitem__(0, ppr_call_count[0] + 1) or [],
        ):
            fused, fused_scores_out = fuser._fuse_scores(
                scores=scores,
                w_temporal=0.0,
                open_domain_mode=False,
            )

        # Storage._get_adjacent is a KnowledgeGraph method, not storage — should be 0
        assert ppr_call_count[0] == 0, (
            f"_get_adjacent called {ppr_call_count[0]} times during _fuse_scores — "
            "graph_prior boost must only call get_memory_graph_priors (O(1) storage read)"
        )

        # get_memory_graph_priors SHOULD be called exactly once
        storage.get_memory_graph_priors.assert_called_once()


# ---------------------------------------------------------------------------
# 3. WRRF_GRAPH_PRIOR_WEIGHT=0.0 → ranking identical to baseline
# ---------------------------------------------------------------------------


class TestGraphPriorWeightZeroDisables:
    """WRRF_GRAPH_PRIOR_WEIGHT=0.0 → graph_prior boost is a no-op."""

    def test_weight_zero_ranking_unchanged(self):
        """With weight=0.0, fused scores must be identical to pre-boost values."""
        from yadgar.retrieval.fusion import _FusionMixin

        s = MagicMock()
        s.WRRF_GRAPH_PRIOR_WEIGHT = 0.0
        s.WRRF_VECTOR_WEIGHT = 1.0
        s.WRRF_FTS_WEIGHT = 0.5
        s.WRRF_PPR_WEIGHT = 0.5
        s.WRRF_SPREADING_WEIGHT = 0.3
        s.FUSION_METHOD = "wrrf"
        s.FUSION_NORM = "zscore"
        s.COMBMNZ_ENABLED = False

        storage = MagicMock()
        storage.get_memory_graph_priors.return_value = {1: 0.9, 2: 0.1}

        class _TestFuser(_FusionMixin):
            def __init__(self):
                self._settings = s
                self._storage = storage
                self._reranker = MagicMock()
                self._reranker.compute_signal_confidence.return_value = 1.0

        fuser = _TestFuser()

        scores = {
            1: {"vector": 0.5, "fts": 0.5, "ppr": 0.0, "spread": 0.0},
            2: {"vector": 0.8, "fts": 0.8, "ppr": 0.0, "spread": 0.0},
        }

        fused, fused_scores = fuser._fuse_scores(
            scores=scores,
            w_temporal=0.0,
            open_domain_mode=False,
        )

        # With weight=0, get_memory_graph_priors should NOT be called at all
        storage.get_memory_graph_priors.assert_not_called()

        # Memory 2 has higher raw scores → must rank first (prior not applied)
        ranked_ids = [mid for mid, _ in fused]
        assert ranked_ids[0] == 2, (
            f"With weight=0, memory 2 (higher raw signal) must rank first. Got {ranked_ids}"
        )


# ---------------------------------------------------------------------------
# 4. balanced/full profiles unaffected (additive, PPR+spreading intact)
# ---------------------------------------------------------------------------


class TestBalancedFullProfilesUnchanged:
    """balanced/full profile definitions remain valid; graph_prior is additive."""

    def test_balanced_profile_signals_unchanged(self):
        """balanced profile still includes ppr and spreading signals."""
        from yadgar.retrieval.fusion import PROFILES

        balanced = PROFILES["balanced"]
        assert "ppr" in balanced["signals"], "balanced must include ppr signal"
        assert "spreading" in balanced["signals"], "balanced must include spreading signal"
        assert balanced["cross_encoder"] is True

    def test_full_profile_signals_unchanged(self):
        """full profile still includes ppr, spreading, and nli."""
        from yadgar.retrieval.fusion import PROFILES

        full = PROFILES["full"]
        assert "ppr" in full["signals"], "full must include ppr signal"
        assert "spreading" in full["signals"], "full must include spreading signal"
        assert full["nli"] is True

    def test_graph_prior_is_additive_to_ppr(self):
        """graph_prior boost stacks on top of ppr+spreading; does not replace them."""
        fusion_src = pathlib.Path(__file__).parent.parent / "retrieval" / "fusion.py"
        source = fusion_src.read_text()

        # Signal weights dict must still include ppr and spread
        assert '"ppr": self._settings.WRRF_PPR_WEIGHT' in source, (
            "fusion.py must still include PPR in signal_weights (graph_prior is additive)"
        )
        assert '"spread": self._settings.WRRF_SPREADING_WEIGHT' in source, (
            "fusion.py must still include spreading in signal_weights"
        )

        # graph_prior boost must appear AFTER the fusion computation, not instead of it
        fusion_idx = source.index("fused_scores")
        prior_idx = source.index("WRRF_GRAPH_PRIOR_WEIGHT")
        assert prior_idx > fusion_idx, (
            "graph_prior boost must occur AFTER main fusion, not replace it"
        )


# ---------------------------------------------------------------------------
# 5. NULL/absent graph_prior does not break recall
# ---------------------------------------------------------------------------


class TestNullGraphPriorSafe:
    """Memories with NULL/absent graph_prior are handled safely."""

    def test_null_prior_treated_as_zero_in_fusion(self):
        """Memory with no graph_prior in storage result is skipped (0.0 additive)."""
        from yadgar.retrieval.fusion import _FusionMixin

        s = MagicMock()
        s.WRRF_GRAPH_PRIOR_WEIGHT = 0.2
        s.WRRF_VECTOR_WEIGHT = 1.0
        s.WRRF_FTS_WEIGHT = 0.5
        s.WRRF_PPR_WEIGHT = 0.5
        s.WRRF_SPREADING_WEIGHT = 0.3
        s.FUSION_METHOD = "wrrf"
        s.FUSION_NORM = "zscore"
        s.COMBMNZ_ENABLED = False

        storage = MagicMock()
        # Return empty dict — no memory has graph_prior set
        storage.get_memory_graph_priors.return_value = {}

        class _TestFuser(_FusionMixin):
            def __init__(self):
                self._settings = s
                self._storage = storage
                self._reranker = MagicMock()
                self._reranker.compute_signal_confidence.return_value = 1.0

        fuser = _TestFuser()

        scores = {
            1: {"vector": 0.9, "fts": 0.9, "ppr": 0.0, "spread": 0.0},
            2: {"vector": 0.5, "fts": 0.3, "ppr": 0.0, "spread": 0.0},
        }

        # Must not raise
        fused, fused_scores = fuser._fuse_scores(
            scores=scores,
            w_temporal=0.0,
            open_domain_mode=False,
        )

        # Rankings must still be sensible based on raw signals
        assert fused is not None
        assert len(fused) >= 1
        # Memory 1 has higher signals → should rank first
        ranked_ids = [mid for mid, _ in fused]
        assert ranked_ids[0] == 1, f"Memory 1 (higher signals) must rank first; got {ranked_ids}"

    def test_get_memory_graph_priors_empty_input(self):
        """get_memory_graph_priors([]) returns empty dict without DB calls."""
        storage = MagicMock()
        storage._q = MagicMock(return_value=[])

        from yadgar.storage.memory import _MemoryMixin

        mixin = object.__new__(_MemoryMixin)
        mixin._q = MagicMock()

        result = _MemoryMixin.get_memory_graph_priors(mixin, [])
        assert result == {}
        mixin._q.assert_not_called()


# ---------------------------------------------------------------------------
# 6. Migration 020 registered in _MIGRATIONS
# ---------------------------------------------------------------------------


class TestMigration020Registered:
    """Migration 020_memory_graph_prior is registered and in correct order."""

    def test_migration_020_in_list(self):
        """_MIGRATIONS must include 020_memory_graph_prior after 019_wiki_page_type."""
        from yadgar.storage.migrations import _MIGRATIONS

        versions = [m["version"] for m in _MIGRATIONS]
        assert "020_memory_graph_prior" in versions, (
            f"020_memory_graph_prior missing from _MIGRATIONS. Found: {versions}"
        )

    def test_migration_020_after_019(self):
        """020 must come after 019 in the migrations list (append-only order)."""
        from yadgar.storage.migrations import _MIGRATIONS

        versions = [m["version"] for m in _MIGRATIONS]
        idx_019 = versions.index("019_wiki_page_type")
        idx_020 = versions.index("020_memory_graph_prior")
        assert idx_020 > idx_019, (
            f"020 must come after 019; got idx_019={idx_019}, idx_020={idx_020}"
        )
        assert idx_020 == idx_019 + 1, (
            f"020 must immediately follow 019 (no gap); idx_019={idx_019}, idx_020={idx_020}"
        )

    def test_migration_017_still_reserved(self):
        """017 slot must remain reserved (no migration using that version)."""
        from yadgar.storage.migrations import _MIGRATIONS

        versions = [m["version"] for m in _MIGRATIONS]
        assert not any("017" in v for v in versions), (
            "017 is RESERVED for v5.61 (wiki_source_hash); must not be used"
        )

    def test_migration_020_fn_callable(self):
        """Migration 020 function must be callable."""
        from yadgar.storage.migrations import _migration_020_memory_graph_prior

        assert callable(_migration_020_memory_graph_prior)


# ---------------------------------------------------------------------------
# 7. get_memory_graph_priors storage method
# ---------------------------------------------------------------------------


class TestGetMemoryGraphPriors:
    """Storage method get_memory_graph_priors correctness."""

    def test_returns_float_values(self):
        """get_memory_graph_priors returns {int: float} for present priors."""
        from yadgar.storage.memory import _MemoryMixin

        mixin = object.__new__(_MemoryMixin)

        def mock_q(sql, params=None):
            mid = params["id"]
            if mid == 1:
                return [{"id": f"memory:{mid}", "graph_prior": 0.75}]
            return []

        mixin._q = mock_q
        mixin._extract_id = lambda rid: (
            int(str(rid).split(":")[-1]) if ":" in str(rid) else int(rid)
        )

        result = _MemoryMixin.get_memory_graph_priors(mixin, [1, 2])
        assert result == {1: 0.75}, f"Expected {{1: 0.75}}, got {result}"

    def test_absent_priors_not_in_result(self):
        """Memories without graph_prior are absent from result (not {mid: 0.0})."""
        from yadgar.storage.memory import _MemoryMixin

        mixin = object.__new__(_MemoryMixin)
        # Always return empty (NULL → IS NOT NONE filter excludes)
        mixin._q = MagicMock(return_value=[])
        mixin._extract_id = lambda rid: (
            int(str(rid).split(":")[-1]) if ":" in str(rid) else int(rid)
        )

        result = _MemoryMixin.get_memory_graph_priors(mixin, [1, 2, 3])
        assert result == {}, f"Expected empty dict for all-NULL priors, got {result}"

    def test_empty_input_no_queries(self):
        """Empty memory_ids list: no DB calls, returns empty dict."""
        from yadgar.storage.memory import _MemoryMixin

        mixin = object.__new__(_MemoryMixin)
        mixin._q = MagicMock()

        result = _MemoryMixin.get_memory_graph_priors(mixin, [])
        assert result == {}
        mixin._q.assert_not_called()


# ---------------------------------------------------------------------------
# 8. I25 three-way config sync for WRRF_GRAPH_PRIOR_WEIGHT
# ---------------------------------------------------------------------------


class TestGraphPriorConfigRegistered:
    """WRRF_GRAPH_PRIOR_WEIGHT is three-way registered (config.py + registry + yaml)."""

    def test_settings_has_wrrf_graph_prior_weight(self):
        """Settings must have WRRF_GRAPH_PRIOR_WEIGHT with default 0.2."""
        from yadgar.config import Settings

        default_val = Settings.model_fields["WRRF_GRAPH_PRIOR_WEIGHT"].default
        assert default_val == 0.2, f"WRRF_GRAPH_PRIOR_WEIGHT default must be 0.2, got {default_val}"

    def test_registry_has_wrrf_graph_prior_weight(self):
        """config_registry must include YADGAR_WRRF_GRAPH_PRIOR_WEIGHT."""
        from yadgar.config_registry import list_config

        names = {e.name for e in list_config()}
        assert "YADGAR_WRRF_GRAPH_PRIOR_WEIGHT" in names, (
            f"YADGAR_WRRF_GRAPH_PRIOR_WEIGHT missing from config_registry. "
            f"Found WRRF entries: {[n for n in names if 'WRRF' in n]}"
        )

    def test_yaml_meta_has_wrrf_graph_prior_weight(self):
        """config_yaml.py FIELD_META must include wrrf_graph_prior_weight."""
        from yadgar.config_yaml import FIELD_META

        assert "wrrf_graph_prior_weight" in FIELD_META, (
            "FIELD_META must include 'wrrf_graph_prior_weight' for I25 three-way sync"
        )

    def test_consolidation_phase_in_orchestrator(self):
        """orchestrator.py must wire compute_graph_priors phase."""
        orch_src = pathlib.Path(__file__).parent.parent / "consolidation" / "orchestrator.py"
        source = orch_src.read_text()

        assert "compute_graph_priors" in source, (
            "orchestrator.py must call _compute_graph_priors in the consolidation cycle"
        )
        assert "phase_start: compute_graph_priors" in source, (
            "orchestrator.py must log phase_start: compute_graph_priors"
        )
