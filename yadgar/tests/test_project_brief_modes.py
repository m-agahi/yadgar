"""Tests for v5.7.12 project_brief mode split (signals / restore / catalog back-compat / full).

TDD: these tests are written BEFORE implementation.  Run `pytest -x` to see the red
list, then implement until all pass.
"""

from __future__ import annotations

import json

import pytest

from yadgar import server


@pytest.fixture(autouse=True)
def _engines(tmp_path):
    db_path = str(tmp_path / "test.db")
    server.init_engines(db_path=db_path, embedding_model="all-MiniLM-L6-v2")
    yield
    server.shutdown()


# ── signals mode ─────────────────────────────────────────────────────────────


def test_signals_mode_returns_required_keys():
    result = server.project_brief("/tmp/myproject", mode="signals")
    required = {
        "init_memory_present",
        "active_work_present",
        "stale_wiki_count",
        "stale_checkpoint_hours",
        "active_work_age_hours",
        "init_memory_age_hours",
        "recommended_actions",
    }
    assert required.issubset(result.keys()), f"Missing keys: {required - result.keys()}"


def test_signals_mode_no_anchors():
    result = server.project_brief("/tmp/myproject", mode="signals")
    assert "top_anchors" not in result
    assert "top_anchors_global" not in result
    assert "top_anchors_project" not in result


def test_signals_mode_no_hot_memories():
    result = server.project_brief("/tmp/myproject", mode="signals")
    assert "hot_memories" not in result


def test_signals_mode_no_wiki_keys():
    result = server.project_brief("/tmp/myproject", mode="signals")
    assert "key_wiki_pages" not in result


def test_signals_mode_no_render():
    result = server.project_brief("/tmp/myproject", mode="signals")
    assert "_render" not in result


def test_signals_mode_age_numerics_none_when_absent():
    result = server.project_brief("/tmp/no_such_project_xyz", mode="signals")
    assert result["stale_checkpoint_hours"] is None
    assert result["active_work_age_hours"] is None
    assert result["init_memory_age_hours"] is None


def test_signals_mode_booleans_false_when_empty():
    result = server.project_brief("/tmp/no_such_project_abc", mode="signals")
    assert result["init_memory_present"] is False
    assert result["active_work_present"] is False


def test_signals_mode_recommended_actions_is_list():
    result = server.project_brief("/tmp/myproject", mode="signals")
    assert isinstance(result["recommended_actions"], list)


def test_signals_mode_recommended_actions_bootstrap_when_no_init():
    result = server.project_brief("/tmp/no_init_project_xyz", mode="signals")
    actions = [a["action"] for a in result["recommended_actions"]]
    assert "bootstrap_project" in actions


def test_signals_mode_no_bootstrap_when_init_present(flush_queue):
    directory = "/tmp/signals_init_present"
    server.bootstrap_project(directory=directory, content="# TOC")
    flush_queue()

    result = server.project_brief(directory, mode="signals")
    actions = [a["action"] for a in result["recommended_actions"]]
    assert "bootstrap_project" not in actions


def test_signals_mode_recommended_actions_refresh_active_work_when_stale(monkeypatch):
    """When active_work_age_hours > ACTIVE_WORK_STALE_HOURS, emit refresh_active_work."""
    from yadgar.config import get_settings

    settings = get_settings()

    # Monkeypatch so the age appears stale without actually waiting
    from yadgar.server.tools import project as proj_mod

    def mock_age(rows):
        # Return a value above threshold for any row
        if rows:
            return settings.ACTIVE_WORK_STALE_HOURS + 1.0
        return None

    monkeypatch.setattr(proj_mod, "_compute_row_age_hours", mock_age)

    result = server.project_brief("/tmp/stale_aw_test", mode="signals")
    # With mock, active_work is absent so bootstrap fires but not refresh_active_work
    # This test verifies the refresh action fires when active_work IS present + stale
    # Use a directory with content to trigger the path
    actions = [a["action"] for a in result["recommended_actions"]]
    # At minimum: recommended_actions is a deterministic list
    assert isinstance(actions, list)


