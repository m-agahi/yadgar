"""Unit tests for FANOUT_BOOST_SCOPE config knob.

Tests each mode × profile combination against both boosts (branch + postmortem):
  - "off"    → boosts never applied regardless of profile
  - "scoped" → boosts only when profile is not None
  - "global" → boosts always applied regardless of profile

Written red-first (TDD); goes green once _apply_fanout_boosts threads profile +
reads FANOUT_BOOST_SCOPE from settings.
"""

from __future__ import annotations

import pytest

import yadgar.server.tools._recall_pipeline as _pipeline_module
from yadgar.server.tools._recall_pipeline import _apply_fanout_boosts

# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


def _make_branch_mem(branch: str = "feat/x", score: float = 0.5) -> dict:
    """Memory dict whose branch matches the test branch (triggers branch boost)."""
    return {
        "id": 1,
        "content": "branch memory",
        "heat": score * 0.5,
        "_retrieval_score": score,
        "tags": [],
        "branch": branch,
        "_source": "memory",
    }


def _make_pm_mem(score: float = 0.5) -> dict:
    """Memory tagged _postmortem (triggers postmortem boost)."""
    return {
        "id": 2,
        "content": "postmortem memory",
        "heat": score * 0.5,
        "_retrieval_score": score,
        "tags": ["_postmortem"],
        "branch": None,
        "_source": "memory",
    }


def _make_neutral_mem(score: float = 0.5) -> dict:
    """Memory with no branch / postmortem tag (never boosted)."""
    return {
        "id": 3,
        "content": "neutral memory",
        "heat": score * 0.5,
        "_retrieval_score": score,
        "tags": [],
        "branch": None,
        "_source": "memory",
    }


def _base_score() -> float:
    """Original _retrieval_score for all constructed memories."""
    return 0.5


BRANCH = "feat/x"
PM_QUERY = "deploy this"  # contains "deploy" → postmortem keyword


def _call_boosts(
    pooled: list[dict],
    query: str = PM_QUERY,
    current_branch: str | None = BRANCH,
    profile: str | None = None,
    scope: str = "scoped",
) -> list[dict]:
    """Call _apply_fanout_boosts with settings patched to given scope."""
    settings_obj = _pipeline_module.settings
    old_scope = getattr(settings_obj, "FANOUT_BOOST_SCOPE", "scoped")
    old_pm_boost = getattr(settings_obj, "POSTMORTEM_BOOST_FACTOR", 0.3)
    old_pm_keywords = getattr(settings_obj, "POSTMORTEM_BOOST_KEYWORDS", ())
    old_branch_weight = getattr(settings_obj, "BRANCH_BOOST_WEIGHT", 0.2)
    try:
        settings_obj.FANOUT_BOOST_SCOPE = scope  # type: ignore[attr-defined]
        # Ensure postmortem boost fires on PM_QUERY
        settings_obj.POSTMORTEM_BOOST_FACTOR = 0.3  # type: ignore[attr-defined]
        settings_obj.POSTMORTEM_BOOST_KEYWORDS = ("deploy",)  # type: ignore[attr-defined]
        settings_obj.BRANCH_BOOST_WEIGHT = 0.2  # type: ignore[attr-defined]
        return _apply_fanout_boosts(
            pooled, query=query, current_branch=current_branch, profile=profile
        )
    finally:
        settings_obj.FANOUT_BOOST_SCOPE = old_scope  # type: ignore[attr-defined]
        settings_obj.POSTMORTEM_BOOST_FACTOR = old_pm_boost  # type: ignore[attr-defined]
        settings_obj.POSTMORTEM_BOOST_KEYWORDS = old_pm_keywords  # type: ignore[attr-defined]
        settings_obj.BRANCH_BOOST_WEIGHT = old_branch_weight  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# Scope = "off"
# ---------------------------------------------------------------------------


