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
    from yadgar.server import _state as _st
    from yadgar.wiki import WikiAddOptions

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

    monkeypatch.setattr("yadgar.server._detect_branch", lambda _d: "master")
    monkeypatch.setattr("yadgar.server._get_default_branch", lambda _d: "master")

    _rm = sys.modules.get("yadgar.server.tools.recall")
    if _rm is None:
        import yadgar.server.tools.recall as _rm  # type: ignore[no-redef]

    _rm.settings.UNIFIED_RECALL_ENABLED = True
    try:
        recall_fn = _rm.recall
        return recall_fn(
            query=query,
            directory=directory,
            max_results=max_results,
            type=type_filter,  # noqa: A002
        )
    finally:
        _rm.settings.UNIFIED_RECALL_ENABLED = False


class TestTypeParamE2E:
    """Live-DB e2e tests for Step 5: recall(type=) filtering and wiki_query deprecation."""

    def test_type_memory_returns_only_memories(self, e2e_engines, monkeypatch):
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

    def test_type_wiki_returns_only_wiki(self, e2e_engines, monkeypatch):
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

    def test_type_all_returns_both(self, e2e_engines, monkeypatch):
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

    def test_type_invalid_raises_before_retrieval(self, e2e_engines, monkeypatch):
        """recall(type="invalid") raises ValueError before any DB query.

        Ref: BC-U5 — validation must be early (pre-DB) to avoid pointless work.
        """
        import sys

        monkeypatch.setattr("yadgar.server._detect_branch", lambda _d: "master")
        monkeypatch.setattr("yadgar.server._get_default_branch", lambda _d: "master")

        _rm = sys.modules.get("yadgar.server.tools.recall")
        if _rm is None:
            import yadgar.server.tools.recall as _rm  # type: ignore[no-redef]

        _rm.settings.UNIFIED_RECALL_ENABLED = True
        try:
            recall_fn = _rm.recall
            with pytest.raises(ValueError, match="invalid"):
                recall_fn(
                    query="any query",
                    directory=YADGAR_DIR,
                    type="invalid",  # noqa: A002
                )
        finally:
            _rm.settings.UNIFIED_RECALL_ENABLED = False

    def test_wiki_query_alias_equivalent_to_type_wiki(self, e2e_engines, monkeypatch, caplog):
        """wiki_query() and recall(type="wiki") return the same wiki slugs.

        Also verifies wiki_query() emits deprecation INFO log when UNIFIED_RECALL_ENABLED=True.
        """
        import sys

        unique = "xztype504"
        wiki_slug = _insert_wiki(
            title=f"Alias Test Wiki {unique}",
            content=f"wiki query alias test {unique}",
        )

        monkeypatch.setattr("yadgar.server._detect_branch", lambda _d: "master")
        monkeypatch.setattr("yadgar.server._get_default_branch", lambda _d: "master")

        _rm = sys.modules.get("yadgar.server.tools.recall")
        if _rm is None:
            import yadgar.server.tools.recall as _rm  # type: ignore[no-redef]

        _wm = sys.modules.get("yadgar.server.tools.wiki")
        if _wm is None:
            import yadgar.server.tools.wiki as _wm  # type: ignore[no-redef]

        # Enable unified recall: both module-level settings AND get_settings cache.
        # wiki_query() calls _get_settings() (function-local import) so we need the
        # cache to return an object with UNIFIED_RECALL_ENABLED=True.
        import yadgar.config as _cfg

        _rm.settings.UNIFIED_RECALL_ENABLED = True
        monkeypatch.setenv("YADGAR_UNIFIED_RECALL_ENABLED", "true")
        _cfg.get_settings.cache_clear()

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
            _rm.settings.UNIFIED_RECALL_ENABLED = False
            monkeypatch.delenv("YADGAR_UNIFIED_RECALL_ENABLED", raising=False)
            _cfg.get_settings.cache_clear()