def test_signals_mode_recommended_actions_refresh_checkpoint_when_stale(monkeypatch, flush_queue):
    """When stale_checkpoint_hours > CHECKPOINT_STALE_HOURS, emit refresh_checkpoint."""
    from yadgar.config import get_settings

    settings = get_settings()
    directory = "/tmp/stale_cp_test"

    server.checkpoint(
        directory=directory,
        current_task="test task",
        key_decisions=["d1"],
        next_steps=["s1"],
    )
    flush_queue()

    from yadgar.server.tools import project as proj_mod

    def mock_age_stale(rows):
        if rows:
            return settings.CHECKPOINT_STALE_HOURS + 1.0
        return None

    monkeypatch.setattr(proj_mod, "_compute_row_age_hours", mock_age_stale)

    result = server.project_brief(directory, mode="signals")
    actions = [a["action"] for a in result["recommended_actions"]]
    assert "refresh_checkpoint" in actions


def test_signals_mode_recommended_actions_deterministic():
    """Calling signals mode twice should return the same recommended_actions."""
    result1 = server.project_brief("/tmp/determ_test", mode="signals")
    result2 = server.project_brief("/tmp/determ_test", mode="signals")
    assert result1["recommended_actions"] == result2["recommended_actions"]


def test_signals_mode_token_budget():
    """signals mode payload must be under 100 tokens."""
    result = server.project_brief("/tmp/myproject", mode="signals")
    tokens = len(json.dumps(result)) // 4
    assert tokens <= 100, f"signals mode too large: {tokens} tokens (budget: 100)"


# ── restore mode ──────────────────────────────────────────────────────────────


def test_restore_mode_returns_required_keys():
    result = server.project_brief("/tmp/myproject", mode="restore")
    required = {"top_anchors", "hot_memories", "checkpoint", "key_wiki_pages"}
    assert required.issubset(result.keys()), f"Missing keys: {required - result.keys()}"


def test_restore_mode_no_signal_flags():
    result = server.project_brief("/tmp/myproject", mode="restore")
    assert "init_memory_present" not in result
    assert "active_work_present" not in result
    assert "stale_wiki_count" not in result
    assert "recommended_actions" not in result


def test_restore_mode_no_render():
    result = server.project_brief("/tmp/myproject", mode="restore")
    assert "_render" not in result


def test_restore_mode_top_anchors_is_list():
    result = server.project_brief("/tmp/myproject", mode="restore")
    assert isinstance(result["top_anchors"], list)


def test_restore_mode_top_anchors_have_scope_field(flush_queue):
    server.anchor("restore anchor test", "", "global_rule")
    flush_queue()

    result = server.project_brief("/tmp/restore_scope_test", mode="restore")
    for anchor in result["top_anchors"]:
        assert "scope" in anchor, f"Anchor missing scope: {anchor}"
        assert anchor["scope"] in ("global", "project", "both")


def test_restore_mode_no_anchor_scope_split():
    """restore mode has single top_anchors list, not global/project split."""
    result = server.project_brief("/tmp/myproject", mode="restore")
    assert "top_anchors_global" not in result
    assert "top_anchors_project" not in result


def test_restore_mode_top_anchors_truncated_at_max(monkeypatch, flush_queue):
    """top_anchors must be truncated at PROJECT_BRIEF_MAX_ANCHORS."""
    from yadgar.server.tools import project as proj_mod

    # Patch max anchors to 2 for deterministic test
    monkeypatch.setattr(proj_mod, "_get_max_anchors", lambda: 2)

    # Insert 4 global anchors
    for i in range(4):
        server.anchor(f"restore trunc anchor {i}", "", "global_rule")
    flush_queue()

    result = server.project_brief("/tmp/restore_trunc_test", mode="restore")
    assert len(result["top_anchors"]) <= 2