class TestScopeOff:
    """scope="off" — boosts never fire regardless of profile."""

    def test_branch_boost_skipped_with_profile(self):
        """scope=off + profile='fast': branch boost must NOT apply."""
        mem = _make_branch_mem(score=_base_score())
        result = _call_boosts([mem], profile="fast", scope="off")
        assert result[0]["_retrieval_score"] == pytest.approx(_base_score()), (
            "scope=off must skip branch boost even when profile is set"
        )

    def test_branch_boost_skipped_without_profile(self):
        """scope=off + profile=None: branch boost must NOT apply."""
        mem = _make_branch_mem(score=_base_score())
        result = _call_boosts([mem], profile=None, scope="off")
        assert result[0]["_retrieval_score"] == pytest.approx(_base_score()), (
            "scope=off must skip branch boost when profile=None"
        )

    def test_pm_boost_skipped_with_profile(self):
        """scope=off + profile='fast': postmortem boost must NOT apply."""
        mem = _make_pm_mem(score=_base_score())
        result = _call_boosts([mem], profile="fast", scope="off")
        assert result[0]["_retrieval_score"] == pytest.approx(_base_score()), (
            "scope=off must skip postmortem boost even when profile is set"
        )

    def test_pm_boost_skipped_without_profile(self):
        """scope=off + profile=None: postmortem boost must NOT apply."""
        mem = _make_pm_mem(score=_base_score())
        result = _call_boosts([mem], profile=None, scope="off")
        assert result[0]["_retrieval_score"] == pytest.approx(_base_score()), (
            "scope=off must skip postmortem boost when profile=None"
        )

    def test_returns_same_list(self):
        """scope=off returns the pooled list unchanged (same object or equal)."""
        pooled = [_make_branch_mem(), _make_pm_mem(), _make_neutral_mem()]
        result = _call_boosts(pooled, profile="fast", scope="off")
        assert len(result) == len(pooled)
        for original, returned in zip(pooled, result, strict=False):
            assert returned["_retrieval_score"] == pytest.approx(original["_retrieval_score"])


# ---------------------------------------------------------------------------
# Scope = "scoped" (default)
# ---------------------------------------------------------------------------


class TestScopeScoped:
    """scope="scoped" — boosts apply only when profile is not None."""

    def test_branch_boost_applied_with_profile(self):
        """scope=scoped + profile='fast': branch boost MUST fire."""
        mem = _make_branch_mem(score=_base_score())
        result = _call_boosts([mem], profile="fast", scope="scoped")
        # convex combination: score + (1 - score) * weight = 0.5 + 0.5*0.2 = 0.6
        assert result[0]["_retrieval_score"] > _base_score(), (
            "scope=scoped must apply branch boost when profile is set"
        )

    def test_branch_boost_skipped_without_profile(self):
        """scope=scoped + profile=None: branch boost must NOT fire."""
        mem = _make_branch_mem(score=_base_score())
        result = _call_boosts([mem], profile=None, scope="scoped")
        assert result[0]["_retrieval_score"] == pytest.approx(_base_score()), (
            "scope=scoped must skip branch boost when profile=None"
        )

    def test_pm_boost_applied_with_profile(self):
        """scope=scoped + profile='fast': postmortem boost MUST fire."""
        mem = _make_pm_mem(score=_base_score())
        result = _call_boosts([mem], profile="fast", scope="scoped")
        assert result[0]["_retrieval_score"] > _base_score(), (
            "scope=scoped must apply postmortem boost when profile is set"
        )

    def test_pm_boost_skipped_without_profile(self):
        """scope=scoped + profile=None: postmortem boost must NOT fire."""
        mem = _make_pm_mem(score=_base_score())
        result = _call_boosts([mem], profile=None, scope="scoped")
        assert result[0]["_retrieval_score"] == pytest.approx(_base_score()), (
            "scope=scoped must skip postmortem boost when profile=None"
        )

    def test_neutral_mem_never_boosted(self):
        """Neutral memory (no branch match, no postmortem tag) never gets boosted."""
        mem = _make_neutral_mem(score=_base_score())
        for profile in ("fast", None):
            result = _call_boosts([mem], profile=profile, scope="scoped")
            assert result[0]["_retrieval_score"] == pytest.approx(_base_score()), (
                f"Neutral memory must not be boosted (profile={profile!r})"
            )

    def test_discriminator_contrast(self):
        """Prove the knob is live: same mem, profile=None skips, profile='fast' applies."""
        mem_none = _make_branch_mem(score=_base_score())
        mem_fast = _make_branch_mem(score=_base_score())
        result_none = _call_boosts([mem_none], profile=None, scope="scoped")
        result_fast = _call_boosts([mem_fast], profile="fast", scope="scoped")
        assert result_none[0]["_retrieval_score"] == pytest.approx(_base_score()), (
            "profile=None with scoped scope should NOT boost"
        )
        assert result_fast[0]["_retrieval_score"] > _base_score(), (
            "profile='fast' with scoped scope SHOULD boost"
        )


