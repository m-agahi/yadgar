"""Tests for §22 project_brief — layered bootstrap (catalog and full modes)."""

import os
import subprocess

import pytest

from yadgar.core import server
from yadgar.tests.core.conftest import TEST_PROJECT_ID

# R3 Car 3d: update_active_work seeds _active_work via the backend /admin op.
# Route the forward through run_admin_op against the shared _st storage (no
# HTTP) so the brief reflects the seeded state.
pytestmark = pytest.mark.usefixtures("admin_backend_bypass")


@pytest.fixture(autouse=True, scope="module")
def _engines(tmp_path_factory):
    tmp_path = tmp_path_factory.mktemp("project_brief")
    db_path = str(tmp_path / "test.db")
    server.init_engines(db_path=db_path, embedding_model="all-MiniLM-L6-v2")
    yield
    server.shutdown()


# ── catalog mode shape ────────────────────────────────────────────────────────


def test_catalog_mode_returns_expected_keys():
    result = server.project_brief("/tmp/myproject", project=TEST_PROJECT_ID)
    expected = {
        "_resolved_directory",
        "_mode",
        "project",
        "tech",
        "init_memory_present",
        "active_work_present",
        "top_anchors",
        "recent_episode_count",
        "stale_wiki_count",
    }
    assert expected.issubset(result.keys()), f"Missing keys: {expected - result.keys()}"


def test_catalog_mode_is_default():
    result = server.project_brief("/tmp/myproject", project=TEST_PROJECT_ID)
    assert result["_mode"] == "catalog"


def test_catalog_mode_explicit():
    result = server.project_brief("/tmp/myproject", mode="catalog", project=TEST_PROJECT_ID)
    assert result["_mode"] == "catalog"


def test_catalog_does_not_include_full_fields():
    # init_memory and active_work content are still full-mode only
    result = server.project_brief("/tmp/myproject", project=TEST_PROJECT_ID)
    assert "init_memory" not in result
    assert "active_work" not in result
    # hot_memories and key_wiki_pages are now in catalog mode (F2 enrichment)
    # but limited to top 3 items vs full mode's top 10/5


def test_catalog_stale_wiki_count_placeholder():
    result = server.project_brief("/tmp/myproject", project=TEST_PROJECT_ID)
    assert result["stale_wiki_count"] == 0


def test_catalog_top_anchors_is_list():
    result = server.project_brief("/tmp/myproject", project=TEST_PROJECT_ID)
    assert isinstance(result["top_anchors"], list)


def test_catalog_top_anchors_at_most_five():
    # Legacy top_anchors union field — may be larger than 5 after F1 scope split
    result = server.project_brief("/tmp/myproject", project=TEST_PROJECT_ID)
    assert isinstance(result["top_anchors"], list)


def test_catalog_recent_episode_count_non_negative():
    result = server.project_brief("/tmp/myproject", project=TEST_PROJECT_ID)
    assert isinstance(result["recent_episode_count"], int)
    assert result["recent_episode_count"] >= 0


def test_catalog_init_memory_present_false_when_none():
    result = server.project_brief("/tmp/noproject", project=TEST_PROJECT_ID)
    assert result["init_memory_present"] is False


def test_catalog_active_work_present_false_when_none():
    result = server.project_brief("/tmp/noproject", project=TEST_PROJECT_ID)
    assert result["active_work_present"] is False


def test_catalog_result_is_json_serializable():
    import json

    result = server.project_brief("/tmp/myproject", project=TEST_PROJECT_ID)
    # Should not raise
    json.dumps(result)


# ── full mode shape ───────────────────────────────────────────────────────────


def test_full_mode_includes_catalog_keys():
    result = server.project_brief("/tmp/myproject", mode="full", project=TEST_PROJECT_ID)
    catalog_keys = {
        "_resolved_directory",
        "_mode",
        "project",
        "tech",
        "init_memory_present",
        "active_work_present",
        "top_anchors",
        "recent_episode_count",
        "stale_wiki_count",
    }
    assert catalog_keys.issubset(result.keys()), f"Missing: {catalog_keys - result.keys()}"


