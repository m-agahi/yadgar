"""Tests for v5.54.2 — precomputed co-recall (cofire) prior (Phase 3, transition edge activation).

Covers:
1. Consolidation computes and stores cofire_prior from memory_transition table;
   more-co-recalled memory receives higher prior.
2. fast-profile recall: memory with high cofire_prior ranks above identical memory
   with prior=0, WITHOUT query-time traversal (assert transition table methods NOT
   called inside _fuse_scores — only O(1) get_memory_cofire_priors called).
3. WRRF_COFIRE_PRIOR_WEIGHT=0.0 → disabled (storage method not called, ranking unchanged).
4. NULL cofire_prior → no crash, today's ranking unaffected.
5. Both graph_prior (5.54.1) and cofire_prior boosts coexist (additive, neither replaces the other).
6. Migration 021 registered in _MIGRATIONS at correct position.
7. get_memory_cofire_priors storage method correctness.
8. I25 three-way config sync for WRRF_COFIRE_PRIOR_WEIGHT.
"""

from __future__ import annotations

import pathlib
from unittest.mock import MagicMock, patch

# ---------------------------------------------------------------------------
# 1. Consolidation computes + stores cofire_prior
# ---------------------------------------------------------------------------


class TestComputeCofirePriors:
    """_compute_cofire_priors computes and stores correct co-recall scores."""

    def _make_consolidation_scheduler(self, storage, settings):
        from yadgar.consolidation import ConsolidationScheduler

        sched = object.__new__(ConsolidationScheduler)
        sched._storage = storage
        sched._settings = settings
        return sched

    def _make_settings(self, similarity_matrix_max_candidates=4000):
        s = MagicMock()
        s.SIMILARITY_MATRIX_MAX_CANDIDATES = similarity_matrix_max_candidates
        return s

    def test_cofire_prior_stored_for_co_recalled_memory(self):
        """Memory that appears in transitions receives non-zero cofire_prior."""
        storage = MagicMock()
        settings = self._make_settings()

        # Two memories; memory 1 appears in 3 transitions, memory 2 in 0
        storage.get_memories_with_embeddings.return_value = [
            {"id": 1, "content": "alpha"},
            {"id": 2, "content": "beta"},
        ]
        # Transitions: memory 1 co-recalled with memory 3 (count=2) and memory 4 (count=1)
        storage.get_all_transitions.return_value = [
            {"from_memory_id": 1, "to_memory_id": 3, "count": 2},
            {"from_memory_id": 4, "to_memory_id": 1, "count": 1},
        ]
        storage.batch_writes = MagicMock()

        sched = self._make_consolidation_scheduler(storage, settings)
        stats = {}
        sched._compute_cofire_priors(stats)

        assert stats["cofire_prior_updated"] == 2
        assert storage.batch_writes.called

        batch = storage.batch_writes.call_args[0][0]
        prior_map: dict[int, float] = {}
        for _sql, params in batch:
            if params and "cp" in params:
                prior_map[params["id"]] = params["cp"]

        # Memory 1: sum of counts = 2+1=3; memory 2: 0 → normalized: 1.0 vs 0.0
        assert prior_map[1] == 1.0, (
            f"Memory 1 (co-recalled 3 times) should be 1.0, got {prior_map[1]}"
        )
        assert prior_map[2] == 0.0, (
            f"Memory 2 (never co-recalled) should be 0.0, got {prior_map[2]}"
        )

    def test_more_co_recalled_memory_has_higher_prior(self):
        """Memory with higher total co-recall count → higher normalized prior."""
        storage = MagicMock()
        settings = self._make_settings()

        storage.get_memories_with_embeddings.return_value = [
            {"id": 10, "content": "high freq"},
            {"id": 20, "content": "low freq"},
        ]
        # Memory 10 total transitions: 5+3=8; memory 20 total: 1
        storage.get_all_transitions.return_value = [
            {"from_memory_id": 10, "to_memory_id": 99, "count": 5},
            {"from_memory_id": 99, "to_memory_id": 10, "count": 3},
            {"from_memory_id": 20, "to_memory_id": 99, "count": 1},
        ]
        storage.batch_writes = MagicMock()

        sched = self._make_consolidation_scheduler(storage, settings)
        stats = {}
        sched._compute_cofire_priors(stats)

        batch = storage.batch_writes.call_args[0][0]
        prior_map: dict[int, float] = {}
        for _sql, params in batch:
            if params and "cp" in params:
                prior_map[params["id"]] = params["cp"]

        assert prior_map[10] > prior_map[20], (
            f"High co-recall memory (prior={prior_map[10]}) must exceed "
            f"low co-recall memory (prior={prior_map[20]})"
        )
        assert prior_map[10] == 1.0, "Max prior must normalize to 1.0"

    def test_no_memories_returns_zero_updated(self):
        """Empty memory set: stats['cofire_prior_updated']=0, no batch write."""
        storage = MagicMock()
        settings = self._make_settings()
        storage.get_memories_with_embeddings.return_value = []
        storage.batch_writes = MagicMock()

        sched = self._make_consolidation_scheduler(storage, settings)
        stats = {}
        sched._compute_cofire_priors(stats)

        assert stats["cofire_prior_updated"] == 0
        storage.batch_writes.assert_not_called()

    def test_no_transitions_all_priors_zero(self):
        """No transitions in DB: all cofire_priors stored as 0.0."""
        storage = MagicMock()
        settings = self._make_settings()
        storage.get_memories_with_embeddings.return_value = [
            {"id": 1, "content": "alpha"},
        ]
        storage.get_all_transitions.return_value = []  # empty transition table
        storage.batch_writes = MagicMock()

        sched = self._make_consolidation_scheduler(storage, settings)
        stats = {}
        sched._compute_cofire_priors(stats)

        # Memories still updated (with 0.0), stats updated
        assert stats["cofire_prior_updated"] == 1
        batch = storage.batch_writes.call_args[0][0]
        _, params = batch[0]
        assert params["cp"] == 0.0

    def test_only_reads_all_transitions_once(self):
        """get_all_transitions called exactly once per consolidation cycle."""
        storage = MagicMock()
        settings = self._make_settings()
        storage.get_memories_with_embeddings.return_value = [
            {"id": i, "content": f"mem {i}"} for i in range(5)
        ]
        storage.get_all_transitions.return_value = []
        storage.batch_writes = MagicMock()

        sched = self._make_consolidation_scheduler(storage, settings)
        sched._compute_cofire_priors({})

        storage.get_all_transitions.assert_called_once()


