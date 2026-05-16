"""Tests for §22 project_brief — layered bootstrap (catalog and full modes)."""

import os
import subprocess

import pytest

from yadgar import server

pytestmark = pytest.mark.xdist_group("server_globals")


@pytest.fixture(autouse=True)
def _engines(tmp_path):
    db_path = str(tmp_path / "test.db")
    server.init_engines(db_path=db_path, embedding_model="all-MiniLM-L6-v2")
    yield
    server.shutdown()


# ── catalog mode shape ────────────────────────────────────────────────────────


def test_catalog_mode_returns_expected_keys():
    result = server.project_brief("/tmp/myproject")
    expected = {
        "_resolved_directory",
        "_mode",
        "project",
        "tech",
        "branch",
        "init_memory_present",
        "active_work_present",
        "top_anchors",
        "recent_episode_count",
        "stale_wiki_count",
    }
    assert expected.issubset(result.keys()), f"Missing keys: {expected - result.keys()}"


def test_catalog_mode_is_default():
    result = server.project_brief("/tmp/myproject")
    assert result["_mode"] == "catalog"


def test_catalog_mode_explicit():
    result = server.project_brief("/tmp/myproject", mode="catalog")
    assert result["_mode"] == "catalog"


def test_catalog_does_not_include_full_fields():
    result = server.project_brief("/tmp/myproject")
    assert "init_memory" not in result
    assert "active_work" not in result
    assert "hot_memories" not in result
    assert "key_wiki_pages" not in result


def test_catalog_stale_wiki_count_placeholder():
    result = server.project_brief("/tmp/myproject")
    assert result["stale_wiki_count"] == 0


def test_catalog_top_anchors_is_list():
    result = server.project_brief("/tmp/myproject")
    assert isinstance(result["top_anchors"], list)


def test_catalog_top_anchors_at_most_five():
    result = server.project_brief("/tmp/myproject")
    assert len(result["top_anchors"]) <= 5


def test_catalog_recent_episode_count_non_negative():
    result = server.project_brief("/tmp/myproject")
    assert isinstance(result["recent_episode_count"], int)
    assert result["recent_episode_count"] >= 0


def test_catalog_init_memory_present_false_when_none():
    result = server.project_brief("/tmp/noproject")
    assert result["init_memory_present"] is False


def test_catalog_active_work_present_false_when_none():
    result = server.project_brief("/tmp/noproject")
    assert result["active_work_present"] is False


def test_catalog_result_is_json_serializable():
    import json

    result = server.project_brief("/tmp/myproject")
    # Should not raise
    json.dumps(result)


# ── full mode shape ───────────────────────────────────────────────────────────


def test_full_mode_includes_catalog_keys():
    result = server.project_brief("/tmp/myproject", mode="full")
    catalog_keys = {
        "_resolved_directory",
        "_mode",
        "project",
        "tech",
        "branch",
        "init_memory_present",
        "active_work_present",
        "top_anchors",
        "recent_episode_count",
        "stale_wiki_count",
    }
    assert catalog_keys.issubset(result.keys()), f"Missing: {catalog_keys - result.keys()}"


def test_full_mode_sets_mode_field():
    result = server.project_brief("/tmp/myproject", mode="full")
    assert result["_mode"] == "full"


def test_full_mode_includes_extra_keys():
    result = server.project_brief("/tmp/myproject", mode="full")
    assert "init_memory" in result
    assert "active_work" in result
    assert "hot_memories" in result
    assert "key_wiki_pages" in result


def test_full_mode_hot_memories_is_list():
    result = server.project_brief("/tmp/myproject", mode="full")
    assert isinstance(result["hot_memories"], list)


def test_full_mode_hot_memories_at_most_ten():
    result = server.project_brief("/tmp/myproject", mode="full")
    assert len(result["hot_memories"]) <= 10


def test_full_mode_key_wiki_pages_is_list():
    result = server.project_brief("/tmp/myproject", mode="full")
    assert isinstance(result["key_wiki_pages"], list)


