"""Unit tests for v6 T6 Step 2 — fan-out orchestrator + UNIFIED_RECALL_ENABLED flag.

Coverage:
  1. UNIFIED_RECALL_ENABLED=True → fan-out returns memory + wiki candidates
  2. UNIFIED_RECALL_ENABLED=False → legacy path taken; _fanout_recall NOT called
  3. Flag-False: existing recall tests are unaffected (regression guard)
  4. _fanout_recall pools memory + wiki raw dicts
  5. _fanout_recall deduplicates by content (reuses _dedup_by_content)
  6. _fanout_recall respects max_results cap
  7. Flag-True with no wiki (_wiki=None) → only memory candidates returned
  8. Flag-True with no retriever (_retriever=None) → only wiki candidates returned
"""

from __future__ import annotations

import sys
from unittest.mock import MagicMock, patch

import yadgar.server._state as _st
import yadgar.server.tools.recall as _recall_symbol  # noqa: F401 — imported for side-effects

# @_tool() registers the function and replaces it in the module's local name,
# so `import yadgar.server.tools.recall` yields the function, not the module.
# Use sys.modules to get the actual module object for monkeypatching settings.
_recall_module = sys.modules["yadgar.server.tools.recall"]

from yadgar.server.tools.recall import _fanout_recall  # noqa: E402

# The MCP tool function — callable directly for end-to-end tests
recall_fn = _recall_module.recall


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_memory_dict(mid: int = 1, score: float = 0.8, content: str | None = None) -> dict:
    return {
        "id": mid,
        "content": content or f"memory content {mid}",
        "heat": 0.6,
        "_retrieval_score": score,
        "directory_context": "/tmp/test",
        "branch": "master",
        "tags": [],
    }


def _make_wiki_dict(slug: str = "overview", score: float = 0.7) -> dict:
    return {
        "id": 100,
        "slug": slug,
        "title": f"Wiki: {slug}",
        "content": f"wiki content for {slug}",
        "_retrieval_score": score,
        "directory_context": "/tmp/test",
        "branch": "master",
        "_source": "wiki",
    }


def _make_mock_retriever(memories=None):
    r = MagicMock()
    r.recall.return_value = memories if memories is not None else [_make_memory_dict(1, 0.9)]
    return r


def _make_mock_wiki(pages=None):
    w = MagicMock()
    w.query.return_value = pages if pages is not None else [_make_wiki_dict("overview", 0.75)]
    return w


# ---------------------------------------------------------------------------
# 1 + 2. UNIFIED_RECALL_ENABLED flag controls routing
# ---------------------------------------------------------------------------


class TestUnifiedRecallFlag:
    """Tests that the flag gates which path is taken, not which path produces what."""

    def _call_recall(self, directory="/tmp/test", **kwargs):
        """Helper to call recall MCP tool with standard mocked dependencies."""
        mock_retriever = _make_mock_retriever()
        mock_storage = MagicMock()
        mock_storage._now_iso.return_value = "2026-01-01T00:00:00"
        mock_wiki = _make_mock_wiki()

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
            return recall_fn(query="test query", max_results=5, directory=directory, **kwargs)

    def test_flag_true_calls_fanout(self, monkeypatch):
        """When UNIFIED_RECALL_ENABLED=True, _fanout_recall is entered."""
        monkeypatch.setattr(_recall_module.settings, "UNIFIED_RECALL_ENABLED", True)

        fanout_called = []

        orig_fanout = _recall_module._fanout_recall

        def spy_fanout(*args, **kwargs):
            fanout_called.append(True)
            return orig_fanout(*args, **kwargs)

        monkeypatch.setattr(_recall_module, "_fanout_recall", spy_fanout)
        self._call_recall()
        assert fanout_called, "_fanout_recall was not called despite UNIFIED_RECALL_ENABLED=True"

    def test_flag_false_does_not_call_fanout(self, monkeypatch):
        """When UNIFIED_RECALL_ENABLED=False (default), _fanout_recall is NOT entered."""
        monkeypatch.setattr(_recall_module.settings, "UNIFIED_RECALL_ENABLED", False)

        fanout_called = []

        def spy_fanout(*args, **kwargs):
            fanout_called.append(True)
            return []

        monkeypatch.setattr(_recall_module, "_fanout_recall", spy_fanout)
        self._call_recall()
        assert not fanout_called, "_fanout_recall was called despite UNIFIED_RECALL_ENABLED=False"

    def test_flag_false_retriever_is_called(self, monkeypatch):
        """Flag=False → retriever.recall() is called (legacy path active)."""
        monkeypatch.setattr(_recall_module.settings, "UNIFIED_RECALL_ENABLED", False)

        mock_retriever = _make_mock_retriever()
        mock_storage = MagicMock()
        mock_storage._now_iso.return_value = "2026-01-01T00:00:00"
        mock_wiki = _make_mock_wiki()

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
            recall_fn(query="test query", max_results=5, directory="/tmp/test")

        mock_retriever.recall.assert_called()


