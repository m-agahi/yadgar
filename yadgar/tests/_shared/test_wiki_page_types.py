"""Tests for v5.53.2 Phase B-schema: page types + templates + format lint.

Covers:
  - PAGE_TYPES registry has required types with non-empty section lists
  - WikiStore.add() stores page_type + wiki_schema_version when provided
  - WikiStore.add() WITHOUT page_type still works (backward compat)
  - wiki_lint flags a typed page missing a required section (warn)
  - wiki_lint passes a well-formed typed page
  - wiki_lint does NOT format-check pages without page_type
  - Catalog groups by page_type when present, falls back to category when absent
"""

import pytest

from yadgar._shared.wiki.contract import WikiAddOptions
from yadgar._shared.wiki.wiki_meta import PAGE_TYPES, WIKI_SCHEMA_VERSION
from yadgar.core import server

# ── Fixtures ─────────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True, scope="module")
def _engines(tmp_path_factory):
    """Full server engine stack with isolated temp database per test."""
    tmp_path = tmp_path_factory.mktemp("wiki_page_types")
    server.init_engines(
        db_path=str(tmp_path / "wiki_types_test.db"),
        embedding_model="all-MiniLM-L6-v2",
    )
    yield
    server.shutdown()


def _wiki():
    return server._wiki


def _storage():
    return server._wiki._storage


# ── A. PAGE_TYPES registry ────────────────────────────────────────────────────


class TestPageTypesRegistry:
    """PAGE_TYPES has the expected starter types with non-empty section lists."""

    def test_registry_has_starter_types(self):
        for expected in ("function", "module", "service", "architecture", "decision"):
            assert expected in PAGE_TYPES, f"Missing page type: {expected!r}"

    def test_registry_has_analysis_type(self):
        assert "analysis" in PAGE_TYPES

    def test_all_types_have_non_empty_sections(self):
        for page_type, sections in PAGE_TYPES.items():
            assert isinstance(sections, list), f"{page_type}: sections must be a list"
            assert len(sections) >= 1, f"{page_type}: must have at least 1 required section"

    def test_function_type_has_expected_sections(self):
        assert "Purpose" in PAGE_TYPES["function"]
        assert "Signature" in PAGE_TYPES["function"]

    def test_module_type_has_expected_sections(self):
        assert "Purpose" in PAGE_TYPES["module"]
        assert "Exports" in PAGE_TYPES["module"]

    def test_decision_type_has_expected_sections(self):
        assert "Context" in PAGE_TYPES["decision"]
        assert "Decision" in PAGE_TYPES["decision"]
        assert "Consequences" in PAGE_TYPES["decision"]

    def test_wiki_schema_version_is_positive(self):
        assert isinstance(WIKI_SCHEMA_VERSION, int)
        assert WIKI_SCHEMA_VERSION >= 1


# ── B. wiki_add with page_type (direct WikiStore) ────────────────────────────