def test_full_mode_key_wiki_pages_at_most_five():
    result = server.project_brief("/tmp/myproject", mode="full")
    assert len(result["key_wiki_pages"]) <= 5


def test_full_mode_result_is_json_serializable():
    import json

    result = server.project_brief("/tmp/myproject", mode="full")
    json.dumps(result)


# ── init_memory / active_work inlining (full mode) ────────────────────────────


def test_full_mode_inlines_init_memory(flush_queue):
    directory = "/tmp/project_init_test"
    server.bootstrap_project(directory=directory, content="# TOC\n- item1")
    flush_queue()

    result = server.project_brief(directory, mode="full")
    assert result["init_memory_present"] is True
    assert result["init_memory"] is not None
    assert "TOC" in result["init_memory"]


def test_catalog_flags_init_memory_present(flush_queue):
    directory = "/tmp/project_init_flag_test"
    server.bootstrap_project(directory=directory, content="# My Project TOC")
    flush_queue()

    result = server.project_brief(directory, mode="catalog")
    assert result["init_memory_present"] is True


def test_full_mode_inlines_active_work(flush_queue):
    directory = "/tmp/active_work_inline_test"
    server.update_active_work(directory=directory, content="## Current task\n- doing stuff")
    flush_queue()

    result = server.project_brief(directory, mode="full")
    assert result["active_work_present"] is True
    assert result["active_work"] is not None
    assert "Current task" in result["active_work"]


def test_catalog_flags_active_work_present(flush_queue):
    directory = "/tmp/active_work_flag_test"
    server.update_active_work(directory=directory, content="## In progress")
    flush_queue()

    result = server.project_brief(directory, mode="catalog")
    assert result["active_work_present"] is True


# ── git-based root walk-up resolution ────────────────────────────────────────


def test_resolved_directory_falls_back_to_input():
    # Non-git directory: resolved directory should equal input
    result = server.project_brief("/tmp/not_a_git_repo_xyz")
    assert result["_resolved_directory"] == "/tmp/not_a_git_repo_xyz"


def test_resolved_directory_uses_git_toplevel(tmp_path):
    # Create a git repo at tmp_path root
    subprocess.run(["git", "init", str(tmp_path)], check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "t@t.com"],
        cwd=str(tmp_path),
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test"],
        cwd=str(tmp_path),
        check=True,
        capture_output=True,
    )

    # Create a sub-directory
    sub = tmp_path / "sub" / "deep"
    sub.mkdir(parents=True)

    result = server.project_brief(str(sub))
    # Should resolve to git root (tmp_path), not the sub-directory
    assert result["_resolved_directory"] == str(tmp_path)


def test_resolved_directory_for_actual_repo():
    import subprocess

    # Resolve the actual git root at runtime so the test is CI-portable.
    expected = (
        subprocess.check_output(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=os.path.dirname(__file__),
        )
        .decode()
        .strip()
    )

    # Use a sub-path inside the repo; server should walk up to the root.
    result = server.project_brief(os.path.join(expected, "yadgar", "tests"))
    assert result["_resolved_directory"] == expected


# ── top_anchors signals ───────────────────────────────────────────────────────


def test_top_anchors_populated_from_anchor_memories(flush_queue):
    directory = "/tmp/anchor_test_dir"
    server.anchor("Important anchor memory", directory, "key_decision")
    flush_queue()

    result = server.project_brief(directory, mode="catalog")
    # top_anchors shows up to 5 most-accessed anchors globally
    assert isinstance(result["top_anchors"], list)
    # Each anchor should have required fields
    for a in result["top_anchors"]:
        assert "id" in a
        assert "title" in a
        assert "tags" in a


# ── branch field ─────────────────────────────────────────────────────────────


def test_catalog_branch_field_is_string_or_none():
    result = server.project_brief("/tmp/myproject")
    assert result["branch"] is None or isinstance(result["branch"], str)
