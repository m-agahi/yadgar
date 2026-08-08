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

from yadgar._shared.wiki import WikiAddOptions
from yadgar.core import server

pytestmark = pytest.mark.usefixtures("recall_backend_bypass")


# ── Fixtures ─────────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True, scope="module")
def _engines(tmp_path_factory):
    """Full server engine stack with isolated temp database per test."""
    tmp_path = tmp_path_factory.mktemp("wiki")
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

    def test_html_entity_ampersand_normalised(self):
        """v5.24.1: &amp; in title must not leak 'amp' into slug (Bug 2)."""
        assert _wiki()._slugify("Yadgar Roadmap &amp; Future Improvements") == (
            "yadgar-roadmap-future-improvements"
        )

    def test_raw_ampersand_normalised(self):
        """& (raw) and &amp; (entity) produce identical slugs."""
        assert _wiki()._slugify("Foo &amp; Bar") == _wiki()._slugify("Foo & Bar")


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
        _wiki().add("Conf Test", "Content.", "reference", opts=WikiAddOptions(confidence="high"))
        _wiki().add("Conf Test", "New content.", "reference", opts=WikiAddOptions(confidence="low"))
        page = _wiki().read("conf-test")
        assert page["confidence"] == "high"

    def test_upsert_merges_source_memory_ids(self):
        _wiki().add(
            "Source Test", "C1.", "reference", opts=WikiAddOptions(source_memory_ids=[1, 2])
        )
        _wiki().add("Source Test", "C2.", "reference", opts=WikiAddOptions(source_memory_ids=[3]))
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
        _wiki().add(
            "Low Conf Page", "Uncertain info.", "reference", opts=WikiAddOptions(confidence="low")
        )
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
        result = _wiki().add(
            "Bad Conf", "Content.", "reference", opts=WikiAddOptions(confidence="invalid")
        )
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
            opts=WikiAddOptions(confidence="high"),
        )
        # Also store a memory so recall has something to blend with
        server.memorize(
            content="Yadgar uses SurrealDB for storage.",
            context="/tmp/test",
            tags=["test"],
        )
        flush_queue()
        results = server.recall(query="yadgar architecture", max_results=5, directory="/tmp/test")
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
        results = server.recall(query="plain memory", max_results=5, directory="/tmp/test")
        assert len(results) >= 1


# ── K. v5.41.0 Versioning Regression Guards ────────────────────────────────


class TestVersioningRegression:
    """wiki_add produces version=1; subsequent writes increment version."""

    def test_wiki_add_produces_version_1(self):
        """wiki_add (insert path) creates version=1 row (v5.41.0 regression guard)."""
        from yadgar._shared.storage.migrations import (
            _migration_013_wiki_page_version,  # noqa: PLC0415
        )

        storage = server._get_storage()
        _migration_013_wiki_page_version(storage)  # DDL + seed (idempotent)

        result = _wiki().add("Versioning Test", "initial content", "reference", ["v5.41"])
        pid = result["id"]
        rows = storage._q(
            "SELECT * FROM wiki_page_version WHERE page_id = $p",
            {"p": pid},
        )
        assert len(rows) == 1, f"Expected 1 version row after add, got {len(rows)}"
        assert rows[0]["version"] == 1

    def test_wiki_add_upsert_produces_version_2(self):
        """wiki_add upsert (update path) creates version=2 row (v5.41.0 regression guard)."""
        from yadgar._shared.storage.migrations import (
            _migration_013_wiki_page_version,  # noqa: PLC0415
        )

        storage = server._get_storage()
        _migration_013_wiki_page_version(storage)

        _wiki().add("Upsert Version", "v1 content", "reference")
        result2 = _wiki().add("Upsert Version", "v2 content", "reference")
        pid = result2["id"]

        rows = storage._q(
            "SELECT * FROM wiki_page_version WHERE page_id = $p ORDER BY version ASC",
            {"p": pid},
        )
        assert len(rows) == 2, f"Expected 2 version rows after upsert, got {len(rows)}"
        assert rows[0]["version"] == 1
        assert rows[1]["version"] == 2


# ── L. Auto-linking pass (v5.85 car #5) ──────────────────────────────────────


