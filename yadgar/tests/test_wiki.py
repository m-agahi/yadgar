"""E2E behavioral tests for the Yadgar wiki subsystem.

Tests verify:
  - CRUD operations (add, read, delete, list)
  - Upsert semantics (add with existing slug updates)
  - Ingest merge strategy (append with timestamp)
  - Wikilink extraction from [[slug]] patterns
  - Cross-reference tracking (sync on add/delete)
  - Hybrid search (FTS + vector)
  - Lint detection (orphans, broken refs, low confidence)
  - Slug generation edge cases
  - Category/confidence validation
  - Recall integration (wiki results blended with memories)
"""

import pytest

from yadgar import server

pytestmark = pytest.mark.xdist_group("server_globals")

# ── Fixtures ─────────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _engines(tmp_path):
    """Full server engine stack with isolated temp database per test."""
    server.init_engines(
        db_path=str(tmp_path / "wiki_test.db"),
        embedding_model="all-MiniLM-L6-v2",
    )
    yield
    server.shutdown()


def _wiki():
    return server._wiki


# ── A. Slug Generation ──────────────────────────────────────────────────────


class TestSlugGeneration:
    """Slugs are lowercase, alphanumeric + hyphens, max 64 chars."""

    def test_basic_title(self):
        assert _wiki()._slugify("Hello World") == "hello-world"

    def test_special_characters(self):
        assert _wiki()._slugify("Auth (v2) — Design!") == "auth-v2-design"

    def test_max_length(self):
        slug = _wiki()._slugify("a" * 100)
        assert len(slug) <= 64

    def test_empty_title(self):
        assert _wiki()._slugify("") == "untitled"

    def test_numeric_title(self):
        assert _wiki()._slugify("123") == "123"


# ── B. Wikilink Extraction ──────────────────────────────────────────────────


class TestWikilinkExtraction:
    """[[slug]] patterns are extracted from content."""

    def test_single_link(self):
        links = _wiki()._extract_wikilinks("See [[my-page]] for details.")
        assert links == ["my-page"]

    def test_multiple_links(self):
        links = _wiki()._extract_wikilinks("See [[page-a]] and [[page-b]].")
        assert links == ["page-a", "page-b"]

    def test_duplicate_links_deduplicated(self):
        links = _wiki()._extract_wikilinks("[[page-a]] and [[page-a]] again.")
        assert links == ["page-a"]

    def test_no_links(self):
        links = _wiki()._extract_wikilinks("No links here.")
        assert links == []

    def test_title_in_brackets_gets_slugified(self):
        links = _wiki()._extract_wikilinks("See [[My Cool Page]].")
        assert links == ["my-cool-page"]


# ── C. CRUD Operations ──────────────────────────────────────────────────────


class TestCRUD:
    """Basic create, read, update, delete operations."""

    def test_add_and_read(self):
        result = _wiki().add("Test Page", "Some content.", "reference", ["test"])
        assert result["slug"] == "test-page"
        assert result["id"] is not None

        page = _wiki().read("test-page")
        assert page is not None
        assert page["title"] == "Test Page"
        assert page["content"] == "Some content."
        assert page["category"] == "reference"
        assert "test" in page["tags"]

    def test_read_nonexistent(self):
        assert _wiki().read("no-such-page") is None

    def test_delete(self):
        _wiki().add("Delete Me", "Content.", "reference")
        assert _wiki().delete("delete-me") is True
        assert _wiki().read("delete-me") is None

    def test_delete_nonexistent(self):
        assert _wiki().delete("no-such-page") is False

    def test_list_pages(self):
        _wiki().add("Page A", "Content A", "architecture")
        _wiki().add("Page B", "Content B", "decision")
        pages = _wiki().list_pages()
        slugs = [p["slug"] for p in pages]
        assert "page-a" in slugs
        assert "page-b" in slugs

    def test_list_pages_filtered_by_category(self):
        _wiki().add("Arch Page", "Architecture stuff", "architecture")
        _wiki().add("Debug Page", "Debugging stuff", "debugging")
        pages = _wiki().list_pages(category="architecture")
        slugs = [p["slug"] for p in pages]
        assert "arch-page" in slugs
        assert "debug-page" not in slugs


# ── D. Upsert Semantics ─────────────────────────────────────────────────────