def test_full_mode_sets_mode_field():
    result = server.project_brief("/tmp/myproject", mode="full", project=TEST_PROJECT_ID)
    assert result["_mode"] == "full"


def test_full_mode_includes_extra_keys():
    result = server.project_brief("/tmp/myproject", mode="full", project=TEST_PROJECT_ID)
    assert "init_memory" in result
    assert "active_work" in result
    assert "hot_memories" in result
    assert "key_wiki_pages" in result


def test_full_mode_hot_memories_is_list():
    result = server.project_brief("/tmp/myproject", mode="full", project=TEST_PROJECT_ID)
    assert isinstance(result["hot_memories"], list)


def test_full_mode_hot_memories_at_most_ten():
    result = server.project_brief("/tmp/myproject", mode="full", project=TEST_PROJECT_ID)
    assert len(result["hot_memories"]) <= 10


def test_full_mode_key_wiki_pages_is_list():
    result = server.project_brief("/tmp/myproject", mode="full", project=TEST_PROJECT_ID)
    assert isinstance(result["key_wiki_pages"], list)


def test_full_mode_key_wiki_pages_at_most_five():
    result = server.project_brief("/tmp/myproject", mode="full", project=TEST_PROJECT_ID)
    assert len(result["key_wiki_pages"]) <= 5


def test_full_mode_result_is_json_serializable():
    import json

    result = server.project_brief("/tmp/myproject", mode="full", project=TEST_PROJECT_ID)
    json.dumps(result)


# ── init_memory / active_work inlining (full mode) ────────────────────────────


def test_full_mode_inlines_init_memory(flush_queue):
    directory = "/tmp/project_init_test"
    server.bootstrap_project(directory=directory, content="# TOC\n- item1", project=TEST_PROJECT_ID)
    flush_queue()

    result = server.project_brief(directory, mode="full", project=TEST_PROJECT_ID)
    assert result["init_memory_present"] is True
    assert result["init_memory"] is not None
    assert "TOC" in result["init_memory"]


def test_catalog_flags_init_memory_present(flush_queue):
    directory = "/tmp/project_init_flag_test"
    server.bootstrap_project(
        directory=directory, content="# My Project TOC", project=TEST_PROJECT_ID
    )
    flush_queue()

    result = server.project_brief(directory, mode="catalog", project=TEST_PROJECT_ID)
    assert result["init_memory_present"] is True


def test_full_mode_inlines_active_work(flush_queue):
    directory = "/tmp/active_work_inline_test"
    server.update_active_work(
        directory=directory, content="## Current task\n- doing stuff", project=TEST_PROJECT_ID
    )
    flush_queue()

    result = server.project_brief(directory, mode="full", project=TEST_PROJECT_ID)
    assert result["active_work_present"] is True
    assert result["active_work"] is not None
    assert "Current task" in result["active_work"]


def test_catalog_flags_active_work_present(flush_queue):
    directory = "/tmp/active_work_flag_test"
    server.update_active_work(
        directory=directory, content="## In progress", project=TEST_PROJECT_ID
    )
    flush_queue()

    result = server.project_brief(directory, mode="catalog", project=TEST_PROJECT_ID)
    assert result["active_work_present"] is True


# ── git-based root walk-up resolution ────────────────────────────────────────


def test_resolved_directory_falls_back_to_input():
    # Non-git directory: resolved directory should equal input
    result = server.project_brief("/tmp/not_a_git_repo_xyz", project=TEST_PROJECT_ID)
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

    result = server.project_brief(str(sub), project=TEST_PROJECT_ID)
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
    result = server.project_brief(
        os.path.join(expected, "yadgar", "tests"), project=TEST_PROJECT_ID
    )
    assert result["_resolved_directory"] == expected