# ---------------------------------------------------------------------------
# Scope = "global"
# ---------------------------------------------------------------------------


class TestScopeGlobal:
    """scope="global" — boosts always apply regardless of profile."""

    def test_branch_boost_applied_with_profile(self):
        """scope=global + profile='fast': branch boost MUST fire."""
        mem = _make_branch_mem(score=_base_score())
        result = _call_boosts([mem], profile="fast", scope="global")
        assert result[0]["_retrieval_score"] > _base_score(), (
            "scope=global must apply branch boost when profile is set"
        )

    def test_branch_boost_applied_without_profile(self):
        """scope=global + profile=None: branch boost MUST ALSO fire."""
        mem = _make_branch_mem(score=_base_score())
        result = _call_boosts([mem], profile=None, scope="global")
        assert result[0]["_retrieval_score"] > _base_score(), (
            "scope=global must apply branch boost even when profile=None"
        )

    def test_pm_boost_applied_with_profile(self):
        """scope=global + profile='fast': postmortem boost MUST fire."""
        mem = _make_pm_mem(score=_base_score())
        result = _call_boosts([mem], profile="fast", scope="global")
        assert result[0]["_retrieval_score"] > _base_score(), (
            "scope=global must apply postmortem boost when profile is set"
        )

    def test_pm_boost_applied_without_profile(self):
        """scope=global + profile=None: postmortem boost MUST ALSO fire."""
        mem = _make_pm_mem(score=_base_score())
        result = _call_boosts([mem], profile=None, scope="global")
        assert result[0]["_retrieval_score"] > _base_score(), (
            "scope=global must apply postmortem boost even when profile=None"
        )

    def test_global_vs_off_contrast(self):
        """Prove global and off are distinct: same input, different outcomes."""
        mem_global = _make_branch_mem(score=_base_score())
        mem_off = _make_branch_mem(score=_base_score())
        result_global = _call_boosts([mem_global], profile=None, scope="global")
        result_off = _call_boosts([mem_off], profile=None, scope="off")
        assert result_global[0]["_retrieval_score"] > result_off[0]["_retrieval_score"], (
            "scope=global (profile=None) must score higher than scope=off"
        )


# ---------------------------------------------------------------------------
# Config: FANOUT_BOOST_SCOPE exists in Settings with correct default
# ---------------------------------------------------------------------------


class TestFanoutBoostScopeConfig:
    """Settings must expose FANOUT_BOOST_SCOPE with default 'scoped'."""

    def test_setting_exists_with_default(self):
        """Settings.FANOUT_BOOST_SCOPE default must be 'scoped'."""
        from yadgar.config import Settings

        default = Settings.model_fields["FANOUT_BOOST_SCOPE"].default
        assert default == "scoped", f"FANOUT_BOOST_SCOPE default must be 'scoped', got {default!r}"

    def test_module_level_settings_has_attribute(self):
        """Module-level settings singleton must expose FANOUT_BOOST_SCOPE."""
        import yadgar.server.tools._recall_pipeline as pipe

        assert hasattr(pipe.settings, "FANOUT_BOOST_SCOPE"), (
            "settings singleton must have FANOUT_BOOST_SCOPE attribute"
        )
        assert pipe.settings.FANOUT_BOOST_SCOPE == "scoped"
