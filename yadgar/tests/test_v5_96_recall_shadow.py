"""v5.96.0 — shadow recall result-cache hit-rate counter (instrumentation only).

The shadow counter measures the hit-rate a hypothetical query→output cache WOULD
achieve — it caches nothing and changes no recall behaviour.  These tests guard the
core state machine:

1. same key + same epoch → would-HIT (second observe).
2. after an epoch bump for that directory → would-MISS.
3. a brand-new key → would-MISS.
4. the observe / bump paths never raise (instrumentation must never break recall).
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.usefixtures("recall_backend_bypass")


@pytest.fixture(autouse=True)
def _reset_shadow():
    from yadgar.server.tools import _recall_shadow

    _recall_shadow._reset_for_test()
    yield
    _recall_shadow._reset_for_test()


def _counts():
    # v5.100.0: counters are labelled; tests use source="tool" (the default tool path).
    from yadgar.metrics import (
        yadgar_recall_shadow_cache_hits_total,
        yadgar_recall_shadow_cache_misses_total,
    )

    return (
        yadgar_recall_shadow_cache_hits_total.labels(source="tool")._value.get(),
        yadgar_recall_shadow_cache_misses_total.labels(source="tool")._value.get(),
    )


def _observe(**overrides):
    from yadgar.server.tools._recall_shadow import RecallShadowParams, observe_recall

    kwargs = {
        "query": "what is the deploy runbook",
        "directory": "/home/u/proj",
        "branch": "main",
        "type_filter": "all",
        "mode": None,
        "profile": None,
        "max_results": 5,
        "min_heat": 0.0,
        "tags": None,
        "source": "tool",  # v5.100.0: required field; existing tests exercise tool path
    }
    kwargs.update(overrides)
    observe_recall(RecallShadowParams(**kwargs))


class TestShadowCounter:
    def test_first_observe_is_miss(self):
        h0, m0 = _counts()
        _observe()
        h1, m1 = _counts()
        assert m1 == m0 + 1, "first sighting of a key must be a MISS"
        assert h1 == h0, "no hit on first sighting"

    def test_same_key_same_epoch_is_hit(self):
        _observe()  # miss (records key)
        h0, m0 = _counts()
        _observe()  # identical → hit
        h1, m1 = _counts()
        assert h1 == h0 + 1, "identical key at same epoch must be a HIT"
        assert m1 == m0, "no extra miss on the hit"

    def test_epoch_bump_forces_miss(self):
        from yadgar.server.tools._recall_shadow import bump_epoch

        _observe()  # records key at epoch 0
        _observe()  # hit
        bump_epoch("/home/u/proj")  # structural write → epoch now 1
        h0, m0 = _counts()
        _observe()  # same key, but epoch changed → MISS
        h1, m1 = _counts()
        assert m1 == m0 + 1, "after epoch bump the stale key must MISS"
        assert h1 == h0

    def test_global_bump_invalidates_directory_key(self):
        """A global bump (None directory — e.g. consolidation prior recompute)
        invalidates keys for EVERY directory, not just one."""
        from yadgar.server.tools._recall_shadow import bump_epoch

        _observe()  # records dir key at effective epoch 0
        _observe()  # hit
        bump_epoch(None)  # global generation bump (cross-directory structural event)
        h0, m0 = _counts()
        _observe()  # same dir key, but global gen changed → MISS
        h1, m1 = _counts()
        assert m1 == m0 + 1, "a global bump must invalidate a per-directory key"
        assert h1 == h0

    def test_bump_of_other_directory_does_not_affect(self):
        from yadgar.server.tools._recall_shadow import bump_epoch

        _observe()  # records key for /home/u/proj @ epoch 0
        bump_epoch("/some/other/dir")  # unrelated dir
        h0, m0 = _counts()
        _observe()  # same key, its dir's epoch unchanged → HIT
        h1, m1 = _counts()
        assert h1 == h0 + 1, "a bump on a different directory must not invalidate"

    def test_new_key_is_miss(self):
        _observe(query="first query")  # miss
        h0, m0 = _counts()
        _observe(query="a totally different query")  # new key → miss
        h1, m1 = _counts()
        assert m1 == m0 + 1
        assert h1 == h0

    def test_param_change_is_new_key(self):
        _observe(max_results=5)  # miss
        h0, m0 = _counts()
        _observe(max_results=10)  # different max_results → new key → miss
        h1, m1 = _counts()
        assert m1 == m0 + 1, "max_results is part of the key → different key → MISS"
        assert h1 == h0

    def test_observe_never_raises(self):
        from yadgar.server.tools._recall_shadow import RecallShadowParams, observe_recall

        # Pathologically bad inputs must not raise.
        observe_recall(
            RecallShadowParams(
                query=None,  # type: ignore[arg-type]
                directory=None,
                branch=None,
                type_filter="all",
                mode=None,
                profile=None,
                max_results=5,
                min_heat=0.0,
                tags=None,
                source="tool",  # v5.100.0: required field
            )
        )

    def test_bump_never_raises(self):
        from yadgar.server.tools._recall_shadow import bump_epoch

        bump_epoch(None)
        bump_epoch("")
        bump_epoch("/x")


# ---------------------------------------------------------------------------
# WIRING — the real recall() must call observe_recall; memorize must bump epoch.
# Guards against the try/except:pass silently no-op'ing the whole feature (a bug
# that would leave every unit test above green while recording nothing).
# No --extra ml needed: observe fires before dispatch, and recall returns [] (not
# raise) when embeddings are unavailable.
# ---------------------------------------------------------------------------


def _call_real_recall(query: str, directory: str):
    """Invoke the real recall MCP tool with a stub retriever (no ML needed)."""
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


class TestRecallToolWiring:
    def test_real_recall_records_shadow_miss_then_hit(self):
        h0, m0 = _counts()
        _call_real_recall("some unique probe query", "/tmp/wiring_proj")
        h1, m1 = _counts()
        assert m1 == m0 + 1, "real recall() must invoke observe_recall (a MISS on first sight)"

        _call_real_recall("some unique probe query", "/tmp/wiring_proj")
        h2, m2 = _counts()
        assert h2 == h1 + 1, "identical recall() at same epoch must be a would-HIT"

    def test_memorize_bumps_epoch_via_phase(self):
        """The memorize post-write phase must bump the directory's shadow epoch."""
        from yadgar.server.tools._memorize_phases._phase_post_write import _bump_shadow_epoch
        from yadgar.server.tools._recall_shadow import _current_epoch

        directory = "/tmp/wiring_memorize_proj"
        before = _current_epoch(directory)

        class _Ctx:
            context = directory

        _bump_shadow_epoch(_Ctx())
        assert _current_epoch(directory) == before + 1, (
            "memorize post-write phase must bump the directory epoch"
        )

    def test_consolidation_bumps_global_epoch(self):
        """The consolidation prior-recompute helper must bump the global generation."""
        from yadgar.consolidation.cls import _bump_shadow_epoch_global
        from yadgar.server.tools._recall_shadow import _current_epoch

        before = _current_epoch("/any/dir")
        _bump_shadow_epoch_global(updated=5)  # non-zero → bumps
        assert _current_epoch("/any/dir") == before + 1, (
            "consolidation prior recompute must bump the global generation"
        )
        # updated=0 → no-op
        mid = _current_epoch("/any/dir")
        _bump_shadow_epoch_global(updated=0)
        assert _current_epoch("/any/dir") == mid, "no-op when nothing was updated"
