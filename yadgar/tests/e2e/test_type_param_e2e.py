"""E2E tests for Step 5 — recall(type=) parameter and wiki_query deprecation.

Design per plan unified-scoped-recall-v2-steps3-5.md §5:

  1. test_type_memory_returns_only_memories — recall(type="memory") → no wiki in results.
  2. test_type_wiki_returns_only_wiki — recall(type="wiki") → no memory in results.
  3. test_type_all_returns_both — recall(type="all") → both memory and wiki in results.
  4. test_type_invalid_raises_before_retrieval — recall(type="invalid") → ValueError
     raised before any DB work (early validation gate).
  5. test_wiki_query_alias_equivalent_to_type_wiki — wiki_query() and
     recall(type="wiki") return the same set of slugs (or wiki_query logs deprecation).

PLACEMENT: lives in yadgar/tests/e2e/ → collected by `make e2e`.
Uses @pytest.mark.e2e for live-surreal DB.
"""

from __future__ import annotations

import logging

import pytest

pytestmark = pytest.mark.e2e

YADGAR_DIR = "/home/test/yadgar-project"


def _insert_mem(storage, embeddings, content: str) -> int:
    """Insert a memory with real embedding."""
    emb = embeddings.encode(content)
    return storage.insert_memory(
        {
            "content": content,
            "embedding": emb,
            "directory_context": YADGAR_DIR,
            "tags": [],
            "heat": 1.0,
        }
    )


def _insert_wiki(title: str, content: str) -> str:
    """Insert a wiki page via WikiStore.add() for correct integer ID + embedding + FTS.

    Returns the slug (as computed by WikiStore._slugify).
    """
    from yadgar._shared.runtime import state as _st
    from yadgar._shared.wiki import WikiAddOptions

    assert _st._wiki is not None, "WikiStore must be initialized in e2e_engines"
    opts = WikiAddOptions(
        source_memory_ids=[],
        branch="master",
        directory_context=YADGAR_DIR,
    )
    page = _st._wiki.add(
        title=title,
        content=content,
        category="reference",
        tags=[],
        opts=opts,
    )
    return page["slug"]


def _run_recall(
    monkeypatch,
    query: str,
    directory: str = YADGAR_DIR,
    max_results: int = 20,
    type_filter: str = "all",
) -> list[dict]:
    """Run recall MCP tool with UNIFIED_RECALL_ENABLED=True."""
    import sys

    _rm = sys.modules.get("yadgar.core.server.tools.recall")
    if _rm is None:
        import yadgar.core.server.tools.recall as _rm  # type: ignore[no-redef]

    recall_fn = _rm.recall
    return recall_fn(
        query=query,
        directory=directory,
        max_results=max_results,
        type=type_filter,  # noqa: A002
    )


