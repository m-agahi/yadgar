"""Phase 2a — MCP recall() forward-only kwargs tests.

Covers (rewritten from v5.31.1 plugin-pipeline routing tests):
  1. profile=None → _forward_to_backend called with profile=None (no retriever.recall_via_pipeline).
  2. profile="balanced" → _forward_to_backend called with profile="balanced" in payload.
  3. profile="fast"/"full"/"debug" → forwarded verbatim.
  4. Invalid profile raises ValueError BEFORE any retrieval work.
  5. stage_overrides REMOVED from MCP tool (#58): param gone from signature + docstring.
  6. profile=None (default) — counter not incremented (plugin pipeline removed).
"""

from __future__ import annotations

import sys
from unittest.mock import patch

import pytest

import yadgar.core.server.tools.recall as _recall_symbol  # noqa: F401 — side-effects

_recall_module = sys.modules["yadgar.core.server.tools.recall"]


def _make_fake_memory(mid: int = 1) -> dict:
    return {
        "id": mid,
        "content": f"memory {mid}",
        "heat": 0.5,
        "tags": [],
        "branch": None,
        "_retrieval_score": 0.5,
    }


def _call_recall(query: str = "test query", profile=None, **kwargs):
    """Call the MCP recall tool directly with _forward_to_backend mocked out.

    Returns (result, captured_call_kwargs) where captured_call_kwargs is the
    kwargs passed to _forward_to_backend so callers can assert on profile/mode.
    """
    from yadgar.core.server.tools.recall import recall as recall_fn

    fake_results = [_make_fake_memory(1)]
    captured = {}

    def _spy_forward(**kw):
        captured.update(kw)
        return fake_results

    with (
        patch.object(_recall_module, "_forward_to_backend", side_effect=_spy_forward),
        patch.object(_recall_module, "_apply_recall_session_side_effects"),
        patch.object(_recall_module, "_st") as mock_st,
        patch("yadgar.core.server.tools.project._detect_branch", return_value=None),
        patch("yadgar.core.server.tools.project._get_default_branch", return_value="master"),
    ):
        mock_st._consolidation = None
        mock_st._pool = None

        call_kwargs: dict = {"query": query, "directory": "/tmp/test"}
        if profile is not None:
            call_kwargs["profile"] = profile
        call_kwargs.update(kwargs)
        result = recall_fn(**call_kwargs)

    return result, captured


# ---------------------------------------------------------------------------
# 1. profile=None (default) → forwarded as profile=None
# ---------------------------------------------------------------------------


class TestRecallProfileNone:
    """profile=None (default) → _forward_to_backend called with profile=None."""

    def test_no_profile_forwarded_as_none(self):
        """Omitting profile calls _forward_to_backend with profile=None."""
        result, captured = _call_recall(query="semantic memory search")
        assert "profile" in captured, "_forward_to_backend must be called with profile kwarg"
        assert captured["profile"] is None

    def test_explicit_none_forwarded_as_none(self):
        """Passing profile=None explicitly is also forwarded as None."""
        result, captured = _call_recall(query="test", profile=None)
        assert captured.get("profile") is None

    def test_zero_behavior_change_for_no_profile(self):
        """forward-only path returns results from backend."""
        result, _ = _call_recall(query="test")
        assert any(m["id"] == 1 for m in result)


# ---------------------------------------------------------------------------
# 2. profile="balanced" / "fast" / "full" / "debug" → forwarded verbatim
# ---------------------------------------------------------------------------


class TestRecallProfileForwarded:
    """profile=X → _forward_to_backend called with profile=X in payload."""

    def test_profile_balanced_forwarded(self):
        """Setting profile='balanced' is forwarded verbatim."""
        _, captured = _call_recall(query="semantic search", profile="balanced")
        assert captured.get("profile") == "balanced"

    def test_profile_fast_forwarded(self):
        _, captured = _call_recall(query="fast search", profile="fast")
        assert captured.get("profile") == "fast"

    def test_profile_full_forwarded(self):
        _, captured = _call_recall(query="full search", profile="full")
        assert captured.get("profile") == "full"

    def test_profile_debug_forwarded(self):
        _, captured = _call_recall(query="debug search", profile="debug")
        assert captured.get("profile") == "debug"

    def test_profile_kwarg_value_forwarded(self):
        """profile value reaches _forward_to_backend intact."""
        _, captured = _call_recall(query="test", profile="fast")
        assert captured["profile"] == "fast"


# ---------------------------------------------------------------------------
# 3. Invalid profile raises ValueError BEFORE forwarding
# ---------------------------------------------------------------------------