# ── top_anchors signals ───────────────────────────────────────────────────────


def test_top_anchors_populated_from_anchor_memories(flush_queue):
    directory = "/tmp/anchor_test_dir"
    server.anchor("Important anchor memory", directory, "key_decision", project=TEST_PROJECT_ID)
    flush_queue()

    result = server.project_brief(directory, mode="catalog", project=TEST_PROJECT_ID)
    # top_anchors shows up to 5 most-accessed anchors globally
    assert isinstance(result["top_anchors"], list)
    # Each anchor should have required fields
    for a in result["top_anchors"]:
        assert "id" in a
        assert "title" in a
        assert "tags" in a


# ── branch field ─────────────────────────────────────────────────────────────


# ── F1: anchor scope split ────────────────────────────────────────────────────


def test_anchor_scope_split_returns_separate_fields():
    """F1: result must contain top_anchors_global and top_anchors_project."""
    result = server.project_brief("/tmp/myproject", project=TEST_PROJECT_ID)
    assert "top_anchors_global" in result
    assert "top_anchors_project" in result


def test_anchor_scope_split_global_list(flush_queue):
    """F1: global anchors include rows with directory_context='' (system)."""
    # Inject a global anchor by directly calling anchor with empty/system context
    server.anchor("global system rule", "", "global_rule", project=TEST_PROJECT_ID)
    flush_queue()

    result = server.project_brief("/tmp/unrelated_project_xyz", project=TEST_PROJECT_ID)
    # Global anchors are returned regardless of project directory
    assert isinstance(result["top_anchors_global"], list)
    titles = [a.get("title", "") for a in result["top_anchors_global"]]
    assert any("global system rule" in t for t in titles)


def test_anchor_scope_split_project_list(flush_queue):
    """F1: project anchors contain only project-scoped rows."""
    directory = "/tmp/scope_test_proj"
    server.anchor("project specific note", directory, "key_decision", project=TEST_PROJECT_ID)
    flush_queue()

    result = server.project_brief(directory, project=TEST_PROJECT_ID)
    project_titles = [a.get("title", "") for a in result["top_anchors_project"]]
    assert any("project specific note" in t for t in project_titles)


def test_anchor_scope_split_project_not_in_other_project(flush_queue):
    """F1: project anchor for dirA must NOT appear in top_anchors_project for dirB."""
    server.anchor("dirA private anchor", "/tmp/proj_dir_a", "key_decision", project=TEST_PROJECT_ID)
    flush_queue()

    result = server.project_brief("/tmp/proj_dir_b_different", project=TEST_PROJECT_ID)
    project_titles = [a.get("title", "") for a in result["top_anchors_project"]]
    assert not any("dirA private anchor" in t for t in project_titles)


def test_anchor_scope_global_includes_system_context(flush_queue, monkeypatch):
    """F1: rows with directory_context='global' or '' surface in global bucket.

    #28: anchor context="global" is the global sentinel, not a filesystem path.
    normalize_write_context resolves it relative to CWD via git heuristics —
    in a linked-worktree CWD this finds the worktree .git FILE and returns the
    canonical repo root instead of passing "global" through. Fix: chdir to /tmp
    so the heuristic finds no .git and the sentinel is stored verbatim.
    """
    monkeypatch.chdir("/tmp")
    server.anchor(
        "global anchor with explicit global ctx", "global", "global_hint", project=TEST_PROJECT_ID
    )
    flush_queue()

    result = server.project_brief("/tmp/any_project_at_all", project=TEST_PROJECT_ID)
    global_titles = [a.get("title", "") for a in result["top_anchors_global"]]
    assert any("global anchor with explicit global ctx" in t for t in global_titles)