class TestTypeParamE2E:
    """Live-DB e2e tests for Step 5: recall(type=) filtering and wiki_query deprecation."""

    def test_type_memory_returns_only_memories(
        self, e2e_engines, monkeypatch, recall_backend_bypass
    ):
        """recall(type="memory") must not return any wiki results.

        Ref: BC-U2.
        """
        storage = e2e_engines["storage"]
        embeddings = e2e_engines["embeddings"]

        unique = "xztype501"
        mem_id = _insert_mem(storage, embeddings, f"type memory only test {unique}")
        _insert_wiki(
            title=f"Type Memory Only Wiki {unique}",
            content=f"type memory only test wiki {unique}",
        )

        results = _run_recall(monkeypatch, f"type memory only test {unique}", type_filter="memory")

        wiki_results = [r for r in results if r.get("_source") == "wiki"]
        assert len(wiki_results) == 0, (
            f"recall(type='memory') must return 0 wiki results; got {len(wiki_results)}: "
            f"{[r.get('slug') or r.get('id') for r in wiki_results]}"
        )

        # Memory must be present
        mem_ids = {r.get("id") for r in results}
        assert mem_id in mem_ids, (
            f"recall(type='memory') must include memory id={mem_id}; got ids={mem_ids}"
        )

    def test_type_wiki_returns_only_wiki(self, e2e_engines, monkeypatch, recall_backend_bypass):
        """recall(type="wiki") must not return any memory results.

        Ref: BC-U3.
        """
        storage = e2e_engines["storage"]
        embeddings = e2e_engines["embeddings"]

        unique = "xztype502"
        _insert_mem(storage, embeddings, f"type wiki only test memory {unique}")
        wiki_slug = _insert_wiki(
            title=f"Type Wiki Only {unique}",
            content=f"type wiki only test {unique}",
        )

        results = _run_recall(monkeypatch, f"type wiki only test {unique}", type_filter="wiki")

        memory_results = [r for r in results if r.get("_source") == "memory"]
        assert len(memory_results) == 0, (
            f"recall(type='wiki') must return 0 memory results; got {len(memory_results)}: "
            f"{[r.get('id') for r in memory_results]}"
        )

        # Wiki must be present
        wiki_slugs_in_results = {
            r.get("slug") or r.get("id") for r in results if r.get("_source") == "wiki"
        }
        assert any(wiki_slug in str(s) for s in wiki_slugs_in_results), (
            f"recall(type='wiki') must include wiki slug='{wiki_slug}'; "
            f"got wiki_slugs={wiki_slugs_in_results}"
        )

    def test_type_memory_order_matches_legacy(
        self, e2e_engines, monkeypatch, recall_backend_bypass
    ):
        """recall(type="memory") fan-out returns results in the SAME ORDER as legacy recall.

        Ref: BC-U2 (strengthened) — the previous e2e only checked membership.
        The v5.80 regression was that fuse_candidates CE-reranked single-provider
        results into a different order, dropping the correct memory from rank 1.
        This test verifies the bypass restores native ordering.

        Methodology:
        - Seed several memories with distinct relevance signals via graded content.
        - Use legacy retriever.recall() as the oracle order (pre-filter for memory only).
        - Use fan-out recall(type="memory") as the candidate.
        - Assert the top-k memory IDs are in the same order.
        - Monkeypatch heat updates to prevent heat mutation across calls from
          perturbing the second recall's ranking.
        """
        storage = e2e_engines["storage"]
        embeddings = e2e_engines["embeddings"]

        unique = "xztype_order501"

        # Insert 3 memories with distinct heat so ordering is deterministic
        # when CE is unavailable (fallback to native_score = retrieval_score ~ heat).
        # Content is similar enough that both recall paths score them, but graded heat
        # means the native ordering is unambiguous: high > mid > low.
        mem_high = _insert_mem(storage, embeddings, f"order parity high relevance {unique}")
        mem_mid = _insert_mem(storage, embeddings, f"order parity mid relevance {unique}")
        mem_low = _insert_mem(storage, embeddings, f"order parity low relevance {unique}")

        # Set explicit heat scores to create deterministic ordering
        storage.update_memory_heat(mem_high, 0.9)
        storage.update_memory_heat(mem_mid, 0.5)
        storage.update_memory_heat(mem_low, 0.1)

        # Insert a wiki page with same tokens — should be EXCLUDED from type=memory results
        _insert_wiki(
            title=f"Order Parity Wiki {unique}",
            content=f"order parity high relevance {unique}",
        )

        # Freeze heat updates so that the legacy call does not raise heat on the
        # memories and perturb the fan-out call's ranking.
        import sys

        # Monkey-patch storage.update_memory_heat to a no-op for parity measurement
        monkeypatch.setattr(storage, "update_memory_heat", lambda mid, heat: None)
        monkeypatch.setattr(storage, "update_memory_last_accessed", lambda mid, ts: None)

        _rm = sys.modules.get("yadgar.core.server.tools.recall")
        if _rm is None:
            import yadgar.core.server.tools.recall as _rm  # type: ignore[no-redef]

        from yadgar._shared.runtime import state as _st

        assert _st._retriever is not None, "Retriever must be initialized for order parity test"

        # Legacy oracle: Retriever.recall() native memory ordering
        legacy_results = _st._retriever.recall(
            f"order parity {unique}",
            max_results=20,
            min_heat=0.0,
        )
        legacy_mem_ids = [
            m.get("id") for m in legacy_results if m.get("id") in {mem_high, mem_mid, mem_low}
        ]

        if len(legacy_mem_ids) < 2:
            pytest.skip(
                f"Legacy recall returned fewer than 2 seeded memories "
                f"(got {legacy_mem_ids}); cannot assert order"
            )

        # Fan-out: recall(type="memory") — should bypass fuse_candidates, return native order
        fanout_results = _rm.recall(
            query=f"order parity {unique}",
            directory=YADGAR_DIR,
            max_results=20,
            type="memory",  # noqa: A002
        )

        fanout_mem_ids = [
            r.get("id") for r in fanout_results if r.get("id") in {mem_high, mem_mid, mem_low}
        ]

        assert fanout_mem_ids == legacy_mem_ids, (
            f"recall(type='memory') fan-out order must match legacy recall order. "
            f"legacy_order={legacy_mem_ids}, fanout_order={fanout_mem_ids}. "
            f"If they differ, the single-provider bypass is not in effect (double CE-rerank regression)."
        )

    def test_type_wiki_order_returns_results(self, e2e_engines, monkeypatch, recall_backend_bypass):
        """recall(type="wiki") fan-out returns non-empty wiki results (coverage preserved).

        Ref: BC-U3 (order parity addendum) — wiki coverage must be non-zero after
        the v5.80 single-provider bypass. The bypass skips fuse_candidates but must
        still return the WikiProvider's native results.

        Success criterion: at least 1 wiki result for an on-topic query.
        (Strict order vs wiki_query() is not asserted because WikiProvider and
        wiki_query() may apply different post-filters; coverage is the signal.)
        """
        storage = e2e_engines["storage"]
        embeddings = e2e_engines["embeddings"]

        unique = "xztype_order502"
        _insert_mem(storage, embeddings, f"wiki order parity memory {unique}")
        wiki_slug = _insert_wiki(
            title=f"Wiki Order Parity {unique}",
            content=f"wiki order parity test content {unique}",
        )

        results = _run_recall(monkeypatch, f"wiki order parity test {unique}", type_filter="wiki")

        wiki_results = [r for r in results if r.get("_source") == "wiki"]
        assert len(wiki_results) >= 1, (
            f"recall(type='wiki') must return ≥1 wiki results after v5.80 bypass; "
            f"got 0. Wiki slug={wiki_slug!r}. "
            f"Sources in results: {[r.get('_source') for r in results]}"
        )

    def test_type_all_returns_both(self, e2e_engines, monkeypatch, recall_backend_bypass):
        """recall(type="all") returns both memory and wiki results.

        Ref: BC-U1.
        """
        storage = e2e_engines["storage"]
        embeddings = e2e_engines["embeddings"]

        unique = "xztype503"
        # Use DISTINCT content so _dedup_by_content does not collapse them.
        mem_id = _insert_mem(storage, embeddings, f"type all memory entry {unique}")
        wiki_slug = _insert_wiki(
            title=f"Type All Wiki {unique}",
            content=f"type all wiki page content {unique}",
        )

        # Query contains tokens present in both to ensure both score
        results = _run_recall(monkeypatch, f"type all {unique}", type_filter="all")

        sources = {r.get("_source") for r in results}
        assert "memory" in sources, (
            f"recall(type='all') must include memory results; sources={sources}"
        )
        assert "wiki" in sources, f"recall(type='all') must include wiki results; sources={sources}"

        mem_ids = {r.get("id") for r in results if r.get("_source") == "memory"}
        assert mem_id in mem_ids, (
            f"Memory id={mem_id} must appear; mem_ids={mem_ids}; "
            f"all_ids={[r.get('id') for r in results]}"
        )

        wiki_result_slugs = {
            r.get("slug") or r.get("id") for r in results if r.get("_source") == "wiki"
        }
        assert any(wiki_slug in str(s) for s in wiki_result_slugs), (
            f"Wiki slug='{wiki_slug}' must appear"
        )

    def test_type_invalid_raises_before_retrieval(
        self, e2e_engines, monkeypatch, recall_backend_bypass
    ):
        """recall(type="invalid") raises ValueError before any DB query.

        Ref: BC-U5 — validation must be early (pre-DB) to avoid pointless work.
        """
        import sys

        _rm = sys.modules.get("yadgar.core.server.tools.recall")
        if _rm is None:
            import yadgar.core.server.tools.recall as _rm  # type: ignore[no-redef]

        recall_fn = _rm.recall
        with pytest.raises(ValueError, match="invalid"):
            recall_fn(
                query="any query",
                directory=YADGAR_DIR,
                type="invalid",  # noqa: A002
            )

    def test_type_all_memory_order_parity_with_relevant_wiki(
        self, e2e_engines, monkeypatch, recall_backend_bypass
    ):
        """recall(type="all") preserves relative memory order while inserting relevant wiki.

        Ref: v5.80 fanout-fusion-fix — the double-rerank bug.

        The double-rerank regression: fuse_candidates CE-reranked ALL pooled candidates
        including memory candidates that MemoryProvider already ranked via WRRF+GTE.
        This reordered the memory subset → MRR 0.81→0.63.

        This test seeds 3 memories with GRADED HEAT (0.9/0.5/0.1) so that native order
        (heat-driven) CONFLICTS with arbitrary CE order — a test that is green both before
        and after the fix would not be discriminating. The test is red on unmodified
        fuse_candidates (which CE-reranks memories) and green after the fix (memories
        stay in WRRF/heat native order).

        Methodology:
          - Seed mem_high/mem_mid/mem_low with graded heat (0.9/0.5/0.1).
          - Seed one relevant wiki page for the same query.
          - Oracle = retriever.recall() order for the seeded memories (native WRRF/heat rank).
          - Candidate = recall(type="all") order for the seeded memories in the result.
          - Assert memories appear in SAME relative order as oracle.
          - Assert the wiki page appears in the results (coverage preserved).
        """
        storage = e2e_engines["storage"]
        embeddings = e2e_engines["embeddings"]

        unique = "xztype_all_order601"

        # Insert 3 memories with distinct heat — creates a graded native order that
        # cross-encoder ranking can invert when double-ranking is active.
        # Heat 0.9 > 0.5 > 0.1 → native order: high, mid, low.
        mem_high = _insert_mem(storage, embeddings, f"type all order parity high heat {unique}")
        mem_mid = _insert_mem(storage, embeddings, f"type all order parity mid heat {unique}")
        mem_low = _insert_mem(storage, embeddings, f"type all order parity low heat {unique}")

        storage.update_memory_heat(mem_high, 0.9)
        storage.update_memory_heat(mem_mid, 0.5)
        storage.update_memory_heat(mem_low, 0.1)

        # One relevant wiki page — must appear in type=all results (coverage assertion).
        wiki_slug = _insert_wiki(
            title=f"Type All Order Wiki {unique}",
            content=f"type all order parity wiki knowledge {unique}",
        )

        import sys

        # Freeze heat updates to prevent the oracle call from perturbing the fanout call.
        monkeypatch.setattr(storage, "update_memory_heat", lambda mid, heat: None)
        monkeypatch.setattr(storage, "update_memory_last_accessed", lambda mid, ts: None)

        _rm = sys.modules.get("yadgar.core.server.tools.recall")
        if _rm is None:
            import yadgar.core.server.tools.recall as _rm  # type: ignore[no-redef]

        from yadgar._shared.runtime import state as _st

        assert _st._retriever is not None, "Retriever must be initialized for order parity test"

        # Oracle: native memory ordering from Retriever.recall() (WRRF+GTE, no CE double-rank).
        legacy_results = _st._retriever.recall(
            f"type all order parity {unique}",
            max_results=20,
            min_heat=0.0,
        )
        seeded_ids = {mem_high, mem_mid, mem_low}
        legacy_mem_ids = [m.get("id") for m in legacy_results if m.get("id") in seeded_ids]

        if len(legacy_mem_ids) < 2:
            pytest.skip(
                f"Legacy recall returned fewer than 2 seeded memories "
                f"(got {legacy_mem_ids}); cannot assert order"
            )

        # Fan-out: recall(type="all") — fuse_candidates must NOT reorder memories.
        fanout_results = _rm.recall(
            query=f"type all order parity {unique}",
            directory=YADGAR_DIR,
            max_results=20,
            type="all",  # noqa: A002
        )

        fanout_mem_ids = [
            r.get("id")
            for r in fanout_results
            if r.get("_source") == "memory" and r.get("id") in seeded_ids
        ]

        # PRIMARY assertion: memories in same relative order as legacy.
        assert fanout_mem_ids == legacy_mem_ids, (
            f"recall(type='all') must preserve memory relative order vs legacy recall. "
            f"legacy_order={legacy_mem_ids}, fanout_order={fanout_mem_ids}. "
            f"If they differ, fuse_candidates is CE-reranking memories (double-rerank regression)."
        )

        # SECONDARY assertion: wiki page appears in results (coverage must be preserved).
        fanout_wiki_slugs = {
            r.get("slug") or r.get("id") for r in fanout_results if r.get("_source") == "wiki"
        }
        assert any(wiki_slug in str(s) for s in fanout_wiki_slugs), (
            f"recall(type='all') must include wiki slug='{wiki_slug}'; "
            f"fanout_wiki_slugs={fanout_wiki_slugs}. "
            f"Wiki coverage must be preserved after fusion fix."
        )

    def test_type_all_wiki_pool_empty_preserves_memory_order(
        self, e2e_engines, monkeypatch, recall_backend_bypass
    ):
        """recall(type="all") preserves native memory order when the wiki pool is empty.

        Ref: v5.80 fanout-fusion-fix — gap (d), BC-U6.

        The single-provider bypass must trigger whenever EITHER pool is empty — not
        only for explicit type="memory"/"wiki". Under type="all" with no wiki
        candidates (no relevant wiki page, or wiki store unavailable), the pre-fix
        code still called fuse_candidates on the memory-only pool → CE-reranked it a
        SECOND time → reordered it (the same double-rerank class, MRR 0.84→0.74). The
        fix routes an empty other-pool to the native-order bypass.

        This is the discriminating scenario the relevant-wiki parity test does NOT
        cover: there, both pools are non-empty so the fuse path runs; here the wiki
        pool is empty so the bypass path must run.

        Methodology:
          - Seed 3 memories with graded heat (0.9/0.5/0.1) so native order is defined.
          - Force the wiki pool empty (disable the wiki store for this call).
          - Oracle = retriever.recall() native order.
          - Candidate = recall(type="all") memory order.
          - Assert identical relative order (bypass preserved native order, no CE rerank)
            and that no wiki results appear (pool was empty).

        Red on the pre-fix code (fuse_candidates CE-reranks the memory-only pool),
        green after the fix (empty other-pool → native-order bypass).
        """
        storage = e2e_engines["storage"]
        embeddings = e2e_engines["embeddings"]

        unique = "xztype_all_emptywiki733"

        mem_high = _insert_mem(storage, embeddings, f"type all empty wiki high heat {unique}")
        mem_mid = _insert_mem(storage, embeddings, f"type all empty wiki mid heat {unique}")
        mem_low = _insert_mem(storage, embeddings, f"type all empty wiki low heat {unique}")

        storage.update_memory_heat(mem_high, 0.9)
        storage.update_memory_heat(mem_mid, 0.5)
        storage.update_memory_heat(mem_low, 0.1)

        import sys

        monkeypatch.setattr(storage, "update_memory_heat", lambda mid, heat: None)
        monkeypatch.setattr(storage, "update_memory_last_accessed", lambda mid, ts: None)

        from yadgar._shared.runtime import state as _st

        assert _st._retriever is not None, "Retriever must be initialized for order parity test"

        # Oracle: native memory ordering from Retriever.recall().
        legacy_results = _st._retriever.recall(
            f"type all empty wiki {unique}",
            max_results=20,
            min_heat=0.0,
        )
        seeded_ids = {mem_high, mem_mid, mem_low}
        legacy_mem_ids = [m.get("id") for m in legacy_results if m.get("id") in seeded_ids]

        if len(legacy_mem_ids) < 2:
            pytest.skip(
                f"Legacy recall returned fewer than 2 seeded memories "
                f"(got {legacy_mem_ids}); cannot assert order"
            )

        _rm = sys.modules.get("yadgar.core.server.tools.recall")
        if _rm is None:
            import yadgar.core.server.tools.recall as _rm  # type: ignore[no-redef]

        # Force the wiki pool EMPTY for the type="all" call: with _st._wiki=None,
        # WikiProvider is never constructed and wiki_candidates stays [] →
        # exercises the `not wiki_candidates` bypass branch under type="all".
        monkeypatch.setattr(_st, "_wiki", None)

        fanout_results = _rm.recall(
            query=f"type all empty wiki {unique}",
            directory=YADGAR_DIR,
            max_results=20,
            type="all",  # noqa: A002
        )

        # No wiki should appear — the pool was forced empty.
        assert not any(r.get("_source") == "wiki" for r in fanout_results), (
            "wiki pool was forced empty; no wiki results expected"
        )

        fanout_mem_ids = [
            r.get("id")
            for r in fanout_results
            if r.get("_source") == "memory" and r.get("id") in seeded_ids
        ]

        assert fanout_mem_ids == legacy_mem_ids, (
            f"recall(type='all') with an empty wiki pool must preserve memory native "
            f"order (single-provider bypass). legacy_order={legacy_mem_ids}, "
            f"fanout_order={fanout_mem_ids}. If they differ, fuse_candidates is "
            f"CE-reranking the memory-only pool (double-rerank regression, gap d)."
        )

    def test_wiki_query_alias_equivalent_to_type_wiki(
        self, e2e_engines, monkeypatch, caplog, recall_backend_bypass
    ):
        """wiki_query() and recall(type="wiki") return the same wiki slugs.

        Also verifies wiki_query() emits deprecation INFO log (always-on since Phase 2a).
        """
        import sys

        unique = "xztype504"
        wiki_slug = _insert_wiki(
            title=f"Alias Test Wiki {unique}",
            content=f"wiki query alias test {unique}",
        )

        _rm = sys.modules.get("yadgar.core.server.tools.recall")
        if _rm is None:
            import yadgar.core.server.tools.recall as _rm  # type: ignore[no-redef]

        _wm = sys.modules.get("yadgar.core.server.tools.wiki")
        if _wm is None:
            import yadgar.core.server.tools.wiki as _wm  # type: ignore[no-redef]

        try:
            recall_fn = _rm.recall
            recall_results = recall_fn(
                query=f"wiki query alias test {unique}",
                directory=YADGAR_DIR,
                max_results=10,
                type="wiki",  # noqa: A002
            )
            recall_slugs = {
                r.get("slug") or r.get("id") for r in recall_results if r.get("_source") == "wiki"
            }

            wiki_query_fn = _wm.wiki_query
            with caplog.at_level(logging.INFO, logger="yadgar"):
                wiki_results = wiki_query_fn(
                    query=f"wiki query alias test {unique}",
                    directory=YADGAR_DIR,
                    max_results=10,
                )
            wiki_query_slugs = {r.get("slug") or r.get("id") for r in wiki_results}

            # The target slug should be in BOTH result sets
            recall_has_slug = any(wiki_slug in str(s) for s in recall_slugs)
            wiki_query_has_slug = any(wiki_slug in str(s) for s in wiki_query_slugs)

            assert recall_has_slug, (
                f"recall(type='wiki') must return wiki slug='{wiki_slug}'; "
                f"recall_slugs={recall_slugs}"
            )
            assert wiki_query_has_slug, (
                f"wiki_query() must return wiki slug='{wiki_slug}'; "
                f"wiki_query_slugs={wiki_query_slugs}"
            )

            # Deprecation log check: INFO log must mention wiki_query deprecation
            deprecation_logs = [
                r.message
                for r in caplog.records
                if "wiki_query" in r.message and "deprecated" in r.message.lower()
            ]
            assert len(deprecation_logs) >= 1, (
                f"wiki_query() must emit deprecation INFO log when UNIFIED_RECALL_ENABLED=True; "
                f"no deprecation log found. All logs: {[r.message for r in caplog.records]}"
            )

        finally:
            pass
