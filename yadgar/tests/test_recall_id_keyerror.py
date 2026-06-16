"""v5.59 regression test — recall KeyError 'id' (and 'heat') from incomplete result dicts.

The retriever pipeline can inject memory-shaped dicts that lack 'id' or 'heat':
  - Profile/belief injection (_search_profiles_and_beliefs in retrieval/fusion.py) produces
    dicts with only {'id': -N, 'content': ..., '_source': ..., '_retrieval_score': ...} — no 'heat'.
  - Any retriever that constructs synthetic dicts may omit 'id'.

The heat-boost loop in recall.py (lines 229-239) accesses both m["heat"] and m["id"] with bare
subscript access — KeyError on either missing key.  m["id"] is also accessed at line 244 for SR
transitions.

This file first proves the bug (red), then the fix makes them green.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

# ---------------------------------------------------------------------------
# Helpers (mirror minimal fixtures from test_recall_wiki_metrics.py)
# ---------------------------------------------------------------------------


def _make_full_memory(mid: int = 1) -> dict:
    return {
        "id": mid,
        "content": f"memory {mid}",
        "heat": 0.5,
        "tags": [],
        "branch": None,
        "_retrieval_score": 0.5,
    }


def _call_recall_with_results(result_dicts: list[dict]) -> list[dict]:
    """Run the recall MCP tool with a mock retriever returning *result_dicts*."""
    import yadgar.server._state as _st
    from yadgar.server.tools.recall import recall as recall_fn

    mock_retriever = MagicMock()
    mock_retriever.recall.return_value = result_dicts

    mock_storage = MagicMock()
    mock_storage._now_iso.return_value = "2026-01-01T00:00:00"
    mock_storage.update_memory_heat.return_value = None
    mock_storage.update_memory_last_accessed.return_value = None

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
        return recall_fn(query="test query", max_results=5, min_heat=0.0, directory="/tmp/test")


# ---------------------------------------------------------------------------
# Regression tests
# ---------------------------------------------------------------------------


class TestRecallIdKeyError:
    def test_recall_does_not_raise_when_result_missing_id(self):
        """recall() must not raise KeyError('id') when a result dict has no 'id' key.

        Root cause: heat-boost loop at recall.py lines 234-235 does m["id"] bare subscript.
        A dict without 'id' (e.g. from a synthetic injection) triggers KeyError.
        Fix: guard with m.get('id') so the loop skips gracefully.
        """
        # Dict with heat but no id — triggers KeyError: 'id' at line 234/235
        no_id_result = {
            "content": "result without id",
            "heat": 0.5,
            "_retrieval_score": 0.6,
            "branch": None,
        }
        # Must not raise
        result = _call_recall_with_results([no_id_result])
        # The id-less dict should still appear in results (or be silently skipped — either OK)
        assert isinstance(result, list)

    def test_recall_does_not_raise_when_result_missing_heat(self):
        """recall() must not raise KeyError('heat') when a result dict has no 'heat' key.

        Root cause: heat-boost loop at recall.py line 233 does m["heat"] bare subscript.
        Profile/belief dicts injected by _rerank_profile_belief_merge have no 'heat' field.
        Fix: guard with m.get('heat', 0.0) so the convex-combination is computed safely.
        """
        # Dict with id but no heat — triggers KeyError: 'heat' at line 233
        no_heat_result = {
            "id": -5,
            "content": "profile result without heat",
            "_source": "profile",
            "_retrieval_score": 0.5,
            "branch": None,
        }
        # Must not raise
        result = _call_recall_with_results([no_heat_result])
        assert isinstance(result, list)

    def test_recall_normal_result_still_works(self):
        """Regression guard: recall() with fully-formed dicts still returns results."""
        normal = _make_full_memory(42)
        result = _call_recall_with_results([normal])
        assert len(result) == 1
        assert result[0]["id"] == 42

    def test_recall_mixed_results_no_crash(self):
        """recall() must handle a mixed list of complete and incomplete result dicts."""
        results = [
            _make_full_memory(1),
            {"content": "no id", "heat": 0.3, "_retrieval_score": 0.3, "branch": None},
            {"id": -99, "content": "no heat", "_source": "profile", "_retrieval_score": 0.4},
        ]
        # Must not raise regardless of the mix
        out = _call_recall_with_results(results)
        assert isinstance(out, list)
