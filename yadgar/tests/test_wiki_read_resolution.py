"""Tests for §25 wiki_read() slug resolution order.

Resolution order:
1. Exact slug match on current branch
2. Exact slug match on default branch
3. Exact slug match with branch IS NONE (legacy/canonical)
4. Not found → return None (server returns error dict)

Covers:
- Returns current-branch page first
- Falls back to default-branch page when no current-branch version
- Falls back to NONE-branch page when no current or default version
- Returns error dict (not found) for missing slug
"""

import pytest

from yadgar import server

pytestmark = pytest.mark.xdist_group("server_globals")


@pytest.fixture(autouse=True)
def _engines(tmp_path):
    db_path = str(tmp_path / "test.db")
    server.init_engines(db_path=db_path, embedding_model="all-MiniLM-L6-v2")
    yield
    server.shutdown()


def _insert_wiki_page(storage, slug: str, content: str, branch: str | None) -> int:
    """Insert a wiki page with given slug and branch directly into storage."""
    now = storage._now_iso()
    pid = storage._next_id("wiki_page")
    storage._q(
        "CREATE type::record('wiki_page', $id) SET "
        "slug = $slug, title = $title, content = $content, "
        "tags = $tags, links = $links, confidence = $conf, "
        "source_memory_ids = [], "
        "created_at = $ts, updated_at = $ts",
        {
            "id": pid,
            "slug": slug,
            "title": slug,
            "content": content,
            "tags": [],
            "links": [],
            "conf": "medium",
            "ts": now,
        },
    )
    if branch is not None:
        storage._q(f"UPDATE wiki_page:{pid} SET branch = $branch", {"branch": branch})
    return pid


# ── Resolution order tests ────────────────────────────────────────────────────


class TestWikiReadResolutionOrder:
    """wiki_read() follows current → default → NONE resolution order."""

    def test_returns_current_branch_page_when_exists(self, monkeypatch):
        """When current-branch version exists, it is returned."""
        monkeypatch.setattr("yadgar.server._detect_branch", lambda _d: "feat/wiki-res")
        monkeypatch.setattr("yadgar.server._get_default_branch", lambda _d: "master")
        storage = server._wiki._storage

        _insert_wiki_page(storage, "resolution-slug", "content from master", "master")
        _insert_wiki_page(storage, "resolution-slug", "content from feat/wiki-res", "feat/wiki-res")
        _insert_wiki_page(storage, "resolution-slug", "content from none", None)

        result = server.wiki_read("resolution-slug")
        assert "error" not in result, f"wiki_read returned error: {result}"
        assert result.get("content") == "content from feat/wiki-res", (
            f"Expected current-branch page, got: {result.get('content')!r}"
        )

    def test_falls_back_to_default_branch_when_no_current(self, monkeypatch):
        """When no current-branch version, default-branch is returned."""
        monkeypatch.setattr("yadgar.server._detect_branch", lambda _d: "feat/absent-branch")
        monkeypatch.setattr("yadgar.server._get_default_branch", lambda _d: "master")
        storage = server._wiki._storage

        _insert_wiki_page(storage, "fallback-slug", "content from master", "master")
        _insert_wiki_page(storage, "fallback-slug", "content from none", None)

        result = server.wiki_read("fallback-slug")
        assert "error" not in result, f"wiki_read returned error: {result}"
        assert result.get("content") == "content from master", (
            f"Expected default-branch page, got: {result.get('content')!r}"
        )

    def test_falls_back_to_none_branch_when_no_current_or_default(self, monkeypatch):
        """When no current or default version, NONE-branch (legacy) is returned."""
        monkeypatch.setattr("yadgar.server._detect_branch", lambda _d: "feat/absent")
        monkeypatch.setattr("yadgar.server._get_default_branch", lambda _d: "main")
        storage = server._wiki._storage

        # Only NONE-branch version exists
        _insert_wiki_page(storage, "legacy-slug", "content from legacy none-branch", None)

        result = server.wiki_read("legacy-slug")
        assert "error" not in result, f"wiki_read returned error: {result}"
        assert result.get("content") == "content from legacy none-branch", (
            f"Expected NONE-branch legacy page, got: {result.get('content')!r}"
        )

    def test_returns_error_when_slug_not_found(self, monkeypatch):
        """Missing slug returns error dict (current behavior)."""
        monkeypatch.setattr("yadgar.server._detect_branch", lambda _d: "feat/any")
        monkeypatch.setattr("yadgar.server._get_default_branch", lambda _d: "master")

        result = server.wiki_read("completely-nonexistent-slug-xyz-abc-123")
        assert "error" in result, f"Expected error for missing slug, got: {result}"

    def test_resolution_with_no_git_context(self, monkeypatch):
        """Non-git context: falls back through default → NONE."""
        monkeypatch.setattr("yadgar.server._detect_branch", lambda _d: None)
        monkeypatch.setattr("yadgar.server._get_default_branch", lambda _d: "master")
        storage = server._wiki._storage

        _insert_wiki_page(storage, "no-git-slug", "content from master", "master")
        _insert_wiki_page(storage, "no-git-slug", "content from none", None)

        result = server.wiki_read("no-git-slug")
        assert "error" not in result, f"wiki_read returned error: {result}"
        # Should return master (default) page when current=None
        assert result.get("content") == "content from master", (
            f"Expected default-branch page for non-git context, got: {result.get('content')!r}"
        )

    def test_current_branch_page_preferred_over_none_branch(self, monkeypatch):
        """Current-branch beats NONE-branch."""
        monkeypatch.setattr("yadgar.server._detect_branch", lambda _d: "feat/preferred")
        monkeypatch.setattr("yadgar.server._get_default_branch", lambda _d: "master")
        storage = server._wiki._storage

        _insert_wiki_page(storage, "prefer-slug", "content from none legacy", None)
        _insert_wiki_page(storage, "prefer-slug", "content from feat/preferred", "feat/preferred")

        result = server.wiki_read("prefer-slug")
        assert "error" not in result
        assert result.get("content") == "content from feat/preferred", (
            f"Current-branch should beat NONE-branch: got {result.get('content')!r}"
        )