class TestUpsert:
    """Adding with an existing slug updates the page."""

    def test_upsert_updates_content(self):
        _wiki().add("Upsert Test", "Original content.", "reference")
        _wiki().add("Upsert Test", "Updated content.", "reference")
        page = _wiki().read("upsert-test")
        assert page["content"] == "Updated content."

    def test_upsert_merges_tags(self):
        _wiki().add("Tag Test", "Content.", "reference", ["tag-a"])
        _wiki().add("Tag Test", "New content.", "reference", ["tag-b"])
        page = _wiki().read("tag-test")
        assert "tag-a" in page["tags"]
        assert "tag-b" in page["tags"]

    def test_upsert_keeps_higher_confidence(self):
        _wiki().add("Conf Test", "Content.", "reference", confidence="high")
        _wiki().add("Conf Test", "New content.", "reference", confidence="low")
        page = _wiki().read("conf-test")
        assert page["confidence"] == "high"

    def test_upsert_merges_source_memory_ids(self):
        _wiki().add("Source Test", "C1.", "reference", source_memory_ids=[1, 2])
        _wiki().add("Source Test", "C2.", "reference", source_memory_ids=[3])
        page = _wiki().read("source-test")
        assert 1 in page["source_memory_ids"]
        assert 3 in page["source_memory_ids"]


# ── E. Ingest (Append Merge) ────────────────────────────────────────────────


class TestIngest:
    """Ingest appends to existing pages with timestamp separators."""

    def test_ingest_creates_new_page(self):
        result = _wiki().ingest("New content.", title="Ingest New")
        assert result["slug"] == "ingest-new"
        page = _wiki().read("ingest-new")
        assert page is not None
        assert "New content." in page["content"]

    def test_ingest_appends_to_existing(self):
        _wiki().add("Ingest Existing", "Original.", "reference")
        _wiki().ingest("Appended content.", title="Ingest Existing")
        page = _wiki().read("ingest-existing")
        assert "Original." in page["content"]
        assert "Appended content." in page["content"]
        assert "---" in page["content"]  # separator
        assert "## Update" in page["content"]

    def test_ingest_merges_tags(self):
        _wiki().add("Ingest Tags", "Content.", "reference", ["tag-1"])
        _wiki().ingest("More.", title="Ingest Tags", tags=["tag-2"])
        page = _wiki().read("ingest-tags")
        assert "tag-1" in page["tags"]
        assert "tag-2" in page["tags"]


# ── F. Cross-References ─────────────────────────────────────────────────────


class TestCrossReferences:
    """Wikilinks create cross-reference records."""

    def test_crossrefs_created_on_add(self):
        _wiki().add("Page With Links", "See [[target-page]] for details.", "reference")
        backlinks = _wiki()._storage.get_wiki_backlinks("target-page")
        assert "page-with-links" in backlinks

    def test_crossrefs_updated_on_upsert(self):
        _wiki().add("Link Page", "See [[old-target]].", "reference")
        _wiki().add("Link Page", "Now see [[new-target]].", "reference")
        old_backlinks = _wiki()._storage.get_wiki_backlinks("old-target")
        new_backlinks = _wiki()._storage.get_wiki_backlinks("new-target")
        assert "link-page" not in old_backlinks
        assert "link-page" in new_backlinks

    def test_crossrefs_cleaned_on_delete(self):
        _wiki().add("Deleting Linker", "See [[some-target]].", "reference")
        assert "deleting-linker" in _wiki()._storage.get_wiki_backlinks("some-target")
        _wiki().delete("deleting-linker")
        assert "deleting-linker" not in _wiki()._storage.get_wiki_backlinks("some-target")

    def test_incoming_crossrefs_cleaned_when_target_deleted(self):
        """Deleting a page must remove wiki_crossref rows pointing TO it.

        Previously delete_wiki_page only removed the wiki_page row, leaving
        dangling to_slug crossrefs behind.
        """
        _wiki().add("Source Page", "See [[target-to-delete]].", "reference")
        assert "source-page" in _wiki()._storage.get_wiki_backlinks("target-to-delete")

        # Add a target page so we can delete it by ID via the storage layer
        _wiki().add("Target To Delete", "I will be deleted.", "reference")

        # Delete the target page — incoming crossref from source-page must be removed
        _wiki().delete("target-to-delete")

        # No dangling to_slug row should survive
        backlinks = _wiki()._storage.get_wiki_backlinks("target-to-delete")
        assert backlinks == []

        # Cross-reference table should have no row with to_slug = target-to-delete
        all_refs = _wiki()._storage.get_all_wiki_crossrefs()
        assert not any(r["to_slug"] == "target-to-delete" for r in all_refs)


# ── G. Hybrid Search ────────────────────────────────────────────────────────


