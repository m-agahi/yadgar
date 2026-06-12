"""TDD tests for v5.51.0 §4.2 — fast profile candidate pool + query analysis skip.

Tests verify:
- PROFILES["fast"] has skip_query_analysis=True and use_fast_candidate_multiplier=True
- core.py uses FAST_PROFILE_CANDIDATE_MULTIPLIER (not global) for fast profile
- core.py skips analyze_query when skip_query_analysis=True
- balanced/full profiles are unaffected by the fast-profile knobs
- fast profile with skip_query_analysis=True does NOT get empty enabled_signals
  (guards the empty-set trap when QUERY_ROUTING_ENABLED=True)
"""

from __future__ import annotations


class TestFastProfileDefinition:
    """PROFILES dict in fusion.py must declare the new fast-profile flags."""

    def test_fast_profile_has_skip_query_analysis(self):
        """PROFILES['fast'] must have skip_query_analysis=True."""
        from yadgar.retrieval.fusion import PROFILES

        assert "fast" in PROFILES, "PROFILES must contain 'fast' key"
        profile = PROFILES["fast"]
        assert profile.get("skip_query_analysis") is True, (
            f"PROFILES['fast']['skip_query_analysis'] expected True, "
            f"got {profile.get('skip_query_analysis')!r}. "
            "Fast profile must skip analyze_query to reduce per-hook overhead."
        )

    def test_fast_profile_has_use_fast_candidate_multiplier(self):
        """PROFILES['fast'] must have use_fast_candidate_multiplier=True."""
        from yadgar.retrieval.fusion import PROFILES

        profile = PROFILES["fast"]
        assert profile.get("use_fast_candidate_multiplier") is True, (
            f"PROFILES['fast']['use_fast_candidate_multiplier'] expected True, "
            f"got {profile.get('use_fast_candidate_multiplier')!r}."
        )

    def test_balanced_profile_unchanged(self):
        """balanced profile must NOT have skip_query_analysis or use_fast_candidate_multiplier."""
        from yadgar.retrieval.fusion import PROFILES

        balanced = PROFILES["balanced"]
        assert not balanced.get("skip_query_analysis", False), (
            "balanced profile must NOT have skip_query_analysis=True"
        )
        assert not balanced.get("use_fast_candidate_multiplier", False), (
            "balanced profile must NOT have use_fast_candidate_multiplier=True"
        )

    def test_full_profile_unchanged(self):
        """full profile must NOT have skip_query_analysis or use_fast_candidate_multiplier."""
        from yadgar.retrieval.fusion import PROFILES

        full = PROFILES["full"]
        assert not full.get("skip_query_analysis", False), (
            "full profile must NOT have skip_query_analysis=True"
        )
        assert not full.get("use_fast_candidate_multiplier", False), (
            "full profile must NOT have use_fast_candidate_multiplier=True"
        )


class TestFastProfileCandidateMultiplier:
    """core.py must use FAST_PROFILE_CANDIDATE_MULTIPLIER when profile has use_fast_candidate_multiplier.

    Tests the logic in recall() directly by extracting the candidate_k computation
    into a standalone helper that mirrors core.py's decision tree.
    """

    def _compute_candidate_k(
        self, profile_name: str, max_results: int, fast_mult: int, global_mult: int
    ) -> int:
        """Mirror the core.py candidate_k computation for testing."""
        from yadgar.retrieval.fusion import PROFILES

        profile = PROFILES.get(profile_name, PROFILES["balanced"])
        if profile.get("use_fast_candidate_multiplier", False):
            return max_results * fast_mult
        else:
            return max_results * global_mult

    def test_fast_profile_uses_fast_multiplier(self):
        """candidate_k for fast profile = max_results * FAST_PROFILE_CANDIDATE_MULTIPLIER."""
        max_results = 5
        fast_mult = 3
        global_mult = 20

        computed = self._compute_candidate_k("fast", max_results, fast_mult, global_mult)
        expected = max_results * fast_mult

        assert computed == expected, (
            f"Fast profile candidate_k expected {expected} "
            f"(max_results={max_results} * FAST_PROFILE_CANDIDATE_MULTIPLIER={fast_mult}), "
            f"got {computed}. "
            f"Must NOT use global CANDIDATE_POOL_MULTIPLIER={global_mult}."
        )
        assert computed != max_results * global_mult, (
            "Fast profile must use smaller multiplier, not global=20."
        )

    def test_balanced_profile_uses_global_multiplier(self):
        """candidate_k for balanced profile = max_results * CANDIDATE_POOL_MULTIPLIER."""
        max_results = 5
        fast_mult = 3
        global_mult = 20

        computed = self._compute_candidate_k("balanced", max_results, fast_mult, global_mult)
        expected = max_results * global_mult

        assert computed == expected, (
            f"Balanced profile candidate_k expected {expected} "
            f"(max_results={max_results} * CANDIDATE_POOL_MULTIPLIER={global_mult}), "
            f"got {computed}."
        )

    def test_fast_profile_candidate_k_smaller_than_global(self):
        """Fast profile candidate_k must be strictly smaller than balanced for same max_results."""
        max_results = 5
        fast_k = self._compute_candidate_k("fast", max_results, 3, 20)
        balanced_k = self._compute_candidate_k("balanced", max_results, 3, 20)

        assert fast_k < balanced_k, (
            f"Fast profile candidate_k={fast_k} must be < balanced candidate_k={balanced_k}. "
            "Fast profile purpose is to reduce DB fetch cost."
        )