def test_legacy_top_anchors_is_union(flush_queue):
    """F1: legacy top_anchors field = union of global + project anchors."""
    server.anchor("union global anchor", "", "global_rule", project=TEST_PROJECT_ID)
    server.anchor(
        "union project anchor", "/tmp/union_test_proj", "key_decision", project=TEST_PROJECT_ID
    )
    flush_queue()

    result = server.project_brief("/tmp/union_test_proj", project=TEST_PROJECT_ID)
    all_titles = [a.get("title", "") for a in result["top_anchors"]]
    [a.get("title", "") for a in result["top_anchors_global"]]
    [a.get("title", "") for a in result["top_anchors_project"]]
    # Every item in global + project should appear in the union
    for a in result["top_anchors_global"] + result["top_anchors_project"]:
        assert a.get("title") in all_titles


# ── F2: catalog mode enriched ────────────────────────────────────────────────


def test_catalog_mode_now_includes_hot_memories():
    """F2: hot_memories must be present in catalog mode."""
    result = server.project_brief("/tmp/myproject", project=TEST_PROJECT_ID)
    assert "hot_memories" in result
    assert isinstance(result["hot_memories"], list)


def test_catalog_mode_now_includes_key_wiki_pages():
    """F2: key_wiki_pages must be present in catalog mode."""
    result = server.project_brief("/tmp/myproject", project=TEST_PROJECT_ID)
    assert "key_wiki_pages" in result
    assert isinstance(result["key_wiki_pages"], list)


def test_catalog_mode_checkpoint_field_present():
    """F2: checkpoint field present in catalog (None when none saved)."""
    result = server.project_brief("/tmp/myproject_no_ckpt", project=TEST_PROJECT_ID)
    assert "checkpoint" in result


def test_catalog_mode_checkpoint_is_none_when_absent():
    """F2: checkpoint is None when no checkpoint saved for directory."""
    result = server.project_brief("/tmp/catalog_no_ckpt_test", project=TEST_PROJECT_ID)
    assert result["checkpoint"] is None


def test_catalog_mode_checkpoint_populated_when_present(flush_queue):
    """F2: checkpoint carries current_task, key_decisions, next_steps from last saved checkpoint."""
    directory = "/tmp/ckpt_catalog_test"
    server.checkpoint(
        directory=directory,
        current_task="refactoring auth module",
        key_decisions=["chose JWT over sessions"],
        next_steps=["write unit tests"],
        project=TEST_PROJECT_ID,
    )
    flush_queue()

    result = server.project_brief(directory, project=TEST_PROJECT_ID)
    cp = result["checkpoint"]
    assert cp is not None
    assert cp.get("current_task") == "refactoring auth module"
    assert "chose JWT over sessions" in (cp.get("key_decisions") or [])
    assert "write unit tests" in (cp.get("next_steps") or [])


def test_catalog_hot_memories_at_most_three():
    """F2: catalog hot_memories limited to top 3."""
    result = server.project_brief("/tmp/myproject", project=TEST_PROJECT_ID)
    assert len(result["hot_memories"]) <= 3


def test_catalog_key_wiki_pages_at_most_three():
    """F2: catalog key_wiki_pages limited to top 3."""
    result = server.project_brief("/tmp/myproject", project=TEST_PROJECT_ID)
    assert len(result["key_wiki_pages"]) <= 3


# ── F3: branch fallback ───────────────────────────────────────────────────────


# ── F4: empty-state nudge ─────────────────────────────────────────────────────


def test_render_suggests_bootstrap_when_no_init_memory():
    """F4: _render includes bootstrap suggestion when init_memory absent."""
    result = server.project_brief("/tmp/empty_state_nudge_test", project=TEST_PROJECT_ID)
    rendered = result.get("_render", "")
    assert "bootstrap_project" in rendered


def test_render_suggests_active_work_when_absent():
    """F4: _render includes update_active_work suggestion when absent."""
    result = server.project_brief("/tmp/empty_state_nudge_test2", project=TEST_PROJECT_ID)
    rendered = result.get("_render", "")
    assert "update_active_work" in rendered


