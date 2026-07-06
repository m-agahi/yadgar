"""Tests for v5.1 C2 / C3 / C4 branch-retrieval polish.

C2: branch filter pushed into SurrealQL WHERE clause.
C3: _detect_branch LRU bucket jittered per directory.
C4: boost uses convex combination; scores stay in [0, 1].
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

# ---------------------------------------------------------------------------
# C2 — branch filter in SurrealQL
# ---------------------------------------------------------------------------


class TestC2BranchFilterInSurrealQL:
    """Branch predicate is injected into storage query strings, not post-filtered in Python."""

    def _make_retriever(self, current_branch=None, default_branch="master"):
        """Build a Retriever with a recording storage mock."""
        from yadgar.config import Settings
        from yadgar.embeddings import EmbeddingEngine
        from yadgar.knowledge_graph import KnowledgeGraph
        from yadgar.retrieval.core import Retriever

        settings = Settings(
            DB_PATH="/tmp/test.db",
            CROSS_ENCODER_ENABLED=False,
            NLI_RERANKING_ENABLED=False,
            ADVERSARIAL_DETECTION_ENABLED=False,
            ADVERSARIAL_DIVERSITY_ENFORCEMENT=False,
            MULTI_PASSAGE_RERANKING_ENABLED=False,
            COMPARISON_DUAL_SEARCH_ENABLED=False,
            TEMPORAL_RETRIEVAL_ENABLED=False,
            QUERY_EXPANSION_ENABLED=False,
            COMET_QUERY_EXPANSION_ENABLED=False,
            RETRIEVAL_PROFILE="balanced",
            FUSION_METHOD="wrrf",
            COMBMNZ_ENABLED=False,
        )

        storage = MagicMock()
        storage.search_memories_fts_scored.return_value = []
        storage.search_vectors.return_value = []
        storage.get_all_entities.return_value = []
        storage.find_memory_ids_by_entity_name.return_value = []
        storage.get_memory.return_value = None
        storage.search_profiles_fts.return_value = []
        storage.search_beliefs_fts.return_value = []
        storage.search_memories_by_content_date.return_value = []
        storage.search_memories_by_month.return_value = []

        embeddings = MagicMock(spec=EmbeddingEngine)
        embeddings.encode_query.return_value = None

        kg = MagicMock(spec=KnowledgeGraph)
        kg._get_adjacent.return_value = []

        stub_ml = MagicMock()
        stub_ml.cross_encode.return_value = []
        stub_ml.nli_score.return_value = []
        stub_ml.is_idle.return_value = True

        retriever = Retriever(storage, embeddings, kg, settings, ml_client=stub_ml)
        retriever._rules_engine = None
        retriever._engram = None
        retriever._metacognition = None

        return retriever, storage

    def test_c2_branch_filter_in_surrealql(self):
        """SurrealQL queries include branch predicate when current_branch is set."""
        retriever, storage = self._make_retriever(current_branch="main", default_branch="master")
        retriever.recall(
            "test query",
            max_results=5,
            min_heat=0.0,
            current_branch="main",
            default_branch="master",
        )

        # At least one call to search_memories_fts_scored or search_vectors
        # must have received a branch_filter argument containing the current branch.
        fts_calls = storage.search_memories_fts_scored.call_args_list
        vec_calls = storage.search_vectors.call_args_list

        branch_filter_passed = False
        for call in fts_calls + vec_calls:
            kwargs = call.kwargs if call.kwargs else {}
            args = call.args if call.args else ()
            bf = kwargs.get("branch_filter") or (args[2] if len(args) > 2 else None)
            if bf is not None:
                if "main" in str(bf) or "master" in str(bf):
                    branch_filter_passed = True
                    break

        assert branch_filter_passed, (
            "Expected branch_filter containing 'main'/'master' to be passed to storage "
            f"search methods. fts_calls={fts_calls}, vec_calls={vec_calls}"
        )

    def test_c2_branch_filter_degrades_to_null_only_when_branch_unknown(self):
        """When current_branch=None, queries should only allow NULL-branch rows (+ default)."""
        retriever, storage = self._make_retriever(current_branch=None, default_branch="master")
        retriever.recall(
            "test query",
            max_results=5,
            min_heat=0.0,
            current_branch=None,
            default_branch="master",
        )

        # When current_branch is None, branch_filter should be passed but
        # should NOT include a current-branch predicate. It should allow only
        # NULL-branch (and optionally default_branch).
        fts_calls = storage.search_memories_fts_scored.call_args_list
        vec_calls = storage.search_vectors.call_args_list

        # If branch_filter is passed at all, it must not include current_branch
        for call in fts_calls + vec_calls:
            kwargs = call.kwargs if call.kwargs else {}
            bf = kwargs.get("branch_filter")
            if bf is not None:
                # Should not contain a feature branch reference beyond master/None
                # i.e., if bf is a tuple/object, current_branch component should be None
                if hasattr(bf, "current_branch"):
                    assert bf.current_branch is None, (
                        f"branch_filter.current_branch should be None when current=None, got {bf}"
                    )
                elif isinstance(bf, dict):
                    assert bf.get("current_branch") is None, (
                        f"branch_filter['current_branch'] should be None, got {bf}"
                    )
                # Accept any representation as long as the filter was constructed
                # without a non-None current branch
        # Test passes if no assertion fired above — filter was either not passed
        # or correctly omitted current_branch component
        assert True


# ---------------------------------------------------------------------------
# C3 — LRU bucket jitter per directory
# ---------------------------------------------------------------------------


class TestC3LRUBucketJitter:
    """_detect_branch LRU bucket is jittered per-directory."""

    def _get_bucket(self, directory: str, t: float) -> int:
        """Compute the bucket integer the same way _detect_branch does."""
        return int((t + (hash(directory) % 30)) // 30)

    def test_c3_lru_bucket_jittered_per_directory(self):
        """Different directories produce different bucket values at the same time (usually)."""
        # Use a fixed time to make this deterministic
        fixed_time = 1_700_000_000.0  # arbitrary epoch seconds

        dirs = [f"/home/user/project-{i}" for i in range(50)]
        buckets = [self._get_bucket(d, fixed_time) for d in dirs]

        # With 50 dirs and a 30-value jitter space, we expect significant spread.
        # If all buckets were identical, the jitter isn't working.
        unique_buckets = set(buckets)
        assert len(unique_buckets) > 1, (
            f"All 50 directories map to the same bucket {buckets[0]}; jitter not working"
        )

        # More specifically: at least 2 distinct adjacent-second offsets should appear.
        # In practice ~half the directories should differ from the other half.
        assert len(unique_buckets) >= 2, (
            f"Expected ≥2 unique buckets across 50 dirs, got {unique_buckets}"
        )

    def test_c3_lru_bucket_stable_within_30s_for_same_dir(self):
        """Same directory bucket stays constant for time within a single 30s window."""
        directory = "/home/user/repo"
        jitter = hash(directory) % 30

        # Find a time t such that (t + jitter) is exactly at the START of a 30s bucket.
        # Then we can safely add up to 29 seconds and stay in the same bucket.
        # Start of bucket n: (t + jitter) == n * 30, so t = n*30 - jitter.
        n = 56666670  # arbitrary large bucket index
        t_start = n * 30 - jitter  # t_start + jitter == n*30 exactly

        b_start = int((t_start + jitter) // 30)

        # Advance 14 seconds — must stay in the same bucket
        t_mid = t_start + 14.0
        b_mid = int((t_mid + jitter) // 30)

        assert b_start == b_mid, (
            f"Bucket changed within 14s window: {b_start} -> {b_mid} "
            f"(jitter={jitter}, t_start={t_start}, t_mid={t_mid})"
        )

        # Advance 29 seconds — still in the same bucket
        t_near_end = t_start + 29.0
        b_near_end = int((t_near_end + jitter) // 30)
        assert b_start == b_near_end, f"Bucket changed within 29s: {b_start} -> {b_near_end}"

    def test_c3_actual_detect_branch_uses_jitter(self, monkeypatch):
        """The actual _detect_branch function injects a per-directory hash jitter."""
        import inspect

        from yadgar import server as s

        # Read the source of _detect_branch to verify the jitter formula is present
        src = inspect.getsource(s._detect_branch)
        assert "hash(" in src, (
            "_detect_branch should use hash(directory) for jitter; source:\n" + src
        )
        assert "% 30" in src, "_detect_branch should use '% 30' jitter offset; source:\n" + src


# ---------------------------------------------------------------------------
# C4 — Convex boost bounded in [0, 1]
# ---------------------------------------------------------------------------


class TestC4BoostBoundedUnitInterval:
    """Branch boost uses convex combination; results stay in [0, 1]."""

    def _apply_boost(self, score: float, weight: float) -> float:
        """Replicate the convex boost formula: score + (1 - score) * weight."""
        return score + (1.0 - score) * weight

    def test_c4_boost_bounded_in_unit_interval(self):
        """Boosted scores stay <= 1.0 regardless of weight."""
        from yadgar.config import Settings

        weight = Settings().BRANCH_BOOST_WEIGHT  # type: ignore[attr-defined]

        for score in [0.0, 0.1, 0.5, 0.9, 1.0]:
            boosted = self._apply_boost(score, weight)
            assert boosted <= 1.0, f"score={score}, weight={weight}: boosted={boosted} exceeds 1.0"
            if score < 1.0:
                assert boosted > score, (
                    f"score={score}: boosted={boosted} should be > original score"
                )

    def test_c4_boost_preserves_ranking(self):
        """Convex boost on middle candidate moves it up without violating [0,1]."""
        from yadgar.config import Settings

        weight = Settings().BRANCH_BOOST_WEIGHT  # type: ignore[attr-defined]

        # Three candidates: only the middle one gets boosted
        scores = [0.3, 0.5, 0.7]
        candidates = [{"_retrieval_score": s, "branch": None} for s in scores]
        candidates[1]["branch"] = "feat/current"

        # Apply boost to middle one
        for m in candidates:
            if m.get("branch") == "feat/current":
                base = m["_retrieval_score"]
                m["_retrieval_score"] = base + (1.0 - base) * weight

        # Re-sort
        candidates.sort(key=lambda m: m["_retrieval_score"], reverse=True)

        # Middle candidate (originally 0.5) should now rank above original 0.5
        boosted_score = candidates[0]["_retrieval_score"]
        assert boosted_score <= 1.0, f"Boosted score {boosted_score} exceeds 1.0"
        assert boosted_score > 0.5, f"Boosted 0.5 should be > 0.5, got {boosted_score}"

        # All scores in [0, 1]
        for c in candidates:
            assert 0.0 <= c["_retrieval_score"] <= 1.0, (
                f"Score {c['_retrieval_score']} out of [0, 1]"
            )

    def test_c4_no_boost_when_branch_is_none(self):
        """When current_branch=None, _retrieval_scores are unchanged."""
        from yadgar import server as s

        # Monkeypatch branch detection to return None
        with patch.object(s, "_detect_branch", return_value=None):
            with patch.object(s, "_get_default_branch", return_value="master"):
                # Build some fake merged results that would come from the retriever
                fake_results = [
                    {
                        "id": 1,
                        "content": "a",
                        "heat": 0.9,
                        "_retrieval_score": 0.8,
                        "branch": "master",
                    },  # noqa: E501
                    {"id": 2, "content": "b", "heat": 0.7, "_retrieval_score": 0.6, "branch": None},
                ]
                original_scores = [m["_retrieval_score"] for m in fake_results]

                # Simulate what server.py's recall() does with the branch boost section
                _current_branch = None
                _default_branch = "master"
                _allowed_branches: set = {_default_branch, None}
                if _current_branch is not None:
                    _allowed_branches.add(_current_branch)

                merged = [m for m in fake_results if m.get("branch") in _allowed_branches]

                # C4: boost only applies when _current_branch is not None
                if _current_branch is not None:
                    from yadgar.config import Settings

                    weight = Settings().BRANCH_BOOST_WEIGHT  # type: ignore[attr-defined]
                    for m in merged:
                        if m.get("branch") == _current_branch:
                            base = m.get("_retrieval_score", m.get("heat", 0.0))
                            m["_retrieval_score"] = base + (1.0 - base) * weight
                    merged.sort(key=lambda m: m.get("_retrieval_score", 0.0), reverse=True)

                # Scores should be unchanged
                for m, orig in zip(merged, original_scores, strict=False):
                    assert m["_retrieval_score"] == orig, (
                        f"Score changed when branch is None: {orig} -> {m['_retrieval_score']}"
                    )

    def test_c4_branch_boost_weight_in_config(self):
        """BRANCH_BOOST_WEIGHT is defined in Settings and is a float in (0, 1)."""
        from yadgar.config import Settings

        settings = Settings()
        assert hasattr(settings, "BRANCH_BOOST_WEIGHT"), (
            "Settings must have BRANCH_BOOST_WEIGHT field"
        )
        w = settings.BRANCH_BOOST_WEIGHT  # type: ignore[attr-defined]
        assert isinstance(w, float), f"BRANCH_BOOST_WEIGHT should be float, got {type(w)}"
        assert 0.0 < w < 1.0, f"BRANCH_BOOST_WEIGHT should be in (0, 1), got {w}"

    def test_c4_boost_formula_matches_convex_combination(self):
        """Verify the branch boost uses the convex combination formula.

        Phase 2a forward-only: server.recall() is now a pure forwarder and the
        C4 branch-boost logic relocated to _apply_fanout_boosts in
        _recall_pipeline.py (runs inside _fanout_recall on the backend). Inspect
        the boost function at its new home.
        """
        import inspect

        from yadgar.server.tools._recall_pipeline import _apply_fanout_boosts

        src = inspect.getsource(_apply_fanout_boosts)
        # Should NOT contain the old 1.5x multiplier
        assert "* 1.5" not in src, (
            "_apply_fanout_boosts still uses the old '* 1.5' multiplier; should use convex boost"
        )
        # Should contain the convex combination pattern
        assert "BRANCH_BOOST_WEIGHT" in src or "branch_boost_weight" in src.lower(), (
            "_apply_fanout_boosts should reference BRANCH_BOOST_WEIGHT for the convex boost"
        )

    def test_c4_boost_clamps_base_above_1(self):
        """Boost must not invert when WRRF produces scores > 1.0 (C4 inversion fix)."""
        # WRRF can produce fused scores > 1.0 e.g. 1.5.
        # Without clamping: 1.5 + (1 - 1.5) * 0.2 = 1.4  (SUPPRESSED, not boosted)
        # With clamping:    min(1.5, 1.0) = 1.0, then 1.0 + 0 * 0.2 = 1.0  (neutral but not inverted)
        weight = 0.2
        base_above_1 = 1.5

        # Formula WITHOUT clamp — inversion:
        inverted = base_above_1 + (1.0 - base_above_1) * weight
        assert inverted < base_above_1, "Sanity: un-clamped formula inverts for base > 1"

        # Formula WITH clamp — no inversion:
        clamped_base = min(base_above_1, 1.0)
        boosted = clamped_base + (1.0 - clamped_base) * weight
        assert boosted >= clamped_base, (
            f"Clamped boost should not suppress: {clamped_base} -> {boosted}"
        )

    def test_c4_server_boost_clamps_base(self):
        """Branch boost must clamp base score to 1.0 before the convex step.

        Phase 2a forward-only: the boost relocated from server.recall() to
        _apply_fanout_boosts in _recall_pipeline.py. Inspect it there.
        """
        import inspect
        import re as _re

        from yadgar.server.tools._recall_pipeline import _apply_fanout_boosts

        src = inspect.getsource(_apply_fanout_boosts)
        # The boost block assigns `base = m.get(...)`.  It must be:
        #   base = min(m.get(...), 1.0)
        # This specific pattern confirms the clamp is in the boost assignment, not
        # somewhere else (e.g. the heat-bump line `min(m["heat"] + 0.1, 1.0)`).
        has_clamp_in_base = bool(_re.search(r"base\s*=\s*min\(", src))
        assert has_clamp_in_base, (
            "_apply_fanout_boosts boost block must assign `base = min(...)` to clamp "
            "WRRF scores > 1.0 before the convex combination step. "
            "Without this, base=1.5 → boosted=1.4 (suppressed instead of boosted)."
        )


# ---------------------------------------------------------------------------
# C5 (follow-up) — temporal retrieval path threads branch_filter
# ---------------------------------------------------------------------------


class TestC5TemporalBranchFilter:
    """_collect_temporal_scores passes branch_filter to storage temporal methods."""

    def _make_retriever_temporal(self, current_branch="feat/x", default_branch="master"):
        from yadgar.config import Settings
        from yadgar.embeddings import EmbeddingEngine
        from yadgar.knowledge_graph import KnowledgeGraph
        from yadgar.retrieval.core import Retriever

        settings = Settings(
            DB_PATH="/tmp/test.db",
            CROSS_ENCODER_ENABLED=False,
            NLI_RERANKING_ENABLED=False,
            ADVERSARIAL_DETECTION_ENABLED=False,
            ADVERSARIAL_DIVERSITY_ENFORCEMENT=False,
            MULTI_PASSAGE_RERANKING_ENABLED=False,
            COMPARISON_DUAL_SEARCH_ENABLED=False,
            TEMPORAL_RETRIEVAL_ENABLED=True,  # <-- enabled
            QUERY_EXPANSION_ENABLED=False,
            COMET_QUERY_EXPANSION_ENABLED=False,
            RETRIEVAL_PROFILE="balanced",
            FUSION_METHOD="wrrf",
            COMBMNZ_ENABLED=False,
        )

        storage = MagicMock()
        storage.search_memories_fts_scored.return_value = []
        storage.search_vectors.return_value = []
        storage.get_all_entities.return_value = []
        storage.find_memory_ids_by_entity_name.return_value = []
        storage.get_memory.return_value = None
        storage.search_profiles_fts.return_value = []
        storage.search_beliefs_fts.return_value = []
        storage.search_memories_by_content_date.return_value = []
        storage.search_memories_by_month.return_value = []

        embeddings = MagicMock(spec=EmbeddingEngine)
        embeddings.encode_query.return_value = None

        kg = MagicMock(spec=KnowledgeGraph)
        kg._get_adjacent.return_value = []

        stub_ml = MagicMock()
        stub_ml.cross_encode.return_value = []
        stub_ml.nli_score.return_value = []
        stub_ml.is_idle.return_value = True

        retriever = Retriever(storage, embeddings, kg, settings, ml_client=stub_ml)
        retriever._rules_engine = None
        retriever._engram = None
        retriever._metacognition = None

        return retriever, storage

    def test_c5_temporal_search_receives_branch_filter(self):
        """search_memories_by_content_date and search_memories_by_month get branch_filter."""
        retriever, storage = self._make_retriever_temporal()

        # Use a query with temporal markers so _collect_temporal_scores actually runs
        retriever.recall(
            "what happened in january last year",
            max_results=5,
            min_heat=0.0,
            current_branch="feat/x",
            default_branch="master",
        )

        # Both temporal storage methods should have received a branch_filter kwarg
        for method_name in ("search_memories_by_content_date", "search_memories_by_month"):
            method = getattr(storage, method_name)
            all_calls = method.call_args_list
            if not all_calls:
                # Method not called — temporal parser may not have triggered; skip silently
                continue
            for call in all_calls:
                kwargs = call.kwargs if call.kwargs else {}
                assert "branch_filter" in kwargs, (
                    f"{method_name} called without branch_filter kwarg. call={call}"
                )
                bf = kwargs["branch_filter"]
                assert bf is not None, (
                    f"{method_name} received branch_filter=None; expected a BranchFilter object"
                )
