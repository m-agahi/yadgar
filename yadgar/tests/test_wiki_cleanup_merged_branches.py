"""Tests for §26 wiki_cleanup_merged_branches MCP tool.

TDD — written BEFORE the implementation.
Covers:
- dry_run=True (default) lists candidates without deleting
- identifies orphaned-branch pages
- dry_run=False executes deletion
- git failure → early return, no DB deletes
"""

from __future__ import annotations

import subprocess
from unittest.mock import patch

import pytest

from yadgar import server


@pytest.fixture(autouse=True)
def _engines(tmp_path):
    db_path = str(tmp_path / "test.db")
    server.init_engines(db_path=db_path, embedding_model="all-MiniLM-L6-v2")
    yield
    server.shutdown()


# ── dry_run default ───────────────────────────────────────────────────────────


def test_dry_run_default_true(tmp_path, flush_queue):
    """Default dry_run=True returns candidates without deleting."""
    result = server.wiki_cleanup_merged_branches(directory=str(tmp_path))
    assert result["dry_run"] is True


def test_dry_run_returns_expected_keys(tmp_path):
    result = server.wiki_cleanup_merged_branches(directory=str(tmp_path))
    assert "candidates" in result
    assert "deleted_count" in result
    assert "dry_run" in result


def test_dry_run_deleted_count_zero(tmp_path):
    """dry_run=True never deletes, so deleted_count must be 0."""
    result = server.wiki_cleanup_merged_branches(directory=str(tmp_path))
    assert result["deleted_count"] == 0


# ── orphaned-branch identification ───────────────────────────────────────────


def test_no_candidates_when_no_branch_pages(tmp_path, flush_queue, monkeypatch):
    """No candidates when no wiki_page rows have a branch set."""
    # Force _detect_branch to None so wiki_add stores a canonical page (no branch)
    # — otherwise the runner's actual git branch leaks into the wiki entry.
    monkeypatch.setattr("yadgar.server._detect_branch", lambda _d: None)

    server.wiki_add(
        title="Canonical Page",
        content="Content",
        category="reference",
        tags=["arch"],
    )
    flush_queue()

    with patch(
        "subprocess.check_output",
        return_value=b"master\nfeat/other\n",
    ):
        result = server.wiki_cleanup_merged_branches(directory=str(tmp_path))

    assert result["candidates"] == []


def test_identifies_orphaned_branch_page(tmp_path, flush_queue):
    """Pages with branch not in live branches are returned as candidates."""
    storage = server._get_storage()

    # Directly insert a wiki page with a now-merged branch
    storage._q(
        "INSERT INTO wiki_page (slug, title, content, category, branch, tags) VALUES "
        "($slug, $title, $content, $cat, $branch, $tags)",
        {
            "slug": "old-feat-page",
            "title": "Old Feature Page",
            "content": "Stale",
            "cat": "reference",
            "branch": "feat/old-done",
            "tags": [],
        },
    )

    # Live branches do NOT include feat/old-done
    live_branches_output = b"master\nremotes/origin/master\n"
    with patch("subprocess.check_output", return_value=live_branches_output):
        result = server.wiki_cleanup_merged_branches(directory=str(tmp_path))

    slugs = [c["slug"] for c in result["candidates"]]
    assert "old-feat-page" in slugs


def test_live_branch_pages_not_candidates(tmp_path, flush_queue):
    """Pages whose branch is still live must NOT be candidates."""
    storage = server._get_storage()

    storage._q(
        "INSERT INTO wiki_page (slug, title, content, category, branch, tags) VALUES "
        "($slug, $title, $content, $cat, $branch, $tags)",
        {
            "slug": "active-feat-page",
            "title": "Active Feature Page",
            "content": "Still active",
            "cat": "reference",
            "branch": "feat/still-active",
            "tags": [],
        },
    )

    # live branches INCLUDE feat/still-active
    live_branches_output = b"master\nremotes/origin/feat/still-active\n"
    with patch("subprocess.check_output", return_value=live_branches_output):
        result = server.wiki_cleanup_merged_branches(directory=str(tmp_path))

    slugs = [c["slug"] for c in result["candidates"]]
    assert "active-feat-page" not in slugs


def test_master_tagged_pages_never_candidates(tmp_path, flush_queue):
    """Pages with branch='master' are never candidates for cleanup."""
    storage = server._get_storage()

    storage._q(
        "INSERT INTO wiki_page (slug, title, content, category, branch, tags) VALUES "
        "($slug, $title, $content, $cat, $branch, $tags)",
        {
            "slug": "master-page",
            "title": "Master Page",
            "content": "Canonical",
            "cat": "reference",
            "branch": "master",
            "tags": [],
        },
    )

    with patch("subprocess.check_output", return_value=b"main\n"):
        result = server.wiki_cleanup_merged_branches(directory=str(tmp_path))

    slugs = [c["slug"] for c in result["candidates"]]
    assert "master-page" not in slugs