# ---------------------------------------------------------------------------
# 3. Flag-False regression — result type and count unchanged
# ---------------------------------------------------------------------------


class TestFlagFalseRegression:
    """Validates that flag-False produces results from the legacy path."""

    def test_flag_false_returns_list(self, monkeypatch):
        """Flag=False → result is a list (legacy path return type preserved)."""
        monkeypatch.setattr(_recall_module.settings, "UNIFIED_RECALL_ENABLED", False)

        mock_retriever = _make_mock_retriever([_make_memory_dict(1)])
        mock_storage = MagicMock()
        mock_storage._now_iso.return_value = "2026-01-01T00:00:00"
        mock_wiki = _make_mock_wiki([])  # empty wiki

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
            result = recall_fn(query="retrieval pipeline", max_results=5, directory="/tmp/test")

        assert isinstance(result, list)

    def test_flag_false_no_source_tag(self, monkeypatch):
        """Flag=False → memory results do not have _source='wiki' (legacy schema)."""
        monkeypatch.setattr(_recall_module.settings, "UNIFIED_RECALL_ENABLED", False)

        mem = _make_memory_dict(1)
        mock_retriever = _make_mock_retriever([mem])
        mock_storage = MagicMock()
        mock_storage._now_iso.return_value = "2026-01-01T00:00:00"
        mock_wiki = _make_mock_wiki([])

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
            result = recall_fn(query="retrieval pipeline", max_results=5, directory="/tmp/test")

        # Memory results in legacy path should not have _source set
        for r in result:
            assert r.get("_source") != "wiki" or r.get("id") == 1


# ---------------------------------------------------------------------------
# 4. _fanout_recall unit tests
# ---------------------------------------------------------------------------


