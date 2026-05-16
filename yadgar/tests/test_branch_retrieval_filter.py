"""Tests for §25 branch-based retrieval filter on recall() and wiki_query().

Covers:
- recall filters to branch IN (current, default, NONE)
- Memory on a different branch is excluded from recall
- Memory on current branch gets 1.5x score boost vs default-branch result
- Non-git directory: no boost, no current filter (degenerates to IN (default, NONE))
- wiki_query: same filter + boost behavior
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


def _insert_memory_with_branch(
    storage, content: str, branch: str | None, directory: str = "/tmp/test-dir"
) -> int:
    """Insert a memory row directly with given branch value."""
    mid = storage.insert_memory(
        {
            "content": content,
            "directory_context": directory,
            "tags": ["test"],
            "heat": 1.0,
        },
        branch=branch,
    )
    return mid


def _insert_wiki_with_branch(storage_wiki, title: str, content: str, branch: str | None) -> str:
    """Insert a wiki page with given branch value."""
    import re

    slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:64]
    storage_wiki._storage.insert_wiki_page(
        {
            "slug": slug,
            "title": title,
            "content": content,
            "category": "reference",
            "tags": ["test"],
            "links": [],
            "source_memory_ids": [],
            "confidence": "medium",
        },
        branch=branch,
    )
    return slug


# ── recall branch filter ──────────────────────────────────────────────────────


class TestRecallBranchFilter:
    """recall() respects branch IN (current, default, NONE) filter."""

    def test_recall_returns_current_branch_memory(self, monkeypatch):
        """Memory tagged with current branch is returned by recall."""
        monkeypatch.setattr("yadgar.server._detect_branch", lambda _d: "feat/my-feature")
        monkeypatch.setattr("yadgar.server._get_default_branch", lambda _d: "master")
        storage = server._get_storage()
        _insert_memory_with_branch(
            storage, "unique-current-branch-content xyz987", "feat/my-feature"
        )

        results = server.recall("unique-current-branch-content xyz987")
        ids_branches = [(r.get("id"), r.get("branch")) for r in results]
        current_branch_results = [r for r in results if r.get("branch") == "feat/my-feature"]
        assert current_branch_results, (
            f"Expected current-branch memory in results, got: {ids_branches}"
        )

    def test_recall_returns_default_branch_memory(self, monkeypatch):
        """Memory tagged with default branch is returned from any branch."""
        monkeypatch.setattr("yadgar.server._detect_branch", lambda _d: "feat/other")
        monkeypatch.setattr("yadgar.server._get_default_branch", lambda _d: "master")
        storage = server._get_storage()
        _insert_memory_with_branch(storage, "unique-default-branch-content abc123", "master")

        results = server.recall("unique-default-branch-content abc123")
        master_results = [r for r in results if r.get("branch") == "master"]
        assert master_results, "Expected default-branch memory in results"

    def test_recall_returns_none_branch_memory(self, monkeypatch):
        """Memory with branch=NONE (legacy) is always returned."""
        monkeypatch.setattr("yadgar.server._detect_branch", lambda _d: "feat/other")
        monkeypatch.setattr("yadgar.server._get_default_branch", lambda _d: "master")
        storage = server._get_storage()
        _insert_memory_with_branch(storage, "unique-no-branch-content leg456", None)

        results = server.recall("unique-no-branch-content leg456")
        none_branch_results = [r for r in results if r.get("branch") is None]
        assert none_branch_results, "Expected NONE-branch (legacy) memory in results"

    def test_recall_excludes_other_branch_memory(self, monkeypatch):
        """Memory on a different feature branch is excluded."""
        monkeypatch.setattr("yadgar.server._detect_branch", lambda _d: "feat/current")
        monkeypatch.setattr("yadgar.server._get_default_branch", lambda _d: "master")
        storage = server._get_storage()
        mid = _insert_memory_with_branch(
            storage, "unique-other-branch-content zzz999", "feat/completely-different"
        )

        results = server.recall("unique-other-branch-content zzz999")
        result_ids = [r.get("id") for r in results]
        assert mid not in result_ids, (
            f"Memory on different branch should be excluded, but id {mid} appeared in results"
        )

    def test_recall_current_branch_gets_boost(self, monkeypatch):
        """Current-branch memory scores 1.5x relative to default-branch memory."""
        monkeypatch.setattr("yadgar.server._detect_branch", lambda _d: "feat/boosted")
        monkeypatch.setattr("yadgar.server._get_default_branch", lambda _d: "master")
        storage = server._get_storage()

        # Insert two very similar memories, one on current, one on default
        _insert_memory_with_branch(
            storage, "boost-test identical query content boosted-branch", "feat/boosted"
        )
        _insert_memory_with_branch(
            storage, "boost-test identical query content default-branch", "master"
        )

        results = server.recall("boost-test identical query content", max_results=10)
        current_results = [r for r in results if r.get("branch") == "feat/boosted"]
        default_results = [r for r in results if r.get("branch") == "master"]

        # Both should be present
        assert current_results, "Current-branch result missing"
        assert default_results, "Default-branch result missing"

        # Current-branch result should rank higher (appears earlier)
        current_rank = next(i for i, r in enumerate(results) if r.get("branch") == "feat/boosted")
        default_rank = next(i for i, r in enumerate(results) if r.get("branch") == "master")
        assert current_rank < default_rank, (
            f"Current-branch (rank {current_rank}) should outrank default-branch (rank {default_rank})"
        )

    def test_recall_non_git_no_current_filter(self, monkeypatch):
        """Non-git directory: current=None, filter degenerates to IN (default, NONE)."""
        monkeypatch.setattr("yadgar.server._detect_branch", lambda _d: None)
        monkeypatch.setattr("yadgar.server._get_default_branch", lambda _d: "master")
        storage = server._get_storage()

        mid_master = _insert_memory_with_branch(
            storage, "non-git-recall master content qqq", "master"
        )
        mid_none = _insert_memory_with_branch(storage, "non-git-recall none content rrr", None)
        mid_feat = _insert_memory_with_branch(
            storage, "non-git-recall feat content sss", "feat/something"
        )

        result_ids = {r.get("id") for r in server.recall("non-git-recall", max_results=10)}
        assert mid_master in result_ids, (
            "Master-branch memory should be included in non-git context"
        )
        assert mid_none in result_ids, "NONE-branch memory should be included in non-git context"
        assert mid_feat not in result_ids, (
            "Feature-branch memory should be excluded when current=None"
        )


# ── wiki_query branch filter ──────────────────────────────────────────────────


class TestWikiQueryBranchFilter:
    """wiki_query() respects branch filter + 1.5x boost on current branch."""

    def test_wiki_query_returns_current_branch_page(self, monkeypatch):
        """Wiki page on current branch is returned."""
        monkeypatch.setattr("yadgar.server._detect_branch", lambda _d: "feat/wiki-filter")
        monkeypatch.setattr("yadgar.server._get_default_branch", lambda _d: "master")
        wiki = server._wiki
        assert wiki is not None
        _insert_wiki_with_branch(
            wiki,
            "Wiki Current Branch Test Unique Bbb",
            "unique wiki current content bbb999",
            "feat/wiki-filter",
        )
        results = server.wiki_query("unique wiki current content bbb999")
        current_results = [r for r in results if r.get("branch") == "feat/wiki-filter"]
        assert current_results, "Expected current-branch wiki page in results"

    def test_wiki_query_excludes_other_branch_page(self, monkeypatch):
        """Wiki page on unrelated branch is excluded."""
        monkeypatch.setattr("yadgar.server._detect_branch", lambda _d: "feat/current-wiki")
        monkeypatch.setattr("yadgar.server._get_default_branch", lambda _d: "master")
        wiki = server._wiki
        slug = _insert_wiki_with_branch(
            wiki,
            "Wiki Other Branch Unique Ccc",
            "unique wiki other content ccc111",
            "feat/other-wiki",
        )
        page = wiki._storage.get_wiki_page_by_slug(slug)
        page_id = page["id"] if page else None

        results = server.wiki_query("unique wiki other content ccc111")
        result_ids = [r.get("id") for r in results]
        assert page_id not in result_ids, (
            f"Wiki page on different branch should be excluded, but {page_id} appeared"
        )

    def test_wiki_query_current_branch_boost(self, monkeypatch):
        """Current-branch wiki page ranks above default-branch page for same query."""
        monkeypatch.setattr("yadgar.server._detect_branch", lambda _d: "feat/wiki-boost")
        monkeypatch.setattr("yadgar.server._get_default_branch", lambda _d: "master")
        wiki = server._wiki
        _insert_wiki_with_branch(
            wiki,
            "Wiki Boost Test Unique Ddd Current",
            "wiki boost identical query ddd999 current",
            "feat/wiki-boost",
        )
        _insert_wiki_with_branch(
            wiki,
            "Wiki Boost Test Unique Ddd Default",
            "wiki boost identical query ddd999 default",
            "master",
        )
        results = server.wiki_query("wiki boost identical query ddd999", max_results=10)
        current_results = [r for r in results if r.get("branch") == "feat/wiki-boost"]
        default_results = [r for r in results if r.get("branch") == "master"]
        assert current_results, "Current-branch wiki missing from results"
        assert default_results, "Default-branch wiki missing from results"
        current_rank = next(
            i for i, r in enumerate(results) if r.get("branch") == "feat/wiki-boost"
        )
        default_rank = next(i for i, r in enumerate(results) if r.get("branch") == "master")
        assert current_rank < default_rank, (
            f"Current-branch wiki (rank {current_rank}) should outrank default (rank {default_rank})"
        )

    def test_wiki_query_non_git_no_current_filter(self, monkeypatch):
        """Non-git: no current filter, master + NONE pages returned, feature excluded."""
        monkeypatch.setattr("yadgar.server._detect_branch", lambda _d: None)
        monkeypatch.setattr("yadgar.server._get_default_branch", lambda _d: "master")
        wiki = server._wiki
        slug_master = _insert_wiki_with_branch(
            wiki, "Wiki Non Git Master Eee", "wiki non-git content eee111 master", "master"
        )
        slug_none = _insert_wiki_with_branch(
            wiki, "Wiki Non Git None Eee", "wiki non-git content eee111 none", None
        )
        slug_feat = _insert_wiki_with_branch(
            wiki, "Wiki Non Git Feat Eee", "wiki non-git content eee111 feat", "feat/other"
        )

        results = server.wiki_query("wiki non-git content eee111", max_results=10)

        def _get_slug_id(s):
            p = wiki._storage.get_wiki_page_by_slug(s)
            return p["id"] if p else None

        id_master = _get_slug_id(slug_master)
        id_none = _get_slug_id(slug_none)
        id_feat = _get_slug_id(slug_feat)

        result_ids = {r.get("id") for r in results}
        assert id_master in result_ids, "Master-branch wiki should be included in non-git context"
        assert id_none in result_ids, "NONE-branch wiki should be included in non-git context"
        assert id_feat not in result_ids, "Feature-branch wiki should be excluded when current=None"