class TestRecallInvalidProfile:
    """Invalid profile raises ValueError BEFORE _forward_to_backend is called."""

    def test_invalid_profile_raises_validation_error(self):
        """Unknown profile name raises ValueError before any retrieval."""
        from yadgar.core.server.tools.recall import recall as recall_fn

        with (
            patch.object(_recall_module, "_forward_to_backend") as mock_fwd,
            patch.object(_recall_module, "_st") as mock_st,
            patch("yadgar.core.server.tools.project._detect_branch", return_value=None),
            patch("yadgar.core.server.tools.project._get_default_branch", return_value="master"),
        ):
            mock_st._consolidation = None
            mock_st._pool = None

            with pytest.raises((ValueError, Exception)) as exc_info:
                recall_fn(query="test", profile="turbo-ultra-hyper", directory="/tmp/test")
            assert (
                "turbo-ultra-hyper" in str(exc_info.value).lower()
                or "unknown" in str(exc_info.value).lower()
                or "valid" in str(exc_info.value).lower()
            )
            # _forward_to_backend must NOT have been called
            mock_fwd.assert_not_called()

    def test_invalid_profile_no_forward_called(self):
        """_forward_to_backend must NOT be called for invalid profile."""
        from yadgar.core.server.tools.recall import recall as recall_fn

        with (
            patch.object(_recall_module, "_forward_to_backend") as mock_fwd,
            patch.object(_recall_module, "_st") as mock_st,
            patch("yadgar.core.server.tools.project._detect_branch", return_value=None),
            patch("yadgar.core.server.tools.project._get_default_branch", return_value="master"),
        ):
            mock_st._consolidation = None
            mock_st._pool = None

            try:
                recall_fn(query="test", profile="bogus_profile", directory="/tmp/test")
            except Exception:
                pass
            mock_fwd.assert_not_called()


# ---------------------------------------------------------------------------
# 4. profile=None counter: plugin pipeline metric no longer fires
# ---------------------------------------------------------------------------


class TestRecallPipelineMetrics:
    """Phase 2a: plugin pipeline counter (yadgar_recall_profile_invocations_total)
    is no longer incremented — forward-only path does not run the plugin pipeline.
    """

    def test_no_profile_does_not_increment_profile_counter(self):
        """Profile=None does not bump profile invocations counter (no plugin pipeline)."""
        from yadgar._shared.observability.metrics import yadgar_recall_profile_invocations_total

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
        assert after_total == before_total, "Profile counter must not increment when profile=None"

    def test_profile_set_does_not_increment_plugin_pipeline_counter(self):
        """Phase 2a: profile= is forwarded to backend; plugin pipeline counter NOT fired."""
        from yadgar._shared.observability.metrics import yadgar_recall_profile_invocations_total

        before = sum(
            s.value
            for fam in yadgar_recall_profile_invocations_total.collect()
            for s in fam.samples
            if s.name.endswith("_total")
        )
        _call_recall(query="metric test", profile="balanced")
        after = sum(
            s.value
            for fam in yadgar_recall_profile_invocations_total.collect()
            for s in fam.samples
            if s.name.endswith("_total")
        )
        # Plugin pipeline counter must NOT fire (forward-only; backend handles profiling)
        assert after == before, (
            "Plugin pipeline counter must not increment in forward-only mode; "
            "profile is forwarded to backend"
        )


# ---------------------------------------------------------------------------
# #58 AC tests — stage_overrides removed from MCP tool (AUDITED-ready plan)
# ---------------------------------------------------------------------------


class TestStageOverridesRemoved:
    """AC-1/AC-2/AC-4: stage_overrides gone from recall() MCP tool signature + docstring.

    Plan: docs/plans/recall-stage-overrides-2026-07-13.md
    Decision D1 = REMOVE-PARAM (user-chosen): the param was never wired
    and targets a dead test-only consumption path (recall_via_pipeline).
    """

    def test_stage_overrides_not_in_recall_signature(self):
        """AC-1: stage_overrides is NOT a parameter of the recall() MCP tool."""
        import inspect

        from yadgar.core.server.tools.recall import recall as recall_fn

        sig = inspect.signature(recall_fn)
        assert "stage_overrides" not in sig.parameters, (
            "stage_overrides must be removed from recall() MCP tool signature (#58)"
        )

    def test_stage_overrides_not_in_recall_docstring(self):
        """AC-2: stage_overrides is NOT mentioned in the recall() docstring."""
        from yadgar.core.server.tools.recall import recall as recall_fn

        doc = recall_fn.__doc__ or ""
        assert "stage_overrides" not in doc, (
            "stage_overrides must be removed from recall() docstring (#58)"
        )

    def test_recall_rejects_stage_overrides_kwarg(self):
        """AC-1 enforcement: passing stage_overrides to recall() raises TypeError."""
        from unittest.mock import patch

        from yadgar.core.server.tools.recall import recall as recall_fn

        with (
            patch.object(_recall_module, "_forward_to_backend", return_value=[]),
            patch.object(_recall_module, "_apply_recall_session_side_effects"),
            patch.object(_recall_module, "_st") as mock_st,
            patch("yadgar.core.server.tools.project._detect_branch", return_value=None),
            patch("yadgar.core.server.tools.project._get_default_branch", return_value="master"),
        ):
            mock_st._consolidation = None
            mock_st._pool = None

            with pytest.raises(TypeError):
                recall_fn(
                    query="test",
                    directory="/tmp/test",
                    stage_overrides={"nli": {"enabled": False}},
                )

    def test_call_recall_helper_no_stage_overrides_param(self):
        """AC-3 / helper hygiene: _call_recall helper no longer accepts stage_overrides."""
        import inspect

        # After cleanup, the _call_recall helper should not have stage_overrides param.
        sig = inspect.signature(_call_recall)
        assert "stage_overrides" not in sig.parameters, (
            "_call_recall helper must have stage_overrides param removed (#58 hygiene)"
        )