# ---------------------------------------------------------------------------
# 2. fast-profile recall: high-prior memory ranks higher, NO traversal
# ---------------------------------------------------------------------------


class TestFastProfileCofirePriorBoost:
    """fast profile: high cofire_prior memory ranks higher, no transition-table calls."""

    def _make_settings_with_weight(self, cofire_weight: float, graph_weight: float = 0.0):
        from unittest.mock import MagicMock as MM

        from yadgar.config import get_settings

        s = MM(spec=get_settings())
        s.WRRF_COFIRE_PRIOR_WEIGHT = cofire_weight
        s.WRRF_GRAPH_PRIOR_WEIGHT = graph_weight
        s.WRRF_VECTOR_WEIGHT = 1.0
        s.WRRF_FTS_WEIGHT = 0.5
        s.WRRF_PPR_WEIGHT = 0.5
        s.WRRF_SPREADING_WEIGHT = 0.3
        s.CONFIDENCE_GATING_ENABLED = False
        s.FUSION_METHOD = "wrrf"
        s.FUSION_NORM = "zscore"
        s.COMBMNZ_ENABLED = False
        s.WRRF_K = 60
        return s

    def test_high_cofire_prior_ranks_higher(self):
        """Memory with cofire_prior=0.8 ranks above identical memory with prior=0."""
        from yadgar.retrieval.fusion import _FusionMixin

        settings = self._make_settings_with_weight(0.15)
        storage = MagicMock()
        # Memory 1 has high cofire prior; memory 2 absent (treated as 0)
        storage.get_memory_cofire_priors.return_value = {1: 0.8}
        # graph_prior disabled
        storage.get_memory_graph_priors.return_value = {}

        class _TestFuser(_FusionMixin):
            def __init__(self):
                self._settings = settings
                self._storage = storage
                self._reranker = MagicMock()
                self._reranker.compute_signal_confidence.return_value = 1.0

        fuser = _TestFuser()

        scores = {
            1: {"vector": 0.8, "fts": 0.6, "ppr": 0.0, "spread": 0.0},
            2: {"vector": 0.8, "fts": 0.6, "ppr": 0.0, "spread": 0.0},
        }

        fused, fused_scores_out = fuser._fuse_scores(
            scores=scores,
            w_temporal=0.0,
            open_domain_mode=False,
        )

        ranked_ids = [mid for mid, _ in fused]
        assert ranked_ids[0] == 1, (
            f"Memory 1 (high cofire_prior) should rank first; got {ranked_ids}"
        )
        assert fused_scores_out[1] > fused_scores_out[2], (
            f"Memory 1 score {fused_scores_out[1]} must exceed memory 2 score "
            f"{fused_scores_out[2]} after cofire_prior boost"
        )

    def test_no_transition_table_calls_in_fuse_scores(self):
        """Runtime check: _fuse_scores must NOT call any transition-table method."""
        from yadgar.retrieval.fusion import _FusionMixin

        settings = self._make_settings_with_weight(0.15)
        storage = MagicMock()
        storage.get_memory_cofire_priors.return_value = {1: 0.5}
        storage.get_memory_graph_priors.return_value = {}

        class _TestFuser(_FusionMixin):
            def __init__(self):
                self._settings = settings
                self._storage = storage
                self._reranker = MagicMock()
                self._reranker.compute_signal_confidence.return_value = 1.0

        fuser = _TestFuser()

        transition_call_count = [0]

        def _fail_if_called(*a, **k):
            transition_call_count[0] += 1
            return []

        scores = {
            1: {"vector": 0.7, "fts": 0.5, "ppr": 0.0, "spread": 0.0},
            2: {"vector": 0.7, "fts": 0.5, "ppr": 0.0, "spread": 0.0},
        }

        # Patch the three transition-reader methods on the storage mock
        with (
            patch.object(storage, "get_all_transitions", side_effect=_fail_if_called),
            patch.object(storage, "get_transitions_from", side_effect=_fail_if_called),
            patch.object(storage, "get_transition", side_effect=_fail_if_called),
        ):
            fused, _ = fuser._fuse_scores(
                scores=scores,
                w_temporal=0.0,
                open_domain_mode=False,
            )

        assert transition_call_count[0] == 0, (
            f"Transition table methods called {transition_call_count[0]} time(s) inside "
            "_fuse_scores — cofire_prior boost must only call get_memory_cofire_priors (O(1))"
        )
        # get_memory_cofire_priors SHOULD be called exactly once
        storage.get_memory_cofire_priors.assert_called_once()

    def test_fusion_source_uses_get_memory_cofire_priors(self):
        """fusion.py source must call get_memory_cofire_priors, not traverse transitions."""
        fusion_src = pathlib.Path(__file__).parent.parent / "retrieval" / "fusion.py"
        source = fusion_src.read_text()

        assert "get_memory_cofire_priors" in source, (
            "fusion.py must call storage.get_memory_cofire_priors for O(1) cofire read"
        )
        # Must NOT call any direct transition-table query in the cofire section
        # (get_all_transitions would be a traversal)
        cofire_idx = source.index("WRRF_COFIRE_PRIOR_WEIGHT")
        # Check no get_all_transitions after the cofire weight line within ~300 chars
        cofire_window = source[cofire_idx : cofire_idx + 500]
        assert "get_all_transitions" not in cofire_window, (
            "fusion.py cofire section must not call get_all_transitions (would be traversal)"
        )