class TestAutolink:
    """wiki autolink inserts [[slug]] cross-refs by matching other pages' titles.

    Safety guards (non-negotiable): dry-run default, verbatim/fence guard,
    length/specificity guard, similarity guard, idempotency, and no metadata
    clobbering on the apply path.
    """

    _DIR = "/home/max/git/yadgar"

    def _opts(self, **kw):
        kw.setdefault("directory_context", self._DIR)
        return WikiAddOptions(**kw)

    def test_autolink_inserts_link(self):
        """Title of A mentioned in B's body → apply wraps it in [[a-slug]]."""
        _wiki().add(
            "Recall Pipeline",
            "How retrieval scoring works.",
            "reference",
            opts=self._opts(),
        )
        _wiki().add(
            "Memorize Path",
            "The Recall Pipeline ranks memories before storage.",
            "reference",
            opts=self._opts(),
        )
        result = _wiki().autolink(directory=self._DIR, dry_run=False, similarity_threshold=0.0)
        page = _wiki().read("memorize-path")
        assert "[[recall-pipeline]]" in page["content"]
        assert "recall-pipeline" in page["links"]
        backlinks = _wiki()._storage.get_wiki_backlinks("recall-pipeline")
        assert "memorize-path" in backlinks
        assert result["applied"] is True

    def test_autolink_dry_run_no_mutation(self):
        """dry_run=True (DEFAULT) returns proposals but never mutates content."""
        _wiki().add("Recall Pipeline", "scoring details", "reference", opts=self._opts())
        _wiki().add(
            "Memorize Path",
            "The Recall Pipeline ranks memories.",
            "reference",
            opts=self._opts(),
        )
        before = _wiki().read("memorize-path")["content"]
        result = _wiki().autolink(directory=self._DIR, similarity_threshold=0.0)
        after = _wiki().read("memorize-path")["content"]
        assert after == before  # unchanged
        assert result["applied"] is False
        assert any(
            p["page"] == "memorize-path" and p["target"] == "recall-pipeline"
            for p in result["proposals"]
        )

    def test_autolink_default_is_dry_run(self):
        """Calling autolink with no dry_run arg must NOT mutate (safe default)."""
        _wiki().add("Recall Pipeline", "scoring details", "reference", opts=self._opts())
        _wiki().add(
            "Memorize Path",
            "The Recall Pipeline ranks memories.",
            "reference",
            opts=self._opts(),
        )
        before = _wiki().read("memorize-path")["content"]
        _wiki().autolink(directory=self._DIR, similarity_threshold=0.0)
        assert _wiki().read("memorize-path")["content"] == before

    def test_autolink_skips_code_fences(self):
        """A title inside a fenced code block is NOT wrapped."""
        _wiki().add("Recall Pipeline", "scoring details", "reference", opts=self._opts())
        _wiki().add(
            "Memorize Path",
            "Run it:\n\n```\nRecall Pipeline --flag\n```\n",
            "reference",
            opts=self._opts(),
        )
        _wiki().autolink(directory=self._DIR, dry_run=False, similarity_threshold=0.0)
        page = _wiki().read("memorize-path")
        assert "[[recall-pipeline]]" not in page["content"]

    def test_autolink_skips_inline_code(self):
        """A title inside inline `backticks` is NOT wrapped."""
        _wiki().add("Recall Pipeline", "scoring details", "reference", opts=self._opts())
        _wiki().add(
            "Memorize Path",
            "Invoke `Recall Pipeline` to score.",
            "reference",
            opts=self._opts(),
        )
        _wiki().autolink(directory=self._DIR, dry_run=False, similarity_threshold=0.0)
        page = _wiki().read("memorize-path")
        assert "[[recall-pipeline]]" not in page["content"]

    def test_autolink_idempotent(self):
        """Run twice; second run reports 0 insertions, no double-wrap."""
        _wiki().add("Recall Pipeline", "scoring details", "reference", opts=self._opts())
        _wiki().add(
            "Memorize Path",
            "The Recall Pipeline ranks memories.",
            "reference",
            opts=self._opts(),
        )
        _wiki().autolink(directory=self._DIR, dry_run=False, similarity_threshold=0.0)
        content_after_first = _wiki().read("memorize-path")["content"]
        result2 = _wiki().autolink(directory=self._DIR, dry_run=False, similarity_threshold=0.0)
        content_after_second = _wiki().read("memorize-path")["content"]
        assert content_after_first == content_after_second
        assert content_after_second.count("[[recall-pipeline]]") == 1
        assert len(result2["proposals"]) == 0

    def test_autolink_skips_already_linked(self):
        """A page that already links the target is left untouched (idempotency)."""
        _wiki().add("Recall Pipeline", "scoring details", "reference", opts=self._opts())
        _wiki().add(
            "Memorize Path",
            "See [[recall-pipeline]] — the Recall Pipeline ranks memories.",
            "reference",
            opts=self._opts(),
        )
        before = _wiki().read("memorize-path")["content"]
        _wiki().autolink(directory=self._DIR, dry_run=False, similarity_threshold=0.0)
        after = _wiki().read("memorize-path")["content"]
        assert after == before
        assert after.count("[[recall-pipeline]]") == 1

    def test_autolink_skips_short_titles(self):
        """A title shorter than min_title_len is never auto-linked."""
        _wiki().add("API", "interface stuff", "reference", opts=self._opts())
        _wiki().add(
            "Memorize Path",
            "The API is used everywhere in API code.",
            "reference",
            opts=self._opts(),
        )
        result = _wiki().autolink(
            directory=self._DIR, dry_run=False, min_title_len=6, similarity_threshold=0.0
        )
        page = _wiki().read("memorize-path")
        assert "[[api]]" not in page["content"]
        assert not any(p["target"] == "api" for p in result["proposals"])

    def test_autolink_word_boundary(self):
        """A title must match on word boundaries — 'Memory' must not hit 'Memorize'."""
        _wiki().add("Memory", "memory subsystem", "reference", opts=self._opts())
        _wiki().add(
            "Other Page",
            "We Memorize things in the store.",
            "reference",
            opts=self._opts(),
        )
        _wiki().autolink(directory=self._DIR, dry_run=False, similarity_threshold=0.0)
        page = _wiki().read("other-page")
        assert "[[memory]]" not in page["content"]
        assert "Memorize" in page["content"]

    def test_autolink_semantic_guard_rejects(self):
        """A coincidental title match below the similarity threshold is rejected."""
        _wiki().add("Recall Pipeline", "scoring details", "reference", opts=self._opts())
        _wiki().add(
            "Memorize Path",
            "The Recall Pipeline ranks memories.",
            "reference",
            opts=self._opts(),
        )
        # Threshold of 0.999 — no two distinct fixtures clear it → rejected.
        result = _wiki().autolink(
            directory=self._DIR,
            dry_run=False,
            similarity_threshold=0.999,
            semantic_guard=True,
        )
        page = _wiki().read("memorize-path")
        assert "[[recall-pipeline]]" not in page["content"]
        assert len(result["proposals"]) == 0

    def test_autolink_no_self_link(self):
        """A page that mentions its own title is not linked to itself."""
        _wiki().add(
            "Recall Pipeline",
            "The Recall Pipeline scores memories.",
            "reference",
            opts=self._opts(),
        )
        _wiki().autolink(directory=self._DIR, dry_run=False, similarity_threshold=0.0)
        page = _wiki().read("recall-pipeline")
        assert "[[recall-pipeline]]" not in page["content"]

    def test_autolink_preserves_metadata_on_apply(self):
        """Apply must NOT clobber a page's directory_context or category.

        WikiStore.add() upsert defaults category→'reference' and
        directory_context→'global'. The apply path must read each page's own
        metadata and pass it back, or curated pages silently move to 'global'.
        """
        _wiki().add("Recall Pipeline", "scoring details", "decision", opts=self._opts())
        _wiki().add(
            "Memorize Path",
            "The Recall Pipeline ranks memories.",
            "decision",
            opts=self._opts(),
        )
        _wiki().autolink(directory=self._DIR, dry_run=False, similarity_threshold=0.0)
        page = _wiki().read("memorize-path")
        assert page["content"].__contains__("[[recall-pipeline]]")
        assert page["directory_context"] == self._DIR  # NOT moved to 'global'
        assert page["category"] == "decision"  # NOT reset to 'reference'

    def test_autolink_directory_scoped(self):
        """A page in project A is not linked into a page from project B."""
        _wiki().add(
            "Recall Pipeline",
            "scoring details",
            "reference",
            opts=WikiAddOptions(directory_context="/home/max/git/projectA"),
        )
        _wiki().add(
            "Memorize Path",
            "The Recall Pipeline ranks memories.",
            "reference",
            opts=WikiAddOptions(directory_context="/home/max/git/projectB"),
        )
        result = _wiki().autolink(
            directory="/home/max/git/projectB", dry_run=False, similarity_threshold=0.0
        )
        page = _wiki().read("memorize-path")
        assert "[[recall-pipeline]]" not in page["content"]
        assert len(result["proposals"]) == 0
