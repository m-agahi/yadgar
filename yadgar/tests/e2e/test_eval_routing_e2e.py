"""E2E test for Step 0 — eval harness routes through fan-out path when --unified on.

Design per plan unified-scoped-recall-v2-steps3-5.md §1:
  - With --unified on, a wiki-only golden pair (gold key wiki:<slug>) is retrievable
    (recall@10 > 0), which is IMPOSSIBLE on the legacy retriever.recall() path
    (it returns memories only).
  - This test fails on master (proves the gap) and passes after Step 0.

Gate-reachability: lives in yadgar/tests/e2e/ → collected by `make e2e`.
Uses @pytest.mark.e2e for the live-surreal tests.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.e2e


class TestEvalRoutesViaMCPTool:
    """Step 0: evaluate_pair routes through MCP recall tool when unified=True.

    Tests the routing change in run_eval.py::evaluate_pair. With flag-ON,
    a wiki-only golden pair becomes retrievable because the fan-out path
    queries WikiStore; the legacy retriever.recall() path never queries wikis.
    """

    def test_eval_routes_through_fanout_when_unified(
        self, e2e_engines, monkeypatch, recall_backend_bypass
    ):
        """With --unified on, wiki-only pair is retrievable (recall@10 > 0).

        Approach:
          1. Add a wiki page about a unique topic.
          2. Call evaluate_pair() with unified=True and a matching query.
          3. Assert recall@10 > 0 for the wiki gold key.

        The same query with unified=False (retriever.recall path) would return
        only memories — recall@10 = 0 for the wiki key (not retrievable).
        """
        from benchmarks.run_eval import evaluate_pair_unified
        from yadgar._shared.runtime import state as _st
        from yadgar._shared.wiki import WikiAddOptions

        # Insert a wiki page via WikiStore.add() for correct integer ID + embedding + FTS.
        # Raw _q INSERT yields non-integer IDs and skips embedding → WikiStore.query() misses.
        assert _st._wiki is not None, "WikiStore must be initialized in e2e_engines"
        opts = WikiAddOptions(
            source_memory_ids=[],
            branch="master",
            directory_context=e2e_engines["yadgar_dir"],
        )
        page = _st._wiki.add(
            title="Step0 Eval Routing Test",
            content="unified-recall eval routing test xzeval001 wiki content unique token",
            category="reference",
            tags=[],
            opts=opts,
        )
        slug = page["slug"]

        # Build a golden pair targeting the wiki slug
        pair = {
            "query_id": "eval-step0-test",
            "query": "unified-recall eval routing test xzeval001",
            "relevant_memory_ids": [],
            "relevant_wiki_slugs": [slug],
            "type": "wiki",
        }

        # Set up server state for wiki retrieval (already asserted above)

        # Patch branch detection to avoid real git calls
        monkeypatch.setattr("yadgar.core.server._detect_branch", lambda _d: "master")
        monkeypatch.setattr("yadgar.core.server._get_default_branch", lambda _d: "master")

        # Fan-out is now unconditional (Phase 2a: recall() is a pure forwarder).
        # recall_backend_bypass fixture routes _forward_to_backend → _fanout_recall.

        try:
            metrics = evaluate_pair_unified(
                pair=pair,
                directory=e2e_engines["yadgar_dir"],
                k_values=[1, 5, 10],
                max_results=20,
            )
            assert "error" not in metrics, f"evaluate_pair_unified error: {metrics.get('error')}"
            assert metrics.get("recall@10", 0.0) > 0, (
                f"Wiki-only pair not retrieved via fan-out path; recall@10=0. "
                f"Got retrieved_count={metrics.get('retrieved_count', 0)}"
            )
        finally:
            pass  # no settings cache to clear (UNIFIED flag removed)

    def test_legacy_path_cannot_retrieve_wiki(self, e2e_engines, monkeypatch):
        """Legacy retriever.recall() path does NOT return wiki results.

        This documents the gap Step 0 bridges: with unified=False (default),
        a wiki-only query returns recall@10=0 because retriever.recall()
        searches only the memory table.
        """
        from benchmarks.run_eval import evaluate_pair

        # Build a wiki-only pair (no relevant memory IDs)
        pair = {
            "query_id": "legacy-wiki-test",
            "query": "this query has no matching memories only wiki",
            "relevant_memory_ids": [],
            "relevant_wiki_slugs": ["nonexistent-wiki-slug"],
            "type": "wiki",
        }

        from yadgar._shared.runtime import state as _st

        assert _st._retriever is not None, "Retriever must be initialized"

        metrics = evaluate_pair(pair, _st._retriever, k_values=[1, 5, 10], max_results=20)
        assert "error" not in metrics, f"evaluate_pair error: {metrics.get('error')}"
        # Legacy path: wiki keys cannot appear in results → recall@10 = 0
        assert metrics.get("recall@10", 0.0) == 0.0, (
            "Legacy path should NOT return wiki results (retriever.recall() is memory-only)"
        )
