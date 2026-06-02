"""RED test — v5.42.1 wiki_page NULL-embedding bug.

Reproduces: find_similar_wiki_pages returns 0 candidates when existing pages
have embedding=NULL (pre-v5.39 rows). SurrealDB KNN operator silently excludes
NULL-embedding rows, so the similarity gate never fires.

Test sequence:
  1. Insert a page with embedding=None (simulating pre-v5.39 row).
  2. Assert find_similar_wiki_pages returns 0 — bug confirmed (RED).
  3. Run backfill (migration_014 logic via WikiStore.backfill_null_embeddings).
  4. Assert find_similar_wiki_pages returns >= 1 — bug fixed (GREEN).
"""

from __future__ import annotations

import pytest

from yadgar import server

# ---------------------------------------------------------------------------
# Content
# ---------------------------------------------------------------------------

_ROADMAP_CONTENT_A = """# Yadgar Roadmap: Future Improvements

## Short-term (next 2 months)
- Implement wiki versioning (v5.41) to track page history
- Add similarity gate to wiki_add to prevent duplicate pages
- Improve embedding model to mpnet for better semantic search

## Medium-term (3-6 months)
- Multi-agent coordination with role specialisation
- Cross-project memory federation
- Automated anchor hygiene with consolidation pass

## Long-term (6+ months)
- LLM-based duplicate resolution and wiki curation
- Retroactive deduplication of existing pages
- Distributed SurrealDB for large-scale deployment
"""

_ROADMAP_CONTENT_B = """# Yadgar Future Roadmap

## Near-term (next 2 months)
- Wiki versioning (v5.41) — track page history and enable rollback
- Similarity gate in wiki_add — block near-duplicate page creation
- Better embedding model (mpnet) for semantic search quality

## Medium-term (3-6 months)
- Multi-agent coordination with role specialisation
- Cross-project memory federation across workspaces
- Automated anchor hygiene during consolidation cycles

## Long-term (6+ months)
- LLM-based wiki curation and duplicate resolution
- Retroactive dedup of existing pages (v5.45+)
- Distributed SurrealDB for large deployments
"""


# ---------------------------------------------------------------------------
# Fixture
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _engines(tmp_path):
    """Isolated temp DB with real embedding model per test."""
    server.init_engines(
        db_path=str(tmp_path / "null_embed_backfill_test.db"),
        embedding_model="all-MiniLM-L6-v2",
    )
    yield
    server.shutdown()


def _wiki():
    return server._wiki


def _storage():
    import yadgar.server._state as _state_mod

    return _state_mod._storage


def _embeddings():
    import yadgar.server._state as _state_mod

    return _state_mod._embeddings


def _insert_page_with_null_embedding(title: str, content: str) -> int:
    """Insert a wiki_page row with embedding=None, simulating a pre-v5.39 row.

    Bypasses WikiStore.add() so _compute_embedding() is never called.
    Returns the new page_id.
    """
    st = _storage()
    slug = _wiki()._slugify(title)
    page_id = st.insert_wiki_page(
        {
            "title": title,
            "slug": slug,
            "content": content,
            "category": None,
            "tags": [],
            "links": [],
            "confidence": 1.0,
            "embedding": None,  # NULL — the pre-v5.39 condition
            "source_memory_ids": [],
        },
        branch=None,
    )
    return page_id


# ---------------------------------------------------------------------------
# Phase 1 — RED: bug reproduction
# ---------------------------------------------------------------------------