# ---------------------------------------------------------------------------
# 3. WRRF_COFIRE_PRIOR_WEIGHT=0.0 → disabled (storage method not called)
# ---------------------------------------------------------------------------


class TestCofirePriorWeightZeroDisables:
    """WRRF_COFIRE_PRIOR_WEIGHT=0.0 → cofire boost is a no-op; storage not called."""

    def test_weight_zero_storage_not_called(self):
        """With weight=0.0, get_memory_cofire_priors must NOT be called."""
        from yadgar.retrieval.fusion import _FusionMixin

        s = MagicMock()
        s.WRRF_COFIRE_PRIOR_WEIGHT = 0.0
        s.WRRF_GRAPH_PRIOR_WEIGHT = 0.0
        s.WRRF_VECTOR_WEIGHT = 1.0
        s.WRRF_FTS_WEIGHT = 0.5
        s.WRRF_PPR_WEIGHT = 0.5
        s.WRRF_SPREADING_WEIGHT = 0.3
        s.CONFIDENCE_GATING_ENABLED = False
        s.FUSION_METHOD = "wrrf"
        s.FUSION_NORM = "zscore"
        s.COMBMNZ_ENABLED = False
        s.WRRF_K = 60

        storage = MagicMock()

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

        storage.get_memory_cofire_priors.assert_not_called()

        # Memory 2 has higher raw signals → must rank first (cofire not applied)
        ranked_ids = [mid for mid, _ in fused]
        assert ranked_ids[0] == 2, (
            f"With weight=0, memory 2 (higher raw signal) must rank first. Got {ranked_ids}"
        )


