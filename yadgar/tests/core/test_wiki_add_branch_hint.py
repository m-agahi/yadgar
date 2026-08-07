"""Tests for wiki_add branch_hint arg (v5.4 W1).

Resolution priority:
1. Explicit branch (non-empty) → use as-is.
2. branch_hint (non-empty, branch omitted/None) → use as branch.
3. Both None → store with branch IS NULL (canonical slot).
4. branch AND branch_hint both provided → explicit branch wins.

Old _detect_branch(os.getcwd()) fallback is removed.

R3 migration: wiki_add always enqueues (no sync path). Tests request
_unit_backend_harness to get drainer and drain after enqueue.
directory= is now required by the MCP boundary check.
"""

from __future__ import annotations

import pytest

from yadgar.core import server

_DIR = "/home/user/wiki-hint-test"


@pytest.fixture(autouse=True, scope="module")
def _engines(tmp_path_factory):
    tmp_path = tmp_path_factory.mktemp("wiki_add_branch_hint")
    db_path = str(tmp_path / "test.db")
    server.init_engines(db_path=db_path, embedding_model="all-MiniLM-L6-v2")
    yield
    server.shutdown()


def _get_page_branch(slug: str) -> str | None:
    """Retrieve the branch stored on a wiki_page row."""
    wiki = server._wiki
    assert wiki is not None
    page = wiki._storage.get_wiki_page_by_slug(slug)
    assert page is not None, f"wiki page '{slug}' not found after insert"
    return page.get("branch")


class TestWikiAddBranchHint:
    """Four-case matrix for branch resolution in wiki_add."""

    def test_branch_explicit_used(self, monkeypatch, _unit_backend_harness):
        """Explicit branch= is stored as-is regardless of _detect_branch.

        R3: wiki_add enqueues; drainer persists to wiki store.
        """
        drainer = _unit_backend_harness
        result = server.wiki_add(
            title="Explicit Branch Page",
            content="content for explicit branch test",
            category="reference",
            directory=_DIR,
            branch="feat/foo",
        )
        assert "slug" in result, f"wiki_add failed: {result}"
        drainer.drain_now()
        assert _get_page_branch(result["slug"]) == "feat/foo"

    def test_branch_hint_used_when_branch_omitted(self, monkeypatch, _unit_backend_harness):
        """branch_hint is used when branch is None/omitted.

        R3: _detect_branch is not called (no fallback path in wiki_add).
        """
        drainer = _unit_backend_harness
        result = server.wiki_add(
            title="Branch Hint Page",
            content="content for branch_hint test",
            category="reference",
            directory=_DIR,
            branch=None,
            branch_hint="master",
        )
        assert "slug" in result, f"wiki_add failed: {result}"
        drainer.drain_now()
        assert _get_page_branch(result["slug"]) == "master"

    def test_no_branch_no_hint_stores_null(self, monkeypatch, _unit_backend_harness):
        """Both branch and branch_hint omitted → stored with branch IS NULL (canonical).

        R3: branch enforcement must be OFF (YADGAR_BRANCH_ENFORCEMENT=false) for
        this to proceed without rejection. When enforcement is ON (default), the
        missing-branch error fires. This test explicitly disables enforcement to
        exercise the null-branch storage path.
        """
        drainer = _unit_backend_harness
        monkeypatch.setenv("YADGAR_BRANCH_ENFORCEMENT", "false")
        # Remove YADGAR_CI_BRANCH so no env-var fallback provides a branch.
        monkeypatch.delenv("YADGAR_CI_BRANCH", raising=False)
        result = server.wiki_add(
            title="Canonical Null Branch Page",
            content="content for null-branch test",
            category="reference",
            directory=_DIR,
            branch=None,
            branch_hint=None,
        )
        assert "slug" in result, f"wiki_add failed or rejected unexpectedly: {result}"
        drainer.drain_now()
        stored_branch = _get_page_branch(result["slug"])
        assert stored_branch is None, (
            f"expected branch IS NULL (canonical slot), got {stored_branch!r}"
        )

        # Also verify slug resolution reaches this page
        wiki = server._wiki
        assert wiki is not None
        page = wiki.read_by_directory(result["slug"], _DIR)
        assert page is not None, "slug resolution must reach the stored page"

    def test_explicit_branch_wins_over_hint(self, monkeypatch, _unit_backend_harness):
        """Explicit branch='A' wins over branch_hint='B'."""
        drainer = _unit_backend_harness
        result = server.wiki_add(
            title="Branch Over Hint Page",
            content="content for branch-wins-over-hint test",
            category="reference",
            directory=_DIR,
            branch="A",
            branch_hint="B",
        )
        assert "slug" in result, f"wiki_add failed: {result}"
        drainer.drain_now()
        assert _get_page_branch(result["slug"]) == "A"