class TestNullEmbeddingBugReproduction:
    """These tests reproduce the NULL-embedding bug BEFORE backfill.

    Status: RED (returns 0 candidates). Passes after migration_014 backfill runs.
    """

    def test_find_similar_returns_zero_for_null_embedding_page(self):
        """BUG: existing page with embedding=NULL is excluded from KNN results.

        find_similar_wiki_pages returns 0 candidates even though the stored page
        has nearly identical content to the query. This is the root cause: KNN
        operator <|fetch_k,40|> silently excludes NULL rows.
        """
        _insert_page_with_null_embedding(
            "Yadgar Roadmap Future Improvements",
            _ROADMAP_CONTENT_A,
        )

        candidates = _wiki().find_similar_wiki_pages(
            title="Yadgar Future Roadmap",
            content=_ROADMAP_CONTENT_B,
            branch=None,
            threshold=0.80,
        )

        # BUG: returns 0 because embedding is NULL → KNN excludes the row.
        # After migration_014 backfill, this will return >= 1.
        assert len(candidates) == 0, (
            f"Expected 0 candidates (bug not reproduced) but got: {candidates}. "
            "If this test passes, the NULL-embedding exclusion bug is already fixed."
        )

    def test_multiple_null_embedding_pages_all_excluded(self):
        """All NULL-embedding rows are invisible to KNN — regardless of count."""
        for i in range(3):
            _insert_page_with_null_embedding(
                f"Yadgar Roadmap v{i}",
                _ROADMAP_CONTENT_A,
            )

        candidates = _wiki().find_similar_wiki_pages(
            title="Yadgar Future Roadmap",
            content=_ROADMAP_CONTENT_B,
            branch=None,
            threshold=0.50,  # very low threshold — any match should show up
        )
        assert len(candidates) == 0, f"Expected 0 (all rows NULL → all excluded). Got: {candidates}"

    def test_get_wiki_pages_without_embedding_returns_null_rows(self):
        """storage.get_wiki_pages_without_embedding() enumerates NULL-embedding rows.

        This is the storage query that migration_014 uses to find rows to backfill.
        Confirms the storage method works before testing the full backfill.
        """
        pid1 = _insert_page_with_null_embedding("Page Alpha", "Alpha content about configuration.")
        pid2 = _insert_page_with_null_embedding("Page Beta", "Beta content about benchmarks.")

        missing = _storage().get_wiki_pages_without_embedding()
        ids = [r["id"] for r in missing]

        assert pid1 in ids, f"pid1={pid1} not in missing list: {ids}"
        assert pid2 in ids, f"pid2={pid2} not in missing list: {ids}"

    def test_get_wiki_pages_without_embedding_skips_populated_rows(self):
        """get_wiki_pages_without_embedding() excludes rows that already have embeddings."""
        # Insert one row with a real embedding (via normal path).
        import yadgar.file_queue._locals as _loc

        _loc._drain_local.active = True
        try:
            server.wiki_add(
                title="Fully Embedded Page",
                content="This page has a real embedding computed by _compute_embedding.",
            )
        finally:
            _loc._drain_local.active = False

        # Insert one NULL-embedding row.
        pid_null = _insert_page_with_null_embedding("Null Embed Page", "Content here.")

        missing = _storage().get_wiki_pages_without_embedding()
        ids = [r["id"] for r in missing]

        # Only the NULL row appears.
        assert pid_null in ids
        assert len(ids) == 1, f"Expected exactly 1 NULL-embedding row, got {len(ids)}: {ids}"


# ---------------------------------------------------------------------------
# Phase 2 — GREEN: backfill fixes the bug
# ---------------------------------------------------------------------------


class TestBackfillFixesBug:
    """After backfill, find_similar_wiki_pages returns candidates correctly."""

    def test_backfill_makes_near_duplicate_detectable(self):
        """After backfill, near-duplicate page is detected by similarity gate.

        Sequence:
        1. Insert page A with NULL embedding (bug state).
        2. Confirm find_similar returns 0 (bug reproduced).
        3. Run backfill.
        4. Confirm find_similar returns >= 1 (bug fixed).
        """
        _insert_page_with_null_embedding(
            "Yadgar Roadmap Future Improvements",
            _ROADMAP_CONTENT_A,
        )

        # Step 2: bug confirmed
        before = _wiki().find_similar_wiki_pages(
            title="Yadgar Future Roadmap",
            content=_ROADMAP_CONTENT_B,
            branch=None,
            threshold=0.80,
        )
        assert len(before) == 0, f"Expected 0 before backfill, got {before}"

        # Step 3: run backfill
        backfilled = _wiki().backfill_null_embeddings()
        assert backfilled >= 1, f"Backfill should have processed >= 1 row, got {backfilled}"

        # Step 4: bug fixed
        after = _wiki().find_similar_wiki_pages(
            title="Yadgar Future Roadmap",
            content=_ROADMAP_CONTENT_B,
            branch=None,
            threshold=0.80,
        )
        assert len(after) >= 1, (
            f"Expected >= 1 candidate after backfill but got 0. "
            f"Backfill returned {backfilled}. Similarity gate still non-functional."
        )
        slugs = [c["slug"] for c in after]
        assert "yadgar-roadmap-future-improvements" in slugs

    def test_backfill_idempotent(self):
        """Running backfill twice does not double-embed or error."""
        _insert_page_with_null_embedding("Page A", _ROADMAP_CONTENT_A)

        first_run = _wiki().backfill_null_embeddings()
        second_run = _wiki().backfill_null_embeddings()

        assert first_run >= 1
        assert second_run == 0, (
            f"Second backfill run should find 0 NULL rows (idempotent). Got {second_run}."
        )

    def test_backfill_skips_already_embedded_rows(self):
        """Backfill leaves pages with existing embeddings untouched."""
        import yadgar.file_queue._locals as _loc

        # Insert a real page (has embedding).
        _loc._drain_local.active = True
        try:
            server.wiki_add(title="Embedded Page", content="Has an embedding already.")
        finally:
            _loc._drain_local.active = False

        # Insert NULL page.
        _insert_page_with_null_embedding("Null Page", "Lacks embedding.")

        backfilled = _wiki().backfill_null_embeddings()
        assert backfilled == 1, (
            f"Expected exactly 1 row backfilled (only the NULL one). Got {backfilled}."
        )

    def test_backfill_returns_count(self):
        """backfill_null_embeddings() returns the number of rows processed."""
        for i in range(5):
            _insert_page_with_null_embedding(f"Page {i}", f"Content for page number {i}.")

        count = _wiki().backfill_null_embeddings()
        assert count == 5, f"Expected 5 rows backfilled, got {count}"