# ---------------------------------------------------------------------------
# 4. NULL cofire_prior → no crash, today's ranking
# ---------------------------------------------------------------------------


class TestNullCofirePriorSafe:
    """Memories with NULL/absent cofire_prior handled safely."""

    def test_null_prior_no_crash(self):
        """Empty storage result → no crash, ranking based on raw signals."""
        from yadgar.retrieval.fusion import _FusionMixin

        s = MagicMock()
        s.WRRF_COFIRE_PRIOR_WEIGHT = 0.15
        s.WRRF_GRAPH_PRIOR_WEIGHT = 0.0
        s.WRRF_VECTOR_WEIGHT = 1.0
        s.WRRF_FTS_WEIGHT = 0.5
        s.WRRF_PPR_WEIGHT = 0.5
        s.WRRF_SPREADING_WEIGHT = 0.3
        s.CONFIDENCE_GATING_ENABLED = False
        s.FUSION_METHOD = "wrrf"
        s.FUSION_NORM = "zscore"
        s.COMBMNZ_ENABLED = False
        s.WRRF_K = 60

        storage = MagicMock()
        storage.get_memory_cofire_priors.return_value = {}  # all NULL

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

        fused, fused_scores = fuser._fuse_scores(
            scores=scores,
            w_temporal=0.0,
            open_domain_mode=False,
        )

        # Must not crash; memory 1 ranks first on raw signals
        assert fused is not None
        ranked_ids = [mid for mid, _ in fused]
        assert ranked_ids[0] == 1, f"Memory 1 (higher signals) must rank first; got {ranked_ids}"

    def test_get_memory_cofire_priors_empty_input(self):
        """get_memory_cofire_priors([]) returns empty dict without DB calls."""
        from yadgar.storage.memory import _MemoryMixin

        mixin = object.__new__(_MemoryMixin)
        mixin._q = MagicMock()

        result = _MemoryMixin.get_memory_cofire_priors(mixin, [])
        assert result == {}
        mixin._q.assert_not_called()


# ---------------------------------------------------------------------------
# 5. Both graph_prior (5.54.1) and cofire_prior coexist — additive
# ---------------------------------------------------------------------------