class TestWikiAddPageType:
    """WikiStore.add() stores page_type + wiki_schema_version when provided."""

    def test_add_with_page_type_stores_type(self):
        result = _wiki().add(
            "My Function",
            "## Purpose\nDoes stuff.\n## Signature\nfoo(x)\n## Behaviour\nReturns x.",
            category="reference",
            opts=WikiAddOptions(page_type="function"),
        )
        assert result.get("page_type") == "function"
        assert result.get("wiki_schema_version") == WIKI_SCHEMA_VERSION

    def test_add_with_page_type_persisted_to_storage(self):
        """page_type + wiki_schema_version survive the storage round-trip."""
        _wiki().add(
            "My Function Persisted",
            "## Purpose\nDoes stuff.\n## Signature\nfoo(x)\n## Behaviour\nReturns x.",
            category="reference",
            opts=WikiAddOptions(page_type="function"),
        )
        slug = "my-function-persisted"
        page = _storage().get_wiki_page_by_slug(slug)
        assert page is not None
        assert page.get("page_type") == "function"
        assert page.get("wiki_schema_version") == WIKI_SCHEMA_VERSION

    def test_add_without_page_type_backward_compat(self):
        """wiki_add without page_type still works exactly as before."""
        result = _wiki().add(
            "Old Style Page",
            "Some content without a type.",
            category="reference",
        )
        # No page_type in result — backward compat
        assert result.get("page_type") is None
        # Stored fine
        slug = "old-style-page"
        page = _storage().get_wiki_page_by_slug(slug)
        assert page is not None
        assert page.get("content") == "Some content without a type."

    def test_add_without_page_type_no_schema_version(self):
        """Pages added without page_type should not have wiki_schema_version set."""
        _wiki().add(
            "Untyped Page",
            "Some content.",
            category="reference",
        )
        page = _storage().get_wiki_page_by_slug("untyped-page")
        assert page is not None
        # wiki_schema_version may be None or absent for untyped pages
        assert page.get("wiki_schema_version") is None

    def test_add_module_page_type(self):
        """module page_type stored correctly."""
        result = _wiki().add(
            "My Module",
            "## Purpose\nCore module.\n## Exports\nfoo, bar\n## Design\nSimple.",
            category="reference",
            opts=WikiAddOptions(page_type="module"),
        )
        assert result.get("page_type") == "module"

    def test_upsert_preserves_page_type(self):
        """Updating a page without re-passing page_type preserves existing type."""
        _wiki().add(
            "Typed Page",
            "## Purpose\nOriginal.\n## Signature\nfoo()\n## Behaviour\nOK.",
            category="reference",
            opts=WikiAddOptions(page_type="function"),
        )
        # Update without page_type — should not clobber existing type
        _wiki().add(
            "Typed Page",
            "## Purpose\nUpdated.\n## Signature\nfoo(x)\n## Behaviour\nUpdated.",
            category="reference",
            # page_type intentionally omitted
        )
        # Existing type preserved in storage
        page = _storage().get_wiki_page_by_slug("typed-page")
        assert page is not None
        # The page_type in storage should still be "function" (not overwritten with None)
        assert page.get("page_type") == "function"

    def test_upsert_can_update_page_type(self):
        """Updating a page WITH a new page_type updates the type."""
        _wiki().add(
            "Type Change Page",
            "## Purpose\nOriginal.\n## Signature\nfoo()\n## Behaviour\nOK.",
            category="reference",
            opts=WikiAddOptions(page_type="function"),
        )
        _wiki().add(
            "Type Change Page",
            "## Purpose\nUpdated.\n## Exports\nfoo\n## Design\nNew.",
            category="reference",
            opts=WikiAddOptions(page_type="module"),
        )
        page = _storage().get_wiki_page_by_slug("type-change-page")
        assert page is not None
        assert page.get("page_type") == "module"


# ── C. wiki_lint format checks ────────────────────────────────────────────────


class TestWikiLintPageType:
    """wiki_lint format-checks typed pages; skips untyped."""

    def test_lint_flags_missing_section_on_typed_page(self):
        """A function page missing 'Signature' gets a missing_section violation."""
        _wiki().add(
            "Missing Sig Function",
            "## Purpose\nDoes stuff.\n## Behaviour\nReturns x.",  # no Signature section
            category="reference",
            opts=WikiAddOptions(page_type="function"),
        )
        report = _wiki().lint()
        violations = [i for i in report["issues"] if i["type"] == "missing_section"]
        assert any(i["page"] == "missing-sig-function" for i in violations), (
            f"Expected missing_section for missing-sig-function, got issues: {violations}"
        )
        # Check the violation message mentions the section
        relevant = [i for i in violations if i["page"] == "missing-sig-function"]
        assert any("Signature" in i["message"] for i in relevant)

    def test_lint_passes_well_formed_typed_page(self):
        """A function page with all required sections passes format check."""
        _wiki().add(
            "Complete Function",
            "## Purpose\nDoes stuff.\n## Signature\nfoo(x: int)\n## Behaviour\nReturns x.",
            category="reference",
            opts=WikiAddOptions(page_type="function"),
        )
        report = _wiki().lint()
        violations = [i for i in report["issues"] if i["type"] == "missing_section"]
        relevant = [i for i in violations if i["page"] == "complete-function"]
        assert len(relevant) == 0, f"Unexpected missing_section violations: {relevant}"

    def test_lint_skips_untyped_page(self):
        """Pages without page_type are NOT format-checked by wiki_lint."""
        _wiki().add(
            "Untyped No Sections",
            "This page has no ## sections at all.",
            category="reference",
            # page_type intentionally omitted
        )
        report = _wiki().lint()
        violations = [i for i in report["issues"] if i["type"] == "missing_section"]
        relevant = [i for i in violations if i["page"] == "untyped-no-sections"]
        assert len(relevant) == 0, (
            "Untyped pages must not get missing_section violations. Got: {relevant}"
        )

    def test_lint_missing_section_is_warn_severity(self):
        """Missing section violations have warning severity."""
        _wiki().add(
            "Decision No Context",
            "## Decision\nWe decided X.\n## Consequences\nSome consequences.",
            # missing Context section
            category="decision",
            opts=WikiAddOptions(page_type="decision"),
        )
        report = _wiki().lint()
        violations = [
            i
            for i in report["issues"]
            if i["type"] == "missing_section" and i["page"] == "decision-no-context"
        ]
        assert len(violations) >= 1
        assert all(i["severity"] == "warning" for i in violations)

    def test_lint_format_violation_count_in_stats(self):
        """Stats include format_violation_count reflecting missing sections."""
        _wiki().add(
            "Bad Function",
            "## Purpose\nDoes stuff.",  # missing Signature + Behaviour
            category="reference",
            opts=WikiAddOptions(page_type="function"),
        )
        report = _wiki().lint()
        assert "format_violation_count" in report["stats"]
        assert report["stats"]["format_violation_count"] >= 1

    def test_lint_zero_violations_when_all_typed_pages_well_formed(self):
        """format_violation_count=0 when typed pages are well-formed."""
        _wiki().add(
            "Good Function",
            "## Purpose\nDoes stuff.\n## Signature\nfoo()\n## Behaviour\nOK.",
            category="reference",
            opts=WikiAddOptions(page_type="function"),
        )
        report = _wiki().lint()
        violations = [
            i
            for i in report["issues"]
            if i["type"] == "missing_section" and i["page"] == "good-function"
        ]
        assert len(violations) == 0
        # format_violation_count reflects only this page (no other typed pages in test)
        assert report["stats"]["format_violation_count"] == 0

    def test_lint_case_insensitive_section_match(self):
        """Section heading match is case-insensitive (## purpose == ## Purpose)."""
        _wiki().add(
            "Lowercase Sections",
            "## purpose\nDoes stuff.\n## signature\nfoo()\n## behaviour\nOK.",
            category="reference",
            opts=WikiAddOptions(page_type="function"),
        )
        report = _wiki().lint()
        violations = [
            i
            for i in report["issues"]
            if i["type"] == "missing_section" and i["page"] == "lowercase-sections"
        ]
        assert len(violations) == 0, f"Case-insensitive match failed — violations: {violations}"


