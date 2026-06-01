"""v5.31.1 — MCP recall() pipeline kwargs tests (Item 2).

Covers:
  1. profile=None (default) routes through legacy retriever.recall() path.
  2. profile="balanced" routes through retriever.recall_via_pipeline().
  3. Invalid profile raises ValidationError BEFORE any retrieval work.
  4. stage_overrides passed through to recall_via_pipeline().
  5. Pipeline metrics (yadgar_recall_profile_invocations_total) increment on profile call.
  6. Zero behavior change: existing callers (no profile) unaffected.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Helpers (minimal copies from test_recall_wiki_metrics pattern)
# ---------------------------------------------------------------------------


def _make_fake_memory(mid: int = 1) -> dict:
    return {
        "id": mid,
        "content": f"memory {mid}",
        "heat": 0.5,
        "tags": [],
        "branch": None,
        "_retrieval_score": 0.5,
    }


def _make_mock_storage() -> Any:
    storage = MagicMock()
    mems = [_make_fake_memory(1)]
    storage.search_memories_fts.return_value = mems
    storage.search_vectors.return_value = []
    storage.get_memory.return_value = mems[0]
    storage._now_iso.return_value = "2026-01-01T00:00:00"
    storage.update_memory_heat.return_value = None
    storage.update_memory_last_accessed.return_value = None
    return storage


def _make_mock_retriever() -> Any:
    retriever = MagicMock()
    retriever.recall.return_value = [_make_fake_memory(1)]
    retriever.recall_via_pipeline.return_value = [_make_fake_memory(2)]
    return retriever


def _call_recall(query: str = "test query", profile=None, stage_overrides=None, **kwargs):
    """Call the MCP recall tool directly with mocked server state."""
    import yadgar.server._state as _st
    from yadgar.server.tools.recall import recall as recall_fn

    mock_retriever = _make_mock_retriever()
    mock_storage = _make_mock_storage()

    with (
        patch.object(_st, "_retriever", mock_retriever),
        patch.object(_st, "_storage", mock_storage),
        patch.object(_st, "_consolidation", None),
        patch.object(_st, "_thermo", None),
        patch.object(_st, "_cognitive_map", None),
        patch.object(_st, "_buffer", None),
        patch.object(_st, "_replay", None),
        patch.object(_st, "_wiki", None),
        patch.object(_st, "_last_recalled_ids", {}),
        patch("yadgar.server.tools.project._detect_branch", return_value=None),
        patch("yadgar.server.tools.project._get_default_branch", return_value="master"),
    ):
        call_kwargs: dict = {"query": query}
        if profile is not None:
            call_kwargs["profile"] = profile
        if stage_overrides is not None:
            call_kwargs["stage_overrides"] = stage_overrides
        call_kwargs.update(kwargs)
        result = recall_fn(**call_kwargs)
    return result, mock_retriever


def _count_labeled(metric, **label_filter) -> float:
    """Read _count from a labeled Counter matching given label values."""
    total = 0.0
    for fam in metric.collect():
        for s in fam.samples:
            if not s.name.endswith("_total"):
                continue
            if all(s.labels.get(k) == v for k, v in label_filter.items()):
                total += s.value
    return total


# ---------------------------------------------------------------------------
# Test classes
# ---------------------------------------------------------------------------


class TestRecallProfileNone:
    """profile=None (default) → legacy retriever.recall() path."""

    def test_no_profile_calls_legacy_recall(self):
        """Omitting profile routes through retriever.recall(), not recall_via_pipeline."""
        result, mock_retriever = _call_recall(query="semantic memory search")
        mock_retriever.recall.assert_called_once()
        mock_retriever.recall_via_pipeline.assert_not_called()

    def test_explicit_none_calls_legacy_recall(self):
        """Passing profile=None explicitly also routes to legacy path."""
        result, mock_retriever = _call_recall(query="test", profile=None)
        mock_retriever.recall.assert_called_once()
        mock_retriever.recall_via_pipeline.assert_not_called()

    def test_zero_behavior_change_for_no_profile(self):
        """Legacy path result is returned unchanged."""
        result, mock_retriever = _call_recall(query="test")
        # recall() returns [_make_fake_memory(1)]
        assert any(m["id"] == 1 for m in result)


class TestRecallProfileBalanced:
    """profile='balanced' → recall_via_pipeline() path."""

    def test_profile_balanced_routes_to_pipeline(self):
        """Setting profile='balanced' calls recall_via_pipeline, not legacy recall."""
        result, mock_retriever = _call_recall(query="semantic search", profile="balanced")
        mock_retriever.recall_via_pipeline.assert_called_once()
        mock_retriever.recall.assert_not_called()

    def test_profile_fast_routes_to_pipeline(self):
        """Setting profile='fast' calls recall_via_pipeline."""
        result, mock_retriever = _call_recall(query="fast search", profile="fast")
        mock_retriever.recall_via_pipeline.assert_called_once()

    def test_profile_full_routes_to_pipeline(self):
        result, mock_retriever = _call_recall(query="full search", profile="full")
        mock_retriever.recall_via_pipeline.assert_called_once()

    def test_profile_debug_routes_to_pipeline(self):
        result, mock_retriever = _call_recall(query="debug search", profile="debug")
        mock_retriever.recall_via_pipeline.assert_called_once()

    def test_profile_kwarg_passed_to_pipeline(self):
        """profile name is forwarded to recall_via_pipeline(profile=...)."""
        _call_recall(query="test", profile="balanced")
        # Can't easily check mock_retriever here without returning it; use mock inspection pattern
        _, mock_retriever = _call_recall(query="test", profile="fast")
        call_kwargs = mock_retriever.recall_via_pipeline.call_args
        assert call_kwargs.kwargs.get("profile") == "fast" or (
            len(call_kwargs.args) >= 2 and call_kwargs.args[1] == "fast"
        )

    def test_stage_overrides_passed_to_pipeline(self):
        """stage_overrides dict forwarded to recall_via_pipeline(stage_overrides=...)."""
        overrides = {"nli": {"enabled": False}, "ce_rerank": {"enabled": False}}
        _, mock_retriever = _call_recall(
            query="test", profile="balanced", stage_overrides=overrides
        )
        call_kw = mock_retriever.recall_via_pipeline.call_args
        assert call_kw.kwargs.get("stage_overrides") == overrides

    def test_stage_overrides_none_when_not_provided(self):
        """stage_overrides=None forwarded (or omitted) when not set."""
        _, mock_retriever = _call_recall(query="test", profile="balanced")
        call_kw = mock_retriever.recall_via_pipeline.call_args
        overrides = call_kw.kwargs.get("stage_overrides")
        assert overrides is None or overrides == {}


class TestRecallInvalidProfile:
    """Invalid profile raises ValidationError BEFORE retrieval work."""

    def test_invalid_profile_raises_validation_error(self):
        """Unknown profile name raises ValueError before any retrieval."""
        import yadgar.server._state as _st
        from yadgar.server.tools.recall import recall as recall_fn

        mock_retriever = _make_mock_retriever()
        mock_storage = _make_mock_storage()

        with (
            patch.object(_st, "_retriever", mock_retriever),
            patch.object(_st, "_storage", mock_storage),
            patch.object(_st, "_consolidation", None),
            patch.object(_st, "_thermo", None),
            patch.object(_st, "_cognitive_map", None),
            patch.object(_st, "_buffer", None),
            patch.object(_st, "_replay", None),
            patch.object(_st, "_wiki", None),
            patch.object(_st, "_last_recalled_ids", {}),
            patch("yadgar.server.tools.project._detect_branch", return_value=None),
            patch("yadgar.server.tools.project._get_default_branch", return_value="master"),
        ):
            with pytest.raises((ValueError, Exception)) as exc_info:
                recall_fn(query="test", profile="turbo-ultra-hyper")
            assert (
                "turbo-ultra-hyper" in str(exc_info.value).lower()
                or "unknown" in str(exc_info.value).lower()
                or "valid" in str(exc_info.value).lower()
            )

    def test_invalid_profile_no_retriever_called(self):
        """retriever.recall and recall_via_pipeline must NOT be called for invalid profile."""
        import yadgar.server._state as _st
        from yadgar.server.tools.recall import recall as recall_fn

        mock_retriever = _make_mock_retriever()
        mock_storage = _make_mock_storage()

        with (
            patch.object(_st, "_retriever", mock_retriever),
            patch.object(_st, "_storage", mock_storage),
            patch.object(_st, "_consolidation", None),
            patch.object(_st, "_thermo", None),
            patch.object(_st, "_cognitive_map", None),
            patch.object(_st, "_buffer", None),
            patch.object(_st, "_replay", None),
            patch.object(_st, "_wiki", None),
            patch.object(_st, "_last_recalled_ids", {}),
            patch("yadgar.server.tools.project._detect_branch", return_value=None),
            patch("yadgar.server.tools.project._get_default_branch", return_value="master"),
        ):
            try:
                recall_fn(query="test", profile="bogus_profile")
            except Exception:
                pass
            mock_retriever.recall.assert_not_called()
            mock_retriever.recall_via_pipeline.assert_not_called()


class TestRecallPipelineMetrics:
    """yadgar_recall_profile_invocations_total increments when profile is set."""

    def test_profile_invocations_counter_increments(self):
        from yadgar.metrics import yadgar_recall_profile_invocations_total

        before = _count_labeled(yadgar_recall_profile_invocations_total, profile="balanced")
        _call_recall(query="metric test", profile="balanced")
        after = _count_labeled(yadgar_recall_profile_invocations_total, profile="balanced")
        assert after > before, "yadgar_recall_profile_invocations_total should increment"

    def test_no_profile_does_not_increment_profile_counter(self):
        """Legacy path (profile=None) does not bump profile invocations counter."""
        from yadgar.metrics import yadgar_recall_profile_invocations_total

        # Collect totals across all labels before
        before_total = sum(
            s.value
            for fam in yadgar_recall_profile_invocations_total.collect()
            for s in fam.samples
            if s.name.endswith("_total")
        )
        _call_recall(query="no profile call")
        after_total = sum(
            s.value
            for fam in yadgar_recall_profile_invocations_total.collect()
            for s in fam.samples
            if s.name.endswith("_total")
        )
        # Counter must not have incremented
        assert after_total == before_total, "Profile counter should not increment when profile=None"