class TestBothBoostsCoexist:
    """graph_prior (5.54.1) and cofire_prior (5.54.2) both applied; neither replaces the other."""

    def test_both_boosts_applied_to_different_memories(self):
        """With both weights >0: memory with graph_prior ranks above the one with cofire_prior
        only if graph_prior boost is bigger; they coexist without overwriting."""
        from yadgar.retrieval.fusion import _FusionMixin

        s = MagicMock()
        s.WRRF_GRAPH_PRIOR_WEIGHT = 0.2
        s.WRRF_COFIRE_PRIOR_WEIGHT = 0.15
        s.WRRF_VECTOR_WEIGHT = 1.0
        s.WRRF_FTS_WEIGHT = 0.5
        s.WRRF_PPR_WEIGHT = 0.5
        s.WRRF_SPREADING_WEIGHT = 0.3
        s.CONFIDENCE_GATING_ENABLED = False
        s.FUSION_METHOD = "wrrf"
        s.FUSION_NORM = "zscore"
        s.COMBMNZ_ENABLED = False
        s.WRRF_K = 60

        storage = MagicMock()
        # Memory 1: high graph_prior only; memory 2: high cofire_prior only
        # Memory 3: baseline (both absent)
        storage.get_memory_graph_priors.return_value = {1: 1.0}
        storage.get_memory_cofire_priors.return_value = {2: 1.0}

        class _TestFuser(_FusionMixin):
            def __init__(self):
                self._settings = s
                self._storage = storage
                self._reranker = MagicMock()
                self._reranker.compute_signal_confidence.return_value = 1.0

        fuser = _TestFuser()

        scores = {
            1: {"vector": 0.7, "fts": 0.5, "ppr": 0.0, "spread": 0.0},
            2: {"vector": 0.7, "fts": 0.5, "ppr": 0.0, "spread": 0.0},
            3: {"vector": 0.7, "fts": 0.5, "ppr": 0.0, "spread": 0.0},
        }

        fused, fused_scores_out = fuser._fuse_scores(
            scores=scores,
            w_temporal=0.0,
            open_domain_mode=False,
        )

        # Both boosted memories must rank above baseline memory 3
        assert fused_scores_out[1] > fused_scores_out[3], (
            f"Memory 1 (graph_prior=1.0) must rank above baseline memory 3 "
            f"(scores: {fused_scores_out[1]:.4f} vs {fused_scores_out[3]:.4f})"
        )
        assert fused_scores_out[2] > fused_scores_out[3], (
            f"Memory 2 (cofire_prior=1.0) must rank above baseline memory 3 "
            f"(scores: {fused_scores_out[2]:.4f} vs {fused_scores_out[3]:.4f})"
        )
        # Both storage methods must be called (both boosts applied)
        storage.get_memory_graph_priors.assert_called_once()
        storage.get_memory_cofire_priors.assert_called_once()

    def test_graph_prior_source_still_present(self):
        """fusion.py must still contain WRRF_GRAPH_PRIOR_WEIGHT (5.54.1 intact)."""
        fusion_src = pathlib.Path(__file__).parent.parent / "retrieval" / "fusion.py"
        source = fusion_src.read_text()

        assert "WRRF_GRAPH_PRIOR_WEIGHT" in source, (
            "fusion.py must still contain WRRF_GRAPH_PRIOR_WEIGHT (5.54.1 intact)"
        )
        assert "get_memory_graph_priors" in source, (
            "fusion.py must still call get_memory_graph_priors (5.54.1 intact)"
        )
        assert "get_memory_cofire_priors" in source, (
            "fusion.py must call get_memory_cofire_priors (5.54.2)"
        )

    def test_cofire_boost_after_graph_prior_in_source(self):
        """fusion.py: cofire boost section must appear after graph_prior boost."""
        fusion_src = pathlib.Path(__file__).parent.parent / "retrieval" / "fusion.py"
        source = fusion_src.read_text()

        gp_idx = source.index("WRRF_GRAPH_PRIOR_WEIGHT")
        cf_idx = source.index("WRRF_COFIRE_PRIOR_WEIGHT")
        assert cf_idx > gp_idx, (
            f"WRRF_COFIRE_PRIOR_WEIGHT (idx={cf_idx}) must appear after "
            f"WRRF_GRAPH_PRIOR_WEIGHT (idx={gp_idx}) in fusion.py"
        )


# ---------------------------------------------------------------------------
# 6. Migration 021 registered in _MIGRATIONS
# ---------------------------------------------------------------------------


class TestMigration021Registered:
    """Migration 021_memory_cofire_prior is registered and in correct order."""

    def test_migration_021_in_list(self):
        """_MIGRATIONS must include 021_memory_cofire_prior."""
        from yadgar.storage.migrations import _MIGRATIONS

        versions = [m["version"] for m in _MIGRATIONS]
        assert "021_memory_cofire_prior" in versions, (
            f"021_memory_cofire_prior missing from _MIGRATIONS. Found: {versions}"
        )

    def test_migration_021_after_020(self):
        """021 must come immediately after 020 in the list."""
        from yadgar.storage.migrations import _MIGRATIONS

        versions = [m["version"] for m in _MIGRATIONS]
        idx_020 = versions.index("020_memory_graph_prior")
        idx_021 = versions.index("021_memory_cofire_prior")
        assert idx_021 == idx_020 + 1, (
            f"021 must immediately follow 020; idx_020={idx_020}, idx_021={idx_021}"
        )

    def test_migration_017_still_reserved(self):
        """017 slot must remain reserved."""
        from yadgar.storage.migrations import _MIGRATIONS

        versions = [m["version"] for m in _MIGRATIONS]
        assert not any("017" in v for v in versions), (
            "017 is RESERVED for v5.61 (wiki_source_hash); must not be used"
        )

    def test_migration_021_fn_callable(self):
        """Migration 021 function must be callable."""
        from yadgar.storage.migrations import _migration_021_memory_cofire_prior

        assert callable(_migration_021_memory_cofire_prior)