# ── D. Catalog page_type grouping ────────────────────────────────────────────


class TestCatalogPageTypeGrouping:
    """_build_wiki_catalog groups by page_type when present, falls back to category."""

    def test_typed_page_groups_by_page_type(self):
        """A page with page_type appears under its page_type key in the catalog."""
        from yadgar.core.server.tools.project import _build_wiki_catalog

        _wiki().add(
            "Fn Example",
            "## Purpose\nP.\n## Signature\nS.\n## Behaviour\nB.",
            category="reference",
            opts=WikiAddOptions(page_type="function"),
        )
        storage = _storage()
        catalog = _build_wiki_catalog(storage, "global")
        groups = catalog.get("groups", {})
        # "function" group should exist
        assert "function" in groups, f"Expected 'function' group, got: {list(groups.keys())}"
        slugs = [p["slug"] for p in groups["function"]["pages"]]
        assert "fn-example" in slugs

    def test_untyped_page_groups_by_category(self):
        """A page without page_type falls back to grouping by category."""
        from yadgar.core.server.tools.project import _build_wiki_catalog

        _wiki().add(
            "Arch Overview",
            "Some architectural overview without page type.",
            category="architecture",
        )
        storage = _storage()
        catalog = _build_wiki_catalog(storage, "global")
        groups = catalog.get("groups", {})
        assert "architecture" in groups, (
            f"Expected 'architecture' group in catalog. Got: {list(groups.keys())}"
        )
        slugs = [p["slug"] for p in groups["architecture"]["pages"]]
        assert "arch-overview" in slugs

    def test_catalog_coexists_typed_and_untyped(self):
        """Typed and untyped pages can coexist in the same catalog."""
        from yadgar.core.server.tools.project import _build_wiki_catalog

        _wiki().add(
            "Fn Thing",
            "## Purpose\nP.\n## Signature\nS.\n## Behaviour\nB.",
            category="reference",
            opts=WikiAddOptions(page_type="function"),
        )
        _wiki().add(
            "Decision Thing",
            "## Context\nC.\n## Decision\nD.\n## Consequences\nCons.",
            category="decision",
            opts=WikiAddOptions(page_type="decision"),
        )
        _wiki().add(
            "Plain Page",
            "No type here.",
            category="pattern",
        )
        storage = _storage()
        catalog = _build_wiki_catalog(storage, "global")
        groups = catalog.get("groups", {})
        # typed pages in type groups
        assert "function" in groups
        assert "decision" in groups
        # untyped page in its category group
        assert "pattern" in groups
        assert catalog["total"] == 3
