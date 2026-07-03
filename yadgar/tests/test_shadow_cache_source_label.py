"""v5.100.0 — source label (hook|tool) on shadow recall-cache counters.

TDD: these tests are written BEFORE the implementation and must fail on the
pre-5.100 codebase.  The two failure points on the old code are:

1. ``RecallShadowParams`` has no ``source`` field
   → constructing it with ``source=...`` raises TypeError.
2. The counters have no labels
   → ``.labels(source=...)`` raises ValueError ("Inconsistent label cardinality").

After the implementation the suite must be fully green.

Invariant being tested: the #88 output-cache gating decision should be
measurable on tool-path traffic only.  Hook auto-recalls (3 endpoints) fire
50-200 times/hour per session and would inflate the would-be hit-rate when
mixed with explicit MCP-tool recalls.  The ``source`` label lets Prometheus
queries filter independently.
"""

from __future__ import annotations

import asyncio

import pytest

# ---------------------------------------------------------------------------
# Shared fixtures / helpers
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_shadow():
    from yadgar.server.tools import _recall_shadow

    _recall_shadow._reset_for_test()
    yield
    _recall_shadow._reset_for_test()


def _label_count(counter, source: str) -> float:
    """Return the current value of counter.labels(source=source)."""
    return counter.labels(source=source)._value.get()


def _observe_source(source: str, **overrides):
    from yadgar.server.tools._recall_shadow import RecallShadowParams, observe_recall

    kwargs = {
        "query": "deploy runbook",
        "directory": "/home/u/proj",
        "branch": "main",
        "type_filter": "all",
        "mode": None,
        "profile": None,
        "max_results": 5,
        "min_heat": 0.0,
        "tags": None,
        "source": source,
    }
    kwargs.update(overrides)
    observe_recall(RecallShadowParams(**kwargs))


# ---------------------------------------------------------------------------
# Unit tests — label routing
# ---------------------------------------------------------------------------


class TestSourceLabel:
    def test_hook_first_observe_is_miss_under_hook_label(self):
        from yadgar.metrics import yadgar_recall_shadow_cache_misses_total

        m0 = _label_count(yadgar_recall_shadow_cache_misses_total, "hook")
        _observe_source("hook")
        assert _label_count(yadgar_recall_shadow_cache_misses_total, "hook") == m0 + 1

    def test_tool_first_observe_is_miss_under_tool_label(self):
        from yadgar.metrics import yadgar_recall_shadow_cache_misses_total

        m0 = _label_count(yadgar_recall_shadow_cache_misses_total, "tool")
        _observe_source("tool")
        assert _label_count(yadgar_recall_shadow_cache_misses_total, "tool") == m0 + 1

    def test_hook_hit_increments_hook_hits_label(self):
        from yadgar.metrics import yadgar_recall_shadow_cache_hits_total

        _observe_source("hook")  # miss
        h0 = _label_count(yadgar_recall_shadow_cache_hits_total, "hook")
        _observe_source("hook")  # hit (same key, same epoch)
        assert _label_count(yadgar_recall_shadow_cache_hits_total, "hook") == h0 + 1

    def test_tool_hit_increments_tool_hits_label(self):
        from yadgar.metrics import yadgar_recall_shadow_cache_hits_total

        _observe_source("tool")  # miss
        h0 = _label_count(yadgar_recall_shadow_cache_hits_total, "tool")
        _observe_source("tool")  # hit
        assert _label_count(yadgar_recall_shadow_cache_hits_total, "tool") == h0 + 1

    def test_hook_traffic_does_not_affect_tool_counters(self):
        """Hook misses and hits must not bleed into the tool-labelled counters."""
        from yadgar.metrics import (
            yadgar_recall_shadow_cache_hits_total,
            yadgar_recall_shadow_cache_misses_total,
        )

        # Capture tool baseline before any hook traffic
        tool_m0 = _label_count(yadgar_recall_shadow_cache_misses_total, "tool")
        tool_h0 = _label_count(yadgar_recall_shadow_cache_hits_total, "tool")

        _observe_source("hook")  # miss for hook
        _observe_source("hook")  # hit for hook

        # Tool counters must be unchanged
        assert _label_count(yadgar_recall_shadow_cache_misses_total, "tool") == tool_m0
        assert _label_count(yadgar_recall_shadow_cache_hits_total, "tool") == tool_h0

    def test_tool_traffic_does_not_affect_hook_counters(self):
        """Tool misses and hits must not bleed into the hook-labelled counters."""
        from yadgar.metrics import (
            yadgar_recall_shadow_cache_hits_total,
            yadgar_recall_shadow_cache_misses_total,
        )

        # Capture hook baseline before any tool traffic
        hook_m0 = _label_count(yadgar_recall_shadow_cache_misses_total, "hook")
        hook_h0 = _label_count(yadgar_recall_shadow_cache_hits_total, "hook")

        _observe_source("tool")  # miss for tool
        _observe_source("tool")  # hit for tool

        # Hook counters must be unchanged
        assert _label_count(yadgar_recall_shadow_cache_misses_total, "hook") == hook_m0
        assert _label_count(yadgar_recall_shadow_cache_hits_total, "hook") == hook_h0

    def test_same_query_hook_and_tool_are_independent_keyspaces(self):
        """A tool call for query Q must not register as a hook hit for query Q.

        source is part of the shadow key — hooks and tools have independent
        would-be cache keyspaces.  This is correct: the hypothetical cache for
        issue #88 would serve only explicit tool recalls.
        """
        from yadgar.metrics import (
            yadgar_recall_shadow_cache_hits_total,
            yadgar_recall_shadow_cache_misses_total,
        )

        same_kwargs = {"query": "same query", "directory": "/tmp/shared"}

        # Capture baselines before any calls
        tool_h0 = _label_count(yadgar_recall_shadow_cache_hits_total, "tool")
        hook_m0 = _label_count(yadgar_recall_shadow_cache_misses_total, "hook")

        _observe_source("tool", **same_kwargs)  # tool miss (records key in tool keyspace)

        _observe_source("hook", **same_kwargs)  # hook: independent keyspace → MISS (not hit)
        assert _label_count(yadgar_recall_shadow_cache_misses_total, "hook") == hook_m0 + 1, (
            "hook call for same query must be a MISS (independent keyspace from tool)"
        )
        assert _label_count(yadgar_recall_shadow_cache_hits_total, "tool") == tool_h0, (
            "tool hit counter must not increment when hook call fires"
        )

    def test_source_required_no_silent_unknown(self):
        """RecallShadowParams must have an explicit source field — no silent default."""
        from yadgar.server.tools._recall_shadow import RecallShadowParams

        # Must be constructable with explicit source
        p = RecallShadowParams(
            query="q",
            directory=None,
            branch=None,
            type_filter="all",
            mode=None,
            profile=None,
            max_results=5,
            min_heat=0.0,
            tags=None,
            source="tool",
        )
        assert p.source == "tool"

        # Without source the constructor must raise TypeError (required field)
        with pytest.raises(TypeError):
            RecallShadowParams(
                query="q",
                directory=None,
                branch=None,
                type_filter="all",
                mode=None,
                profile=None,
                max_results=5,
                min_heat=0.0,
                tags=None,
                # source intentionally omitted
            )