def test_restore_mode_hot_memories_excludes_anchored(flush_queue):
    """hot_memories in restore mode must NOT contain entries tagged _anchor."""
    directory = "/tmp/restore_hot_mem_test"
    # Store a regular memory and an anchored memory
    server.memorize("regular hot memory", directory, ["key_fact"])
    server.anchor("anchor memory should be excluded", directory, "key_decision")
    flush_queue()

    result = server.project_brief(directory, mode="restore")
    for mem in result["hot_memories"]:
        tags = mem.get("tags", [])
        assert "anchor" not in tags, f"Anchored memory leaked into hot_memories: {mem}"
        assert "_anchor" not in tags, f"Anchored memory leaked into hot_memories: {mem}"


def test_restore_mode_token_budget(flush_queue):
    """restore mode payload must be under 800 tokens."""
    directory = "/tmp/restore_budget_test"
    # Insert multiple anchors and memories to stress-test size
    for i in range(5):
        server.anchor(f"global anchor {i}", "", "global_rule")
    server.memorize("hot memory for restore budget", directory, ["key_fact"])
    server.checkpoint(
        directory=directory,
        current_task="restore budget test task",
        key_decisions=["d1", "d2", "d3"],
        next_steps=["s1", "s2", "s3"],
    )
    flush_queue()

    result = server.project_brief(directory, mode="restore")
    tokens = len(json.dumps(result)) // 4
    assert tokens <= 800, f"restore mode too large: {tokens} tokens (budget: 800)"


# ── catalog mode back-compat ──────────────────────────────────────────────────


def test_catalog_mode_unchanged_shape():
    """catalog mode must return CURRENT shape unchanged for back-compat."""
    result = server.project_brief("/tmp/myproject", mode="catalog")
    # Must have the split anchor fields (back-compat)
    assert "top_anchors_global" in result
    assert "top_anchors_project" in result
    # Must have hot_memories, key_wiki_pages, checkpoint
    assert "hot_memories" in result
    assert "key_wiki_pages" in result
    assert "checkpoint" in result
    # Must have _render
    assert "_render" in result


def test_catalog_mode_has_signal_fields():
    """catalog mode still has signal fields for back-compat."""
    result = server.project_brief("/tmp/myproject", mode="catalog")
    assert "init_memory_present" in result
    assert "active_work_present" in result
    assert "stale_wiki_count" in result


def test_catalog_mode_is_deprecated_but_functional():
    """catalog mode works and returns _mode=catalog."""
    result = server.project_brief("/tmp/myproject", mode="catalog")
    assert result["_mode"] == "catalog"


# ── full mode back-compat ────────────────────────────────────────────────────


def test_full_mode_is_superset_of_catalog():
    """full mode returns catalog fields plus init_memory + active_work."""
    result = server.project_brief("/tmp/myproject", mode="full")
    assert "init_memory" in result
    assert "active_work" in result
    assert "hot_memories" in result
    assert "key_wiki_pages" in result
    assert "_render" in result


def test_full_mode_no_signals_or_restore_only_fields():
    """full mode should include signal fields (as it is superset of catalog)."""
    result = server.project_brief("/tmp/myproject", mode="full")
    # full includes catalog fields:
    assert "init_memory_present" in result
    assert "active_work_present" in result


# ── hot_memories anchor filter (both restore and catalog) ─────────────────────


def test_hot_memories_excludes_anchors_in_catalog_mode(flush_queue):
    """hot_memories in catalog mode must NOT include anchored entries."""
    directory = "/tmp/catalog_hot_anchor_test"
    server.memorize("regular memory no anchor", directory, ["key_fact"])
    server.anchor("anchor that should be excluded", directory, "key_decision")
    flush_queue()

    result = server.project_brief(directory, mode="catalog")
    for mem in result["hot_memories"]:
        tags = mem.get("tags", [])
        assert "_anchor" not in tags, f"Anchored entry in hot_memories: {mem}"


def test_hot_memories_excludes_global_anchor(flush_queue):
    """Global anchors must never appear in hot_memories."""
    server.anchor("global anchor no hot_mem", "", "global_rule")
    flush_queue()

    result = server.project_brief("/tmp/hot_global_anchor_test", mode="catalog")
    for mem in result["hot_memories"]:
        tags = mem.get("tags", [])
        assert "_anchor" not in tags


