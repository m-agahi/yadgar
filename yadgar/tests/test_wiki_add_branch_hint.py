"""Tests for wiki_add branch_hint arg (v5.4 W1).

Resolution priority:
1. Explicit branch (non-empty) → use as-is.
2. branch_hint (non-empty, branch omitted/None) → use as branch.
3. Both None → store with branch IS NULL (canonical slot).
4. branch AND branch_hint both provided → explicit branch wins.

Old _detect_branch(os.getcwd()) fallback is removed.
"""

from __future__ import annotations

import pytest

from yadgar import server


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


def _wiki_add_sync(monkeypatch, **kwargs) -> dict:
    """Call wiki_add on the sync path (is_draining=True)."""
    monkeypatch.setattr("yadgar.file_queue._drain_local.active", True, raising=False)
    return server.wiki_add(**kwargs)


class TestWikiAddBranchHint:
    """Four-case matrix for branch resolution in wiki_add."""

    def test_branch_explicit_used(self, monkeypatch):
        """Explicit branch= is stored as-is regardless of _detect_branch."""
        monkeypatch.setattr("yadgar.server._detect_branch", lambda _d: "should-not-be-used")
        result = _wiki_add_sync(
            monkeypatch,
            title="Explicit Branch Page",
            content="content for explicit branch test",
            category="reference",
            branch="feat/foo",
        )
        assert "slug" in result, f"wiki_add failed: {result}"
        assert _get_page_branch(result["slug"]) == "feat/foo"

    def test_branch_hint_used_when_branch_omitted(self, monkeypatch):
        """branch_hint is used when branch is None/omitted."""
        # Patch _detect_branch to confirm it is NOT called (no fallback path)
        detect_calls: list[str] = []
        monkeypatch.setattr(
            "yadgar.server._detect_branch",
            lambda _d: detect_calls.append(_d) or "wrong-branch",
        )
        result = _wiki_add_sync(
            monkeypatch,
            title="Branch Hint Page",
            content="content for branch_hint test",
            category="reference",
            branch=None,
            branch_hint="master",
        )
        assert "slug" in result, f"wiki_add failed: {result}"
        assert _get_page_branch(result["slug"]) == "master"
        # _detect_branch must NOT have been called — it is not the fallback path
        assert detect_calls == [], (
            f"_detect_branch was called {len(detect_calls)} time(s); should be 0"
        )

    def test_no_branch_no_hint_stores_null(self, monkeypatch):
        """Both branch and branch_hint omitted → stored with branch IS NULL (canonical)."""
        monkeypatch.setattr("yadgar.server._detect_branch", lambda _d: "should-not-be-used")
        result = _wiki_add_sync(
            monkeypatch,
            title="Canonical Null Branch Page",
            content="content for null-branch test",
            category="reference",
            branch=None,
            branch_hint=None,
        )
        assert "slug" in result, f"wiki_add failed: {result}"
        stored_branch = _get_page_branch(result["slug"])
        assert stored_branch is None, (
            f"expected branch IS NULL (canonical slot), got {stored_branch!r}"
        )

        # Also verify wiki_read step-3 resolution resolves this page
        wiki = server._wiki
        assert wiki is not None
        page = wiki.read_by_branch(result["slug"], current_branch=None, default_branch="master")
        assert page is not None, "wiki_read step-3 must resolve canonical NULL page"

    def test_explicit_branch_wins_over_hint(self, monkeypatch):
        """Explicit branch='A' wins over branch_hint='B'."""
        result = _wiki_add_sync(
            monkeypatch,
            title="Branch Over Hint Page",
            content="content for branch-wins-over-hint test",
            category="reference",
            branch="A",
            branch_hint="B",
        )
        assert "slug" in result, f"wiki_add failed: {result}"
        assert _get_page_branch(result["slug"]) == "A"