# ---------------------------------------------------------------------------
# Wiring — real hook_prompt_recall must record source="hook"
# ---------------------------------------------------------------------------


def _call_real_hook_prompt_recall(query: str, directory: str):
    """Drive the real hook_prompt_recall endpoint with a mocked request.

    Patches _recall_with_timeout to return [] immediately (so the hook takes
    no real retrieval work and returns {"text": ""}), but the shadow observe
    fires before the retriever call.
    """
    from unittest.mock import AsyncMock, MagicMock, patch

    import yadgar.server._state as _st
    from yadgar.server.http import hook_prompt_recall

    mock_request = MagicMock()
    params_dict = {"query": query, "directory": directory}
    mock_request.query_params.get.side_effect = lambda k, d="": params_dict.get(k, d)

    async def _run():
        with (
            patch.object(_st, "_retriever", MagicMock()),
            patch.object(_st, "_last_session_context", {}),
            patch.object(_st, "_last_prompt_recall", {}),
            patch("yadgar.server.http._recall_with_timeout", new=AsyncMock(return_value=[])),
        ):
            return await hook_prompt_recall(mock_request)

    return asyncio.run(_run())


class TestHookWiring:
    def test_hook_prompt_recall_records_hook_source_miss(self):
        """hook_prompt_recall must call observe_recall with source='hook'."""
        from yadgar.metrics import yadgar_recall_shadow_cache_misses_total

        m0 = _label_count(yadgar_recall_shadow_cache_misses_total, "hook")
        _call_real_hook_prompt_recall("wiring probe", "/tmp/wiring_hook_proj")
        m1 = _label_count(yadgar_recall_shadow_cache_misses_total, "hook")
        assert m1 == m0 + 1, (
            "hook_prompt_recall must invoke observe_recall(source='hook') — first call is a MISS"
        )

    def test_hook_prompt_recall_records_hook_source_hit(self):
        """Second identical hook recall at same epoch must register as source='hook' HIT."""
        from yadgar.metrics import yadgar_recall_shadow_cache_hits_total

        _call_real_hook_prompt_recall("wiring probe", "/tmp/wiring_hook_proj")  # miss
        h0 = _label_count(yadgar_recall_shadow_cache_hits_total, "hook")
        _call_real_hook_prompt_recall("wiring probe", "/tmp/wiring_hook_proj")  # hit
        h1 = _label_count(yadgar_recall_shadow_cache_hits_total, "hook")
        assert h1 == h0 + 1, (
            "identical hook_prompt_recall at same epoch must record source='hook' HIT"
        )

    def test_hook_miss_does_not_increment_tool_counter(self):
        """A hook-path recall must not touch the tool-labelled counters."""
        from yadgar.metrics import (
            yadgar_recall_shadow_cache_hits_total,
            yadgar_recall_shadow_cache_misses_total,
        )

        tool_m0 = _label_count(yadgar_recall_shadow_cache_misses_total, "tool")
        tool_h0 = _label_count(yadgar_recall_shadow_cache_hits_total, "tool")

        _call_real_hook_prompt_recall("wiring probe", "/tmp/wiring_hook_proj")
        _call_real_hook_prompt_recall("wiring probe", "/tmp/wiring_hook_proj")

        assert _label_count(yadgar_recall_shadow_cache_misses_total, "tool") == tool_m0
        assert _label_count(yadgar_recall_shadow_cache_hits_total, "tool") == tool_h0