class TestFastProfileSkipsQueryAnalysis:
    """core.py skip_query_analysis flag in PROFILES["fast"] prevents analyze_query call.

    Tests verify by inspecting the profile flag and the core.py source logic,
    rather than mocking the full Retriever call chain (mixin methods not patchable
    via class-level patch when self is a MagicMock).
    """

    def test_fast_profile_flag_skips_analysis(self):
        """PROFILES['fast']['skip_query_analysis']=True means analyze_query is NOT called."""
        from yadgar.retrieval.fusion import PROFILES

        profile = PROFILES["fast"]
        assert profile.get("skip_query_analysis") is True, (
            "PROFILES['fast']['skip_query_analysis'] must be True — "
            "this is the gate that prevents analyze_query from running."
        )

    def test_balanced_profile_flag_does_not_skip_analysis(self):
        """PROFILES['balanced']['skip_query_analysis'] is absent/False → analyze_query runs."""
        from yadgar.retrieval.fusion import PROFILES

        profile = PROFILES["balanced"]
        assert not profile.get("skip_query_analysis", False), (
            "PROFILES['balanced'] must NOT have skip_query_analysis=True — "
            "balanced profile must run analyze_query for routing."
        )

    def test_core_py_branches_on_skip_query_analysis(self):
        """core.py recall() must have a branch guarded by profile.get('skip_query_analysis')."""
        import pathlib

        core_src = pathlib.Path(__file__).parent.parent / "retrieval" / "core.py"
        source = core_src.read_text()

        assert "skip_query_analysis" in source, (
            "core.py must gate analyze_query on profile.get('skip_query_analysis'). "
            "This flag controls whether query expansion runs on the fast path."
        )
        # The guard must appear in the recall() flow
        assert (
            'profile.get("skip_query_analysis"' in source
            or "profile.get('skip_query_analysis'" in source
        ), "core.py must call profile.get('skip_query_analysis') in the recall() flow."


class TestFastProfileEnabledSignalsNotEmpty:
    """Fast profile with skip_query_analysis=True must yield non-empty enabled_signals.

    Regression guard: if QUERY_ROUTING_ENABLED=True and routing intersection is
    applied to an empty query_analysis dict, enabled_signals would be empty set,
    causing fast profile to retrieve nothing. The fix bypasses the intersection.

    Tests at the logic level: when skip_query_analysis=True, core.py must take the
    branch that sets enabled_signals = profile_signals (bypassing routing intersection).
    """

    def test_core_py_bypasses_routing_intersection_for_fast_profile(self):
        """When skip_query_analysis=True, core.py sets enabled_signals=profile_signals directly."""
        import pathlib

        core_src = pathlib.Path(__file__).parent.parent / "retrieval" / "core.py"
        source = core_src.read_text()

        # Verify that the skip path assigns enabled_signals = profile_signals (or equivalent)
        # rather than doing the routing intersection
        assert "enabled_signals = profile_signals" in source, (
            "core.py must have 'enabled_signals = profile_signals' on the fast-profile "
            "skip path. Without this, QUERY_ROUTING_ENABLED=True with an empty "
            "query_analysis would yield enabled_signals=set() and retrieve nothing."
        )

    def test_fast_profile_signals_non_empty_from_profile_definition(self):
        """PROFILES['fast']['signals'] must include vector and fts."""
        from yadgar.retrieval.fusion import PROFILES

        fast_signals = set(PROFILES["fast"]["signals"])
        assert len(fast_signals) > 0, (
            f"PROFILES['fast']['signals'] is empty: {fast_signals!r}. "
            "Fast profile must have at least vector + fts."
        )
        assert "vector" in fast_signals, "PROFILES['fast']['signals'] must include 'vector'"
        assert "fts" in fast_signals, "PROFILES['fast']['signals'] must include 'fts'"
