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
    """Exercise the DB-side heat-boost loop directly (Phase 2a: it relocated from
    the in-core recall() into _apply_recall_db_side_effects, which runs inside
    _fanout_recall on the backend). The guard under test is m.get("id") /
    m.get("heat", 0.0) against KeyError on incomplete result dicts."""
    import yadgar._shared.runtime.state as _st
    from yadgar.backend.retrieval.recall_pipeline import _apply_recall_db_side_effects

    mock_storage = MagicMock()
    mock_storage._now_iso.return_value = "2026-01-01T00:00:00"
    mock_storage.boost_memories_access.return_value = None

    # thermo=None path is exercised (no thermo.record_access); mutates dicts in place.
    with patch.object(_st, "_thermo", None):
        _apply_recall_db_side_effects(result_dicts, "test query", mock_storage)
    return result_dicts


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
        # Heat-boost guard: 0.5 + 0.1 = 0.6 (mutated in-place by _apply_recall_db_side_effects)
        assert abs(result[0]["heat"] - 0.6) < 0.01

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