# ---------------------------------------------------------------------------
# Wiring — real recall() MCP tool must record source="tool"
# ---------------------------------------------------------------------------


def _call_real_tool_recall(query: str, directory: str):
    from unittest.mock import MagicMock, patch

    import yadgar.server._state as _st
    from yadgar.server.tools.recall import recall as recall_fn

    mock_retriever = MagicMock()
    mock_retriever.recall.return_value = []
    mock_storage = MagicMock()
    mock_storage._now_iso.return_value = "2026-01-01T00:00:00"
    mock_wiki = MagicMock()
    mock_wiki.query.return_value = []

    with (
        patch.object(_st, "_retriever", mock_retriever),
        patch.object(_st, "_storage", mock_storage),
        patch.object(_st, "_consolidation", None),
        patch.object(_st, "_thermo", None),
        patch.object(_st, "_cognitive_map", None),
        patch.object(_st, "_buffer", None),
        patch.object(_st, "_replay", None),
        patch.object(_st, "_wiki", mock_wiki),
        patch.object(_st, "_last_recalled_ids", {}),
        patch("yadgar.server.tools.project._detect_branch", return_value=None),
        patch("yadgar.server.tools.project._get_default_branch", return_value="master"),
    ):
        return recall_fn(query=query, max_results=5, min_heat=0.0, directory=directory)


class TestToolWiring:
    def test_tool_recall_records_tool_source_miss(self):
        from yadgar.metrics import yadgar_recall_shadow_cache_misses_total

        m0 = _label_count(yadgar_recall_shadow_cache_misses_total, "tool")
        _call_real_tool_recall("unique tool wiring probe", "/tmp/wiring_tool_proj")
        m1 = _label_count(yadgar_recall_shadow_cache_misses_total, "tool")
        assert m1 == m0 + 1, (
            "recall() must invoke observe_recall(source='tool') — MISS on first sight"
        )

    def test_tool_recall_records_tool_source_hit(self):
        from yadgar.metrics import yadgar_recall_shadow_cache_hits_total

        _call_real_tool_recall("unique tool wiring probe", "/tmp/wiring_tool_proj")  # miss
        h0 = _label_count(yadgar_recall_shadow_cache_hits_total, "tool")
        _call_real_tool_recall("unique tool wiring probe", "/tmp/wiring_tool_proj")  # hit
        h1 = _label_count(yadgar_recall_shadow_cache_hits_total, "tool")
        assert h1 == h0 + 1, "identical recall() at same epoch must be source='tool' HIT"

    def test_tool_miss_does_not_increment_hook_counter(self):
        from yadgar.metrics import (
            yadgar_recall_shadow_cache_hits_total,
            yadgar_recall_shadow_cache_misses_total,
        )

        hook_m0 = _label_count(yadgar_recall_shadow_cache_misses_total, "hook")
        hook_h0 = _label_count(yadgar_recall_shadow_cache_hits_total, "hook")

        _call_real_tool_recall("unique tool wiring probe", "/tmp/wiring_tool_proj")
        _call_real_tool_recall("unique tool wiring probe", "/tmp/wiring_tool_proj")

        assert _label_count(yadgar_recall_shadow_cache_misses_total, "hook") == hook_m0
        assert _label_count(yadgar_recall_shadow_cache_hits_total, "hook") == hook_h0