class TestSearch:
    """Hybrid FTS + vector search finds relevant pages."""

    def test_query_finds_by_keyword(self):
        _wiki().add("SurrealDB Guide", "SurrealDB is an embedded database engine.", "reference")
        results = _wiki().query("SurrealDB")
        assert len(results) >= 1
        assert any(r["slug"] == "surrealdb-guide" for r in results)

    def test_query_finds_by_semantic(self):
        _wiki().add(
            "Memory Decay",
            "Heat decreases over time following an exponential curve with configurable factors.",
            "architecture",
        )
        results = _wiki().query("how does memory temperature change")
        assert len(results) >= 1
        assert any(r["slug"] == "memory-decay" for r in results)

    def test_query_returns_scores(self):
        _wiki().add("Scored Page", "This page has scored content.", "reference")
        results = _wiki().query("scored content")
        assert len(results) >= 1
        assert "_retrieval_score" in results[0]
        assert results[0]["_retrieval_score"] > 0

    def test_query_filters_by_category(self):
        _wiki().add("Arch Item", "Architecture content.", "architecture")
        _wiki().add("Debug Item", "Debug content.", "debugging")
        results = _wiki().query("content", category="architecture")
        slugs = [r["slug"] for r in results]
        assert "arch-item" in slugs
        assert "debug-item" not in slugs

    def test_query_filters_by_tags(self):
        _wiki().add("Tagged A", "Some content.", "reference", ["alpha"])
        _wiki().add("Tagged B", "Some content.", "reference", ["beta"])
        results = _wiki().query("content", tags=["alpha"])
        slugs = [r["slug"] for r in results]
        assert "tagged-a" in slugs
        assert "tagged-b" not in slugs

    def test_query_no_results(self):
        results = _wiki().query("xyzzy nonexistent gibberish")
        assert results == []


# ── H. Lint ──────────────────────────────────────────────────────────────────


class TestLint:
    """Lint detects orphans, broken refs, and low confidence."""

    def test_lint_detects_orphan(self):
        _wiki().add("Orphan Page", "No one links here.", "reference")
        report = _wiki().lint()
        orphans = [i for i in report["issues"] if i["type"] == "orphan"]
        assert any(i["page"] == "orphan-page" for i in orphans)
        assert report["stats"]["orphan_count"] >= 1

    def test_lint_detects_broken_ref(self):
        _wiki().add("Broken Ref Page", "See [[nonexistent-page]].", "reference")
        report = _wiki().lint()
        broken = [i for i in report["issues"] if i["type"] == "broken_ref"]
        assert any(i["page"] == "broken-ref-page" for i in broken)
        assert report["stats"]["broken_ref_count"] >= 1

    def test_lint_detects_low_confidence(self):
        _wiki().add("Low Conf Page", "Uncertain info.", "reference", confidence="low")
        report = _wiki().lint()
        low = [i for i in report["issues"] if i["type"] == "low_confidence"]
        assert any(i["page"] == "low-conf-page" for i in low)
        assert report["stats"]["low_confidence_count"] >= 1

    def test_lint_no_issues_on_linked_pages(self):
        _wiki().add("Hub Page", "See [[spoke-page]].", "reference")
        _wiki().add("Spoke Page", "See [[hub-page]].", "reference")
        report = _wiki().lint()
        orphans = [i for i in report["issues"] if i["type"] == "orphan"]
        orphan_slugs = [i["page"] for i in orphans]
        assert "hub-page" not in orphan_slugs
        assert "spoke-page" not in orphan_slugs


# ── I. Category & Confidence Validation ──────────────────────────────────────


class TestValidation:
    """Invalid category/confidence falls back to defaults."""

    def test_invalid_category_defaults_to_reference(self):
        result = _wiki().add("Bad Cat", "Content.", "invalid_category")
        assert result["category"] == "reference"

    def test_invalid_confidence_defaults_to_medium(self):
        result = _wiki().add("Bad Conf", "Content.", "reference", confidence="invalid")
        assert result["confidence"] == "medium"


# ── J. Recall Integration ────────────────────────────────────────────────────


class TestRecallIntegration:
    """Wiki results are blended into recall() output."""

    def test_recall_includes_wiki_results(self, flush_queue):
        _wiki().add(
            "Yadgar Architecture",
            "Yadgar is a biologically-inspired memory engine with WRRF retrieval.",
            "architecture",
            ["core"],
            confidence="high",
        )
        # Also store a memory so recall has something to blend with
        server.memorize(
            content="Yadgar uses SurrealDB for storage.",
            context="/tmp/test",
            tags=["test"],
        )
        flush_queue()
        results = server.recall(query="yadgar architecture", max_results=5)
        wiki_results = [r for r in results if r.get("_source") == "wiki"]
        assert len(wiki_results) >= 1
        assert wiki_results[0].get("title") == "Yadgar Architecture"

    def test_recall_without_wiki_still_works(self, flush_queue):
        server.memorize(
            content="A plain memory without any wiki pages related.",
            context="/tmp/test",
            tags=["test"],
        )
        flush_queue()
        results = server.recall(query="plain memory", max_results=5)
        assert len(results) >= 1