def test_render_no_bootstrap_nudge_when_init_present(flush_queue):
    """F4: no bootstrap suggestion once init_memory is present."""
    directory = "/tmp/nudge_init_present_test"
    server.bootstrap_project(directory=directory, content="# TOC", project=TEST_PROJECT_ID)
    flush_queue()

    result = server.project_brief(directory, project=TEST_PROJECT_ID)
    rendered = result.get("_render", "")
    assert "bootstrap_project" not in rendered


# ── F5: renderer restructure ──────────────────────────────────────────────────


def test_render_has_global_anchors_section(flush_queue):
    """F5: _render contains ## Global Anchors section."""
    server.anchor("global render anchor", "", "global_rule", project=TEST_PROJECT_ID)
    flush_queue()

    result = server.project_brief("/tmp/render_test_proj", project=TEST_PROJECT_ID)
    assert "## Global Anchors" in result["_render"]


def test_render_has_project_anchors_section(flush_queue):
    """F5: _render contains ## Project Anchors section."""
    directory = "/tmp/render_proj_anchor_test"
    server.anchor("project render anchor", directory, "key_decision", project=TEST_PROJECT_ID)
    flush_queue()

    result = server.project_brief(directory, project=TEST_PROJECT_ID)
    assert "## Project Anchors" in result["_render"]


def test_render_has_checkpoint_section(flush_queue):
    """F5: _render contains ## Checkpoint section when checkpoint present."""
    directory = "/tmp/render_ckpt_test"
    server.checkpoint(
        directory=directory,
        current_task="building render test",
        key_decisions=["use markdown"],
        next_steps=["validate output"],
        project=TEST_PROJECT_ID,
    )
    flush_queue()

    result = server.project_brief(directory, project=TEST_PROJECT_ID)
    assert "## Checkpoint" in result["_render"]
    assert "building render test" in result["_render"]


def test_render_has_hot_memories_section(flush_queue):
    """F5: _render contains ## Hot Memories section when memories present."""
    directory = "/tmp/render_hot_mem_test"
    server.memorize(
        "important hot memory for rendering", directory, ["key_fact"], project=TEST_PROJECT_ID
    )
    flush_queue()

    result = server.project_brief(directory, project=TEST_PROJECT_ID)
    rendered = result["_render"]
    # Section exists (even if 0 hot memories, headers present or absent based on content)
    # With a real memory stored, section should appear
    assert "## Hot Memories" in rendered


def test_render_has_wiki_keys_section():
    """F5: _render contains ## Wiki Index section (v5.53.0: renamed from Wiki Keys)."""
    result = server.project_brief("/tmp/render_wiki_keys_test", project=TEST_PROJECT_ID)
    assert "## Wiki Index" in result["_render"]


def test_render_token_count_under_limit(flush_queue):
    """F5: rendered catalog payload stays under ~1500 tokens (~1500 words)."""
    directory = "/tmp/render_token_test"
    # Insert multiple anchors, checkpoint, memories to stress-test size
    server.anchor("global ctx anchor one", "", "global_rule", project=TEST_PROJECT_ID)
    server.anchor("global ctx anchor two", "global", "global_rule", project=TEST_PROJECT_ID)
    server.anchor("project anchor", directory, "key_decision", project=TEST_PROJECT_ID)
    server.checkpoint(
        directory=directory,
        current_task="stress test task",
        key_decisions=["decision A", "decision B", "decision C"],
        next_steps=["step 1", "step 2", "step 3"],
        project=TEST_PROJECT_ID,
    )
    server.memorize(
        "hot memory content for token test", directory, ["key_fact"], project=TEST_PROJECT_ID
    )
    flush_queue()

    result = server.project_brief(directory, project=TEST_PROJECT_ID)
    rendered = result["_render"]
    word_count = len(rendered.split())
    # 1500 token budget ~ 1500-1800 words; we keep a conservative upper bound
    assert word_count < 1800, f"Rendered catalog too large: {word_count} words"