class TestFanoutRecall:
    """Direct unit tests for the _fanout_recall() helper."""

    def _call_fanout(self, query="test", max_results=5, retriever=None, wiki=None):
        with (
            patch.object(_st, "_retriever", retriever),
            patch.object(_st, "_wiki", wiki),
        ):
            return _fanout_recall(
                query=query,
                max_results=max_results,
                min_heat=0.0,
                directory="/tmp/test",
                current_branch="main",
                default_branch="master",
            )

    def test_pools_memory_and_wiki(self):
        """Fan-out with both retriever + wiki returns items from both sources."""
        mem = _make_memory_dict(1, 0.9)
        wiki = _make_wiki_dict("overview", 0.8)

        mock_retriever = _make_mock_retriever([mem])
        mock_wiki = _make_mock_wiki([wiki])

        results = self._call_fanout(retriever=mock_retriever, wiki=mock_wiki)

        sources = {r.get("_source", "memory") for r in results}
        assert "memory" in sources or any(r.get("id") == 1 for r in results), (
            "Expected at least one memory result"
        )
        assert any(r.get("_source") == "wiki" for r in results), "Expected at least one wiki result"

    def test_memory_only_when_wiki_none(self):
        """With _wiki=None, only memory candidates are returned."""
        mem = _make_memory_dict(1, 0.9)
        mock_retriever = _make_mock_retriever([mem])

        results = self._call_fanout(retriever=mock_retriever, wiki=None)

        assert len(results) >= 1
        assert all(r.get("_source") != "wiki" for r in results)

    def test_wiki_only_when_retriever_none(self):
        """With _retriever=None, only wiki candidates are returned."""
        wiki_page = _make_wiki_dict("overview", 0.8)
        mock_wiki = _make_mock_wiki([wiki_page])

        results = self._call_fanout(retriever=None, wiki=mock_wiki)

        assert len(results) >= 1
        assert all(r.get("_source") == "wiki" for r in results)

    def test_respects_max_results(self):
        """Fan-out result count does not exceed max_results."""
        # Seed many memories and wikis
        mems = [_make_memory_dict(i, 0.9 - i * 0.01) for i in range(1, 20)]
        wikis = [_make_wiki_dict(f"page-{i}", 0.8) for i in range(10)]
        mock_retriever = _make_mock_retriever(mems)
        mock_wiki = _make_mock_wiki(wikis)

        results = self._call_fanout(retriever=mock_retriever, wiki=mock_wiki, max_results=3)

        assert len(results) <= 3

    def test_deduplicates_by_content(self):
        """Fan-out deduplicates items with identical content."""
        shared_content = "This content appears in both memory and wiki"
        mem = _make_memory_dict(1, 0.9, content=shared_content)
        wiki_page = {
            "id": 50,
            "slug": "dup-page",
            "content": shared_content,
            "_retrieval_score": 0.7,
            "_source": "wiki",
        }

        mock_retriever = _make_mock_retriever([mem])
        mock_wiki = _make_mock_wiki([wiki_page])

        results = self._call_fanout(retriever=mock_retriever, wiki=mock_wiki, max_results=10)

        # Only one item with the shared content should survive dedup
        contents = [r.get("content") for r in results]
        assert contents.count(shared_content) == 1

    def test_empty_result_when_both_none(self):
        """With both _retriever=None and _wiki=None, returns empty list."""
        results = self._call_fanout(retriever=None, wiki=None)
        assert results == []

    def test_returns_list_of_dicts(self):
        """Fan-out always returns list[dict] (same type as legacy recall)."""
        mem = _make_memory_dict(1, 0.9)
        mock_retriever = _make_mock_retriever([mem])
        results = self._call_fanout(retriever=mock_retriever, wiki=None)
        assert isinstance(results, list)
        for r in results:
            assert isinstance(r, dict)

    def test_sorted_by_retrieval_score_descending(self):
        """Pool is sorted by _retrieval_score descending before max_results trim."""
        mems = [
            _make_memory_dict(1, 0.5),  # lower score
            _make_memory_dict(2, 0.9),  # higher score
        ]
        mock_retriever = _make_mock_retriever(mems)

        results = self._call_fanout(retriever=mock_retriever, wiki=None, max_results=10)

        scores = [r.get("_retrieval_score", r.get("heat", 0.0)) for r in results]
        assert scores == sorted(scores, reverse=True), "Results not sorted by score descending"


# ---------------------------------------------------------------------------
# 5. Flag-True end-to-end: recall MCP tool returns both types
# ---------------------------------------------------------------------------


class TestFlagTrueEndToEnd:
    """Integration: recall MCP tool with flag=True returns memory + wiki candidates."""

    def test_flag_true_returns_memory_and_wiki(self, monkeypatch):
        """With UNIFIED_RECALL_ENABLED=True, recall returns items from both providers."""
        monkeypatch.setattr(_recall_module.settings, "UNIFIED_RECALL_ENABLED", True)

        mem = _make_memory_dict(1, 0.9)
        wiki_page = _make_wiki_dict("test-wiki", 0.75)

        mock_retriever = _make_mock_retriever([mem])
        mock_storage = MagicMock()
        mock_storage._now_iso.return_value = "2026-01-01T00:00:00"
        mock_wiki = _make_mock_wiki([wiki_page])

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
            result = recall_fn(query="test query", max_results=10, directory="/tmp/test")

        has_wiki = any(r.get("_source") == "wiki" for r in result)
        has_memory = any(r.get("id") == 1 for r in result)

        assert has_wiki, f"Expected wiki result in fan-out output; got: {result}"
        assert has_memory, f"Expected memory result in fan-out output; got: {result}"
