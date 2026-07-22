"""Tests for yadgar._shared.wiki.repo_wiki_schema.

Car A of #83 (repo-wiki page-type).

Covers:
  - REPO_WIKI_PAGE_TYPE constant
  - repo_wiki_slug(project, module_name) — deterministic, cross-project distinct,
    uses shared slugify over the full string "{project}-mod-{module_name}"
  - validate_repo_wiki_page() — pure validator returning list[str] of errors
"""

from __future__ import annotations

import pytest

# ── Import (RED until repo_wiki_schema.py created) ────────────────────────────
from yadgar._shared.wiki.repo_wiki_schema import (  # noqa: E402
    REPO_WIKI_PAGE_TYPE,
    repo_wiki_slug,
    validate_repo_wiki_page,
)

# ── A. REPO_WIKI_PAGE_TYPE constant ──────────────────────────────────────────


class TestConstant:
    def test_value(self):
        assert REPO_WIKI_PAGE_TYPE == "repo_wiki"

    def test_is_string(self):
        assert isinstance(REPO_WIKI_PAGE_TYPE, str)


# ── B. repo_wiki_slug ─────────────────────────────────────────────────────────


class TestRepoWikiSlug:
    """repo_wiki_slug produces a deterministic, project-namespaced slug."""

    def test_known_value(self):
        """yadgar + yadgar._shared.embeddings → deterministic slug."""
        slug = repo_wiki_slug("yadgar", "yadgar._shared.embeddings")
        # slugify("yadgar-mod-yadgar._shared.embeddings") = "yadgar-mod-yadgar-shared-embeddings"
        assert slug == "yadgar-mod-yadgar-shared-embeddings"

    def test_contains_mod_separator(self):
        """Every slug must contain the '-mod-' separator."""
        slug = repo_wiki_slug("myproject", "some.module")
        assert "-mod-" in slug

    def test_project_prefix_present(self):
        """Project name is the prefix."""
        slug = repo_wiki_slug("myproject", "some.module")
        assert slug.startswith("myproject-")

    def test_cross_project_distinctness(self):
        """Same module_name under different projects produces different slugs."""
        a = repo_wiki_slug("proj_a", "logging")
        b = repo_wiki_slug("proj_b", "logging")
        assert a != b

    def test_determinism(self):
        """Same inputs → same output every call."""
        slug1 = repo_wiki_slug("yadgar", "yadgar.retrieval.core")
        slug2 = repo_wiki_slug("yadgar", "yadgar.retrieval.core")
        assert slug1 == slug2

    def test_dotted_module_name(self):
        """Dots and underscores in module name become hyphens."""
        slug = repo_wiki_slug("yadgar", "yadgar._shared.wiki")
        # slugify("yadgar-mod-yadgar._shared.wiki") → "yadgar-mod-yadgar-shared-wiki"
        assert slug == "yadgar-mod-yadgar-shared-wiki"

    def test_max_length_respected(self):
        """slugify 64-cap applies to the full output."""
        slug = repo_wiki_slug("proj", "very.long." * 20)
        assert len(slug) <= 64

    def test_all_lowercase(self):
        """Output is always lowercase."""
        slug = repo_wiki_slug("MyProject", "Some.Module")
        assert slug == slug.lower()


# ── C. validate_repo_wiki_page ────────────────────────────────────────────────


class TestValidateRepoWikiPage:
    """validate_repo_wiki_page returns [] on valid input, error strings on invalid."""

    # -- happy path --

    def test_valid_accepts(self):
        errors = validate_repo_wiki_page(
            slug="yadgar-mod-yadgar-shared-embeddings",
            source_file="/home/user/yadgar/yadgar/_shared/embeddings.py",
            hash="a" * 64,
        )
        assert errors == []

    def test_valid_hash_is_lowercase_hex(self):
        errors = validate_repo_wiki_page(
            slug="yadgar-mod-some-module",
            source_file="/absolute/path/to/file.py",
            hash="deadbeef" + "0" * 56,  # 64 hex chars
        )
        assert errors == []

    # -- source_file errors --

    def test_missing_source_file(self):
        errors = validate_repo_wiki_page(
            slug="yadgar-mod-some-module",
            source_file=None,
            hash="a" * 64,
        )
        assert len(errors) >= 1
        assert any("source_file" in e for e in errors)

    def test_relative_source_file(self):
        errors = validate_repo_wiki_page(
            slug="yadgar-mod-some-module",
            source_file="relative/path/module.py",
            hash="a" * 64,
        )
        assert len(errors) >= 1
        assert any("source_file" in e for e in errors)

    # -- hash errors --

    def test_missing_hash(self):
        errors = validate_repo_wiki_page(
            slug="yadgar-mod-some-module",
            source_file="/abs/path/module.py",
            hash=None,
        )
        assert len(errors) >= 1
        assert any("hash" in e for e in errors)

    def test_hash_wrong_length(self):
        errors = validate_repo_wiki_page(
            slug="yadgar-mod-some-module",
            source_file="/abs/path/module.py",
            hash="a" * 63,  # one short
        )
        assert len(errors) >= 1
        assert any("hash" in e for e in errors)

    def test_hash_non_hex(self):
        errors = validate_repo_wiki_page(
            slug="yadgar-mod-some-module",
            source_file="/abs/path/module.py",
            hash="z" * 64,  # z is not hex
        )
        assert len(errors) >= 1
        assert any("hash" in e for e in errors)

    def test_hash_uppercase_rejected(self):
        """Hash must be lowercase hex per contract."""
        errors = validate_repo_wiki_page(
            slug="yadgar-mod-some-module",
            source_file="/abs/path/module.py",
            hash="A" * 64,  # uppercase hex
        )
        assert len(errors) >= 1
        assert any("hash" in e for e in errors)

    # -- slug errors --

    def test_slug_without_mod_separator(self):
        errors = validate_repo_wiki_page(
            slug="yadgar-something",
            source_file="/abs/path/module.py",
            hash="a" * 64,
        )
        assert len(errors) >= 1
        assert any("slug" in e for e in errors)

    # -- validator is non-raising --

    def test_does_not_raise_on_all_none(self):
        """Validator returns errors, never raises."""
        try:
            errors = validate_repo_wiki_page(
                slug=None,  # type: ignore[arg-type]
                source_file=None,
                hash=None,
            )
            assert isinstance(errors, list)
            assert len(errors) >= 1
        except Exception as exc:
            pytest.fail(f"validate_repo_wiki_page raised unexpectedly: {exc}")