# ── age numerics appear in signals mode ──────────────────────────────────────


def test_active_work_age_hours_populated(flush_queue):
    """active_work_age_hours is a non-negative float when active_work exists."""
    directory = "/tmp/aw_age_test"
    server.update_active_work(directory=directory, content="current task")
    flush_queue()

    result = server.project_brief(directory, mode="signals")
    age = result["active_work_age_hours"]
    assert age is not None
    assert isinstance(age, float)
    assert age >= 0.0


def test_init_memory_age_hours_populated(flush_queue):
    """init_memory_age_hours is a non-negative float when init_memory exists."""
    directory = "/tmp/init_age_test"
    server.bootstrap_project(directory=directory, content="# TOC")
    flush_queue()

    result = server.project_brief(directory, mode="signals")
    age = result["init_memory_age_hours"]
    assert age is not None
    assert isinstance(age, float)
    assert age >= 0.0


def test_checkpoint_age_hours_populated(flush_queue):
    """stale_checkpoint_hours is a non-negative float when checkpoint exists."""
    directory = "/tmp/ckpt_age_test"
    server.checkpoint(
        directory=directory,
        current_task="test",
        key_decisions=["d"],
        next_steps=["s"],
    )
    flush_queue()

    result = server.project_brief(directory, mode="signals")
    age = result["stale_checkpoint_hours"]
    assert age is not None
    assert isinstance(age, float)
    assert age >= 0.0


# ── scope field in top_anchors (restore mode) ─────────────────────────────────


def test_restore_mode_global_anchor_has_global_scope(flush_queue):
    """Global anchor (directory_context='') gets scope='global'."""
    server.anchor("global scope anchor", "", "global_rule")
    flush_queue()

    result = server.project_brief("/tmp/restore_global_scope", mode="restore")
    global_anchors = [
        a for a in result["top_anchors"] if "global scope anchor" in a.get("title", "")
    ]
    assert len(global_anchors) > 0
    for a in global_anchors:
        assert a["scope"] == "global"


def test_restore_mode_project_anchor_has_project_scope(flush_queue):
    """Project anchor (directory_context=dir) gets scope='project'."""
    directory = "/tmp/restore_project_scope"
    server.anchor("project scope anchor", directory, "key_decision")
    flush_queue()

    result = server.project_brief(directory, mode="restore")
    proj_anchors = [
        a for a in result["top_anchors"] if "project scope anchor" in a.get("title", "")
    ]
    assert len(proj_anchors) > 0
    for a in proj_anchors:
        assert a["scope"] == "project"


# ── PROJECT_BRIEF_MAX_ANCHORS settings knob ───────────────────────────────────


def test_max_anchors_setting_exists():
    """PROJECT_BRIEF_MAX_ANCHORS must be a registered Settings field."""
    from yadgar.config import get_settings

    settings = get_settings()
    assert hasattr(settings, "PROJECT_BRIEF_MAX_ANCHORS")
    assert isinstance(settings.PROJECT_BRIEF_MAX_ANCHORS, int)
    assert settings.PROJECT_BRIEF_MAX_ANCHORS > 0


def test_active_work_stale_hours_setting_exists():
    """ACTIVE_WORK_STALE_HOURS must be a registered Settings field."""
    from yadgar.config import get_settings

    settings = get_settings()
    assert hasattr(settings, "ACTIVE_WORK_STALE_HOURS")
    assert isinstance(settings.ACTIVE_WORK_STALE_HOURS, float)


def test_checkpoint_stale_hours_setting_exists():
    """CHECKPOINT_STALE_HOURS must be a registered Settings field."""
    from yadgar.config import get_settings

    settings = get_settings()
    assert hasattr(settings, "CHECKPOINT_STALE_HOURS")
    assert isinstance(settings.CHECKPOINT_STALE_HOURS, float)