def test_main_tagged_pages_never_candidates(tmp_path, flush_queue):
    """Pages with branch='main' are never candidates for cleanup."""
    storage = server._get_storage()

    storage._q(
        "INSERT INTO wiki_page (slug, title, content, category, branch, tags) VALUES "
        "($slug, $title, $content, $cat, $branch, $tags)",
        {
            "slug": "main-page",
            "title": "Main Page",
            "content": "Canonical",
            "cat": "reference",
            "branch": "main",
            "tags": [],
        },
    )

    with patch("subprocess.check_output", return_value=b"master\n"):
        result = server.wiki_cleanup_merged_branches(directory=str(tmp_path))

    slugs = [c["slug"] for c in result["candidates"]]
    assert "main-page" not in slugs


# ── dry_run=False executes cleanup ────────────────────────────────────────────


def test_dry_run_false_deletes_orphaned_pages(tmp_path, flush_queue):
    """dry_run=False actually deletes the orphaned pages."""
    storage = server._get_storage()

    storage._q(
        "INSERT INTO wiki_page (slug, title, content, category, branch, tags) VALUES "
        "($slug, $title, $content, $cat, $branch, $tags)",
        {
            "slug": "to-delete",
            "title": "Orphaned",
            "content": "Gone",
            "cat": "reference",
            "branch": "feat/merged-long-ago",
            "tags": [],
        },
    )

    live_branches_output = b"master\n"
    with patch("subprocess.check_output", return_value=live_branches_output):
        result = server.wiki_cleanup_merged_branches(directory=str(tmp_path), dry_run=False)

    assert result["dry_run"] is False
    assert result["deleted_count"] >= 1

    # Verify it's actually gone
    rows = storage._q("SELECT id FROM wiki_page WHERE slug = 'to-delete'")
    assert rows == [], "orphaned page should have been deleted"


def test_dry_run_false_returns_correct_count(tmp_path, flush_queue):
    """dry_run=False returns deleted_count matching the candidates count."""
    storage = server._get_storage()

    for i in range(3):
        storage._q(
            "INSERT INTO wiki_page (slug, title, content, category, branch, tags) VALUES "
            "($slug, $title, $content, $cat, $branch, $tags)",
            {
                "slug": f"orphan-{i}",
                "title": f"Orphan {i}",
                "content": "Content",
                "cat": "reference",
                "branch": "feat/old-branch",
                "tags": [],
            },
        )

    with patch("subprocess.check_output", return_value=b"master\n"):
        result = server.wiki_cleanup_merged_branches(directory=str(tmp_path), dry_run=False)

    assert result["deleted_count"] == 3


# ── P0: git failure → early return, no mass-delete ───────────────────────────


def test_git_failure_returns_error_dict(tmp_path, flush_queue):
    """CalledProcessError → error dict returned, no DB deletes."""
    storage = server._get_storage()

    # Insert an orphaned-branch page that WOULD be a candidate
    storage._q(
        "INSERT INTO wiki_page (slug, title, content, category, branch, tags) VALUES "
        "($slug, $title, $content, $cat, $branch, $tags)",
        {
            "slug": "should-not-be-deleted",
            "title": "Protected Page",
            "content": "Still here",
            "cat": "reference",
            "branch": "feat/merged",
            "tags": [],
        },
    )

    with patch(
        "subprocess.check_output",
        side_effect=subprocess.CalledProcessError(1, "git"),
    ):
        result = server.wiki_cleanup_merged_branches(directory=str(tmp_path), dry_run=False)

    assert "error" in result
    assert result["deleted_count"] == 0
    assert result["candidates"] == []

    # Verify page was NOT deleted
    rows = storage._q("SELECT id FROM wiki_page WHERE slug = 'should-not-be-deleted'")
    assert rows != [], "page must not be deleted when git enumeration fails"


def test_git_timeout_returns_error_dict(tmp_path):
    """TimeoutExpired → error dict, cleanup aborted."""
    with patch(
        "subprocess.check_output",
        side_effect=subprocess.TimeoutExpired("git", 5),
    ):
        result = server.wiki_cleanup_merged_branches(directory=str(tmp_path), dry_run=False)

    assert "error" in result
    assert result["deleted_count"] == 0


def test_git_not_found_returns_error_dict(tmp_path):
    """FileNotFoundError (git not on PATH) → error dict, cleanup aborted."""
    with patch(
        "subprocess.check_output",
        side_effect=FileNotFoundError("git not found"),
    ):
        result = server.wiki_cleanup_merged_branches(directory=str(tmp_path), dry_run=False)

    assert "error" in result
    assert result["deleted_count"] == 0