# ---------------------------------------------------------------------------
# 7. get_memory_cofire_priors storage method
# ---------------------------------------------------------------------------


class TestGetMemoryCofirePriors:
    """Storage method get_memory_cofire_priors correctness."""

    def test_returns_float_values(self):
        """get_memory_cofire_priors returns {int: float} for present priors."""
        from yadgar.storage.memory import _MemoryMixin

        mixin = object.__new__(_MemoryMixin)

        def mock_q(sql, params=None):
            mid = params["id"]
            if mid == 1:
                return [{"id": f"memory:{mid}", "cofire_prior": 0.65}]
            return []

        mixin._q = mock_q
        mixin._extract_id = lambda rid: (
            int(str(rid).split(":")[-1]) if ":" in str(rid) else int(rid)
        )

        result = _MemoryMixin.get_memory_cofire_priors(mixin, [1, 2])
        assert result == {1: 0.65}, f"Expected {{1: 0.65}}, got {result}"

    def test_absent_priors_not_in_result(self):
        """Memories without cofire_prior are absent from result."""
        from yadgar.storage.memory import _MemoryMixin

        mixin = object.__new__(_MemoryMixin)
        mixin._q = MagicMock(return_value=[])
        mixin._extract_id = lambda rid: (
            int(str(rid).split(":")[-1]) if ":" in str(rid) else int(rid)
        )

        result = _MemoryMixin.get_memory_cofire_priors(mixin, [1, 2, 3])
        assert result == {}, f"Expected empty dict for all-NULL priors, got {result}"

    def test_empty_input_no_queries(self):
        """Empty memory_ids list: no DB calls, returns empty dict."""
        from yadgar.storage.memory import _MemoryMixin

        mixin = object.__new__(_MemoryMixin)
        mixin._q = MagicMock()

        result = _MemoryMixin.get_memory_cofire_priors(mixin, [])
        assert result == {}
        mixin._q.assert_not_called()


# ---------------------------------------------------------------------------
# 8. I25 three-way config sync for WRRF_COFIRE_PRIOR_WEIGHT
# ---------------------------------------------------------------------------


class TestCofirePriorConfigRegistered:
    """WRRF_COFIRE_PRIOR_WEIGHT is three-way registered (config.py + registry + yaml)."""

    def test_settings_has_wrrf_cofire_prior_weight(self):
        """Settings must have WRRF_COFIRE_PRIOR_WEIGHT with default 0.15."""
        from yadgar.config import Settings

        default_val = Settings.model_fields["WRRF_COFIRE_PRIOR_WEIGHT"].default
        assert default_val == 0.15, (
            f"WRRF_COFIRE_PRIOR_WEIGHT default must be 0.15, got {default_val}"
        )

    def test_registry_has_wrrf_cofire_prior_weight(self):
        """config_registry must include YADGAR_WRRF_COFIRE_PRIOR_WEIGHT."""
        from yadgar.config_registry import list_config

        names = {e.name for e in list_config()}
        assert "YADGAR_WRRF_COFIRE_PRIOR_WEIGHT" in names, (
            f"YADGAR_WRRF_COFIRE_PRIOR_WEIGHT missing from config_registry. "
            f"Found WRRF entries: {[n for n in names if 'WRRF' in n]}"
        )

    def test_yaml_meta_has_wrrf_cofire_prior_weight(self):
        """config_yaml.py FIELD_META must include wrrf_cofire_prior_weight."""
        from yadgar.config_yaml import FIELD_META

        assert "wrrf_cofire_prior_weight" in FIELD_META, (
            "FIELD_META must include 'wrrf_cofire_prior_weight' for I25 three-way sync"
        )

    def test_consolidation_cofire_phase_in_orchestrator(self):
        """orchestrator.py must wire compute_cofire_priors phase."""
        orch_src = pathlib.Path(__file__).parent.parent / "consolidation" / "orchestrator.py"
        source = orch_src.read_text()

        assert "compute_cofire_priors" in source, (
            "orchestrator.py must call _compute_cofire_priors in the consolidation cycle"
        )
        assert "phase_start: compute_cofire_priors" in source, (
            "orchestrator.py must log phase_start: compute_cofire_priors"
        )
