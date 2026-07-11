"""Tests for the Yadgar MCP server tool functions."""

import json
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

from yadgar.core import server
from yadgar.tests.conftest import memorize_sync


@pytest.fixture(autouse=True, scope="module")
def _engines(tmp_path_factory):
    """Initialize global engines with a temp database for each test."""
    tmp_path = tmp_path_factory.mktemp("server")
    db_path = str(tmp_path / "test.db")
    storage, embeddings, buffer, consolidation, staleness = server.init_engines(
        db_path=db_path, embedding_model="all-MiniLM-L6-v2"
    )
    # v5.42.3: /tmp/* dirs are not git repos; patch _detect_branch so tests
    # that call memorize/anchor/etc. with /tmp paths pass branch context.
    with (
        patch("yadgar.core.server.tools.project._detect_branch", return_value="feat/test-branch"),
        patch("yadgar.core.server._detect_branch", return_value="feat/test-branch"),
    ):
        yield
    server.shutdown()


# ── remember ───────────────────────────────────────────────────────────


def test_remember_creates_memory():
    result = memorize_sync("pytest is great", "/tmp/project", ["testing"])
    assert result["id"] is not None
    assert result["content"] == "pytest is great"
    assert result["directory_context"] == "/tmp/project"
    assert result["tags"] == ["testing"]
    assert result["heat"] == 1.0
    assert result["is_stale"] is False
    assert "embedding" not in result


def test_remember_computes_file_hash():
    with tempfile.NamedTemporaryFile(mode="w", suffix=".py", delete=False) as f:
        f.write("print('hello')")
        f.flush()
        filepath = f.name

    result = memorize_sync("file-based memory", filepath, ["file"])
    assert result["file_hash"] is not None

    Path(filepath).unlink()


def test_remember_no_file_hash_for_directory():
    result = memorize_sync("directory memory", "/tmp", ["dir"])
    assert result["file_hash"] is None


# ── recall ─────────────────────────────────────────────────────────────


def test_recall_finds_by_fts(flush_queue, recall_backend_bypass):
    server.memorize("SQLite full text search is useful", "/tmp", ["db"])
    server.memorize("Python asyncio event loop", "/tmp", ["async"])
    flush_queue()

    results = server.recall("SQLite search", directory="/tmp")
    assert len(results) >= 1
    assert any("SQLite" in r["content"] for r in results)


def test_recall_boosts_heat():
    # Phase 2a forward-only: recall() forwards to the backend and the heat-boost
    # side effect now runs backend-side in _apply_recall_db_side_effects (called
    # inside _fanout_recall on the backend). recall_backend_bypass deliberately
    # SKIPS db side effects, so it cannot exercise the boost. Test the live
    # heat-boost function directly instead — same code path the backend runs.
    from yadgar.backend.retrieval.recall_pipeline import _apply_recall_db_side_effects

    result = memorize_sync("heat boost test", "/tmp", ["test"])
    mid = result["id"]

    storage = server._get_storage()
    # Set heat to 0.5 so we can observe the boost
    storage.update_memory_heat(mid, 0.5)

    merged = [{"id": mid, "content": "heat boost test", "heat": 0.5}]
    _apply_recall_db_side_effects(merged, "heat boost test", storage)

    # Heat should be 0.5 + 0.1 = 0.6 on the mutated dict...
    assert abs(merged[0]["heat"] - 0.6) < 0.01
    # ...and the batched DB write should have persisted the access boost.
    assert storage.get_memory(mid)["last_accessed"] is not None


def test_recall_respects_min_heat(recall_backend_bypass):
    r = memorize_sync("low heat memory", "/tmp", ["test"])
    server._get_storage().update_memory_heat(r["id"], 0.05)

    results = server.recall("low heat memory", min_heat=0.5, directory="/tmp")
    matching = [r for r in results if r["content"] == "low heat memory"]
    assert len(matching) == 0


def test_recall_max_results(flush_queue, recall_backend_bypass):
    for i in range(10):
        server.memorize(f"memory number {i} test recall", "/tmp", ["bulk"])
    flush_queue()

    results = server.recall("memory number test recall", max_results=3, directory="/tmp")
    assert len(results) <= 3


def test_recall_no_embedding_in_results(flush_queue, recall_backend_bypass):
    server.memorize("no embedding leak", "/tmp", ["test"])
    flush_queue()
    results = server.recall("no embedding leak", directory="/tmp")
    for r in results:
        assert "embedding" not in r


# ── forget ─────────────────────────────────────────────────────────────


def test_forget_deletes_memory(admin_backend_bypass):
    result = memorize_sync("to be forgotten", "/tmp", ["test"])
    mid = result["id"]

    resp = server.forget(mid)
    assert resp["status"] == "deleted"
    assert resp["memory_id"] == mid

    # Verify it's gone
    assert server._get_storage().get_memory(mid) is None


def test_forget_not_found(admin_backend_bypass):
    resp = server.forget(999999)
    assert resp["status"] == "not_found"


# ── validate_memory ────────────────────────────────────────────────────


def test_validate_memory_no_file_hash():
    result = memorize_sync("no hash memory", "/tmp", [])
    resp = server.validate_memory(result["id"])
    assert resp["is_valid"] is True
    assert "no file" in resp["reason"]


def test_validate_memory_file_matches():
    with tempfile.NamedTemporaryFile(mode="w", suffix=".py", delete=False) as f:
        f.write("unchanged content")
        f.flush()
        filepath = f.name

    result = memorize_sync("file memory", filepath, ["file"])
    resp = server.validate_memory(result["id"])
    assert resp["is_valid"] is True
    assert "unchanged" in resp["reason"] or "matches" in resp["reason"]

    Path(filepath).unlink()


def test_validate_memory_file_changed():
    with tempfile.NamedTemporaryFile(mode="w", suffix=".py", delete=False) as f:
        f.write("original content")
        f.flush()
        filepath = f.name

    result = memorize_sync("tracked file", filepath, ["file"])

    # Modify the file
    Path(filepath).write_text("modified content")

    resp = server.validate_memory(result["id"])
    assert resp["is_valid"] is False
    assert "changed" in resp["reason"]

    # Verify staleness was set
    mem = server._get_storage().get_memory(result["id"])
    assert mem["is_stale"] is True

    Path(filepath).unlink()


def test_validate_memory_file_deleted():
    with tempfile.NamedTemporaryFile(mode="w", suffix=".py", delete=False) as f:
        f.write("will be deleted")
        f.flush()
        filepath = f.name

    result = memorize_sync("soon gone", filepath, ["file"])
    Path(filepath).unlink()

    resp = server.validate_memory(result["id"])
    assert resp["is_valid"] is False
    assert "changed" in resp["reason"] or "no longer exists" in resp["reason"]


def test_validate_memory_not_found():
    resp = server.validate_memory(999999)
    assert resp["is_valid"] is False
    assert "not found" in resp["reason"]


def test_project_brief_returns_hot_memories_in_full_mode(flush_queue):
    server.memorize("hot memory", "/projects/d", ["test"])  # heat=1.0
    flush_queue()

    result = server.project_brief("/projects/d", mode="full")
    assert "hot_memories" in result
    contents = [m["content"] for m in result["hot_memories"]]
    assert any("hot memory" in c for c in contents)


# ── consolidate_now ────────────────────────────────────────────────────


def test_consolidate_now_runs():
    resp = server.consolidate_now()
    assert resp["status"] == "completed"
    assert "memories_added" in resp


# ── memory_stats ───────────────────────────────────────────────────────


def test_memory_stats_structure():
    stats = server.memory_stats()
    assert "total_memories" in stats
    assert "active_count" in stats
    assert "archived_count" in stats
    assert "stale_count" in stats
    assert "avg_heat" in stats
    assert "last_consolidation" in stats


def test_memory_stats_counts(flush_queue):
    server.memorize("stat test 1", "/tmp", [])
    server.memorize("stat test 2", "/tmp", [])
    flush_queue()

    stats = server.memory_stats()
    assert stats["total_memories"] == 2
    assert stats["active_count"] == 2
    assert stats["stale_count"] == 0


# ── MCP Resources ─────────────────────────────────────────────────────


def test_resource_stats(flush_queue):
    server.memorize("resource stats test", "/tmp", [])
    flush_queue()
    result = server.resource_stats()
    data = json.loads(result)
    assert data["total_memories"] == 1


def test_resource_hot(flush_queue):
    server.memorize("hot resource test", "/tmp", [])  # heat=1.0
    flush_queue()
    result = server.resource_hot()
    data = json.loads(result)
    assert len(data) == 1
    assert data[0]["content"] == "hot resource test"


def test_resource_stale():
    r = memorize_sync("stale resource test", "/tmp", [])
    server._get_storage().update_memory_staleness(r["id"], True)

    result = server.resource_stale()
    data = json.loads(result)
    assert len(data) == 1
    assert data[0]["is_stale"] is True


# ── MCP server object ─────────────────────────────────────────────────


def test_mcp_server_has_tools():
    """Verify tools are registered on the FastMCP instance."""
    tools = mcp_server_tools()
    tool_names = {t.name for t in tools}
    expected = {
        "memorize",
        "recall",
        "forget",
        "validate_memory",
        "project_brief",
        "consolidate_now",
        "memory_stats",
        "check_invariants",
    }
    assert expected.issubset(tool_names)


def test_mcp_server_has_resources():
    """Verify resources are registered on the FastMCP instance."""
    resources = mcp_server_resources()
    uris = {str(r.uri) for r in resources}
    assert "memory://stats" in uris
    assert "memory://hot" in uris
    assert "memory://stale" in uris


# ── check_invariants ───────────────────────────────────────────────────


def test_check_invariants_ok_on_clean_db():
    """check_invariants returns ok=True on a fresh, empty database."""
    from yadgar.backend.admin_exec.invariants import _run_check_invariants

    result = _run_check_invariants(server._get_storage())
    assert result["ok"] is True
    assert result["violations"] == []
    assert "counts" in result


def test_check_invariants_detects_dangling_similarity_link():
    """Dangling memory_similarity_link rows are auto-repaired and appear in 'fixed', not 'violations'."""
    from yadgar.backend.admin_exec.invariants import _run_check_invariants

    storage = server._get_storage()
    # Insert a link referencing memory IDs that don't exist
    fake_id_a, fake_id_b = 999991, 999992
    storage._q(
        "CREATE memory_similarity_link SET "
        "source_memory_id = $a, target_memory_id = $b, "
        "weight = 0.9, created_at = time::now(), updated_at = time::now()",
        {"a": fake_id_a, "b": fake_id_b},
    )

    result = _run_check_invariants(storage)
    # After auto-repair: ok=True, violations empty, fixed contains the repair description
    assert result["ok"] is True
    assert "fixed" in result
    assert any("memory_similarity_link" in f for f in result["fixed"])
    assert not any("memory_similarity_link" in v for v in result["violations"])


def test_check_invariants_result_has_fixed_key():
    """check_invariants always returns a 'fixed' key, even when nothing was fixed."""
    from yadgar.backend.admin_exec.invariants import _run_check_invariants

    result = _run_check_invariants(server._get_storage())
    assert "fixed" in result
    assert isinstance(result["fixed"], list)


def test_check_invariants_autorepair_wiki_crossref():
    """Dangling wiki_crossref rows are auto-deleted and appear in 'fixed'."""
    from yadgar.backend.admin_exec.invariants import _run_check_invariants

    storage = server._get_storage()
    # Insert a crossref pointing to non-existent slugs
    storage._q(
        "CREATE wiki_crossref SET from_slug = $fs, to_slug = $ts",
        {"fs": "nonexistent-page-from", "ts": "nonexistent-page-to"},
    )

    result = _run_check_invariants(storage)
    assert "fixed" in result
    assert any("wiki_crossref" in f for f in result["fixed"])
    assert not any("wiki_crossref" in v for v in result["violations"])
    # After fix: row should be gone
    remaining = storage._q("SELECT count() AS c FROM wiki_crossref GROUP ALL")
    count = int(remaining[0].get("c", 0)) if remaining else 0
    assert count == 0


def test_check_invariants_autorepair_memory_entity_orphans():
    """memory:N entity rows where N is not a live memory ID are deleted and appear in 'fixed'."""
    from yadgar.backend.admin_exec.invariants import _run_check_invariants

    storage = server._get_storage()
    # Insert an orphan entity (memory ID 888888 doesn't exist)
    storage._q(
        "CREATE entity SET name = $n, entity_type = 'memory_node', "
        "created_at = time::now(), updated_at = time::now()",
        {"n": "memory:888888"},
    )

    result = _run_check_invariants(storage)
    assert "fixed" in result
    assert any("memory:N" in f or "memory_entity" in f or "entity" in f for f in result["fixed"])
    assert not any(
        "memory_entity_orphan" in v or ("entity" in v and "orphan" in v)
        for v in result["violations"]
    )


def test_check_invariants_nonfixable_stays_in_violations():
    """Ceiling breaches and slot anomalies remain in violations (not auto-repaired)."""
    from unittest.mock import patch

    from yadgar.backend.admin_exec.invariants import _run_check_invariants

    storage = server._get_storage()
    # Patch the settings to set HOPFIELD_MAX_PATTERNS to a value that won't match
    # engram_slot count, triggering a structural violation
    with patch("yadgar.core.server.settings") as mock_settings:
        mock_settings.MAX_SIMILARITY_LINKS_PER_MEMORY = 10
        mock_settings.PROJECT_CONTEXT_MIN_HEAT = 0.01
        mock_settings.HOPFIELD_MAX_PATTERNS = 99999  # wrong count → structural violation

        result = _run_check_invariants(storage)
        # The engram_slot mismatch should still be in violations, not fixed
        assert any("engram_slot" in v for v in result["violations"])
        assert result["ok"] is False


# ── install_hooks (global scope) ───────────────────────────────────────


def test_install_hooks_default_scope_is_project(tmp_path):
    """install_hooks with no scope writes to project directory."""
    result = server.install_hooks(str(tmp_path))
    assert result["status"] == "installed"
    assert result.get("scope", "project") == "project"
    # Settings written to project .claude/settings.json
    settings_file = tmp_path / ".claude" / "settings.json"
    assert settings_file.exists()
    data = json.loads(settings_file.read_text())
    assert "hooks" in data


def test_install_hooks_global_scope_writes_to_home(tmp_path, monkeypatch):
    """install_hooks(scope='global') writes SessionStart+PreCompact+PostToolUse hooks to ~/.claude/settings.json."""
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))

    # Also monkeypatch Path.home() used inside install_hooks
    from pathlib import Path as _Path

    monkeypatch.setattr(_Path, "home", staticmethod(lambda: fake_home))

    result = server.install_hooks(str(tmp_path), scope="global")
    assert result["status"] == "installed"
    assert result["scope"] == "global"

    # Global settings.json must contain the hook entries
    global_settings = fake_home / ".claude" / "settings.json"
    assert global_settings.exists()
    data = json.loads(global_settings.read_text())
    hooks = data.get("hooks", {})
    assert "SessionStart" in hooks
    assert "PreCompact" in hooks

    # Project .claude/settings.json should NOT have the project hooks
    project_settings = tmp_path / ".claude" / "settings.json"
    if project_settings.exists():
        proj_data = json.loads(project_settings.read_text())
        proj_hooks = proj_data.get("hooks", {})
        # SessionStart and PreCompact should NOT be in project settings when scope=global
        assert "SessionStart" not in proj_hooks
        assert "PreCompact" not in proj_hooks


def test_install_hooks_global_scope_hooks_dir_is_home(tmp_path, monkeypatch):
    """install_hooks(scope='global') writes hook scripts to ~/.claude/hooks/."""
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    from pathlib import Path as _Path

    monkeypatch.setattr(_Path, "home", staticmethod(lambda: fake_home))

    result = server.install_hooks(str(tmp_path), scope="global")
    global_hooks_dir = fake_home / ".claude" / "hooks"
    assert global_hooks_dir.exists()
    # Hook scripts should be in the global dir
    assert result["hooks_directory"] == str(global_hooks_dir)


def test_install_hooks_global_scope_returns_scope(tmp_path, monkeypatch):
    """install_hooks(scope='global') return value includes scope='global'."""
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    from pathlib import Path as _Path

    monkeypatch.setattr(_Path, "home", staticmethod(lambda: fake_home))

    result = server.install_hooks(str(tmp_path), scope="global")
    assert result["scope"] == "global"


def test_install_hooks_pretooluse_direct_command_not_dispatcher(tmp_path, monkeypatch):
    """v5.20.0: PreToolUse hook uses standalone script, NOT hook_runner.py dispatcher.

    The old wiring routed through hook_runner.py which emitted JSON missing
    hookEventName, causing repeated PreToolUse validation errors in Claude Code 2026.
    The new wiring calls yadgar-db-lockdown-check.py directly.
    """
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    from pathlib import Path as _Path

    monkeypatch.setattr(_Path, "home", staticmethod(lambda: fake_home))

    result = server.install_hooks(str(tmp_path), scope="global")
    assert result["status"] == "installed"

    global_settings = fake_home / ".claude" / "settings.json"
    data = json.loads(global_settings.read_text())
    hooks = data.get("hooks", {})

    # PreToolUse must exist and have Bash matcher
    assert "PreToolUse" in hooks
    pre_tool_entries = hooks["PreToolUse"]
    assert len(pre_tool_entries) >= 1
    entry = pre_tool_entries[0]
    assert entry.get("matcher") == "Bash"
    cmd = entry["hooks"][0]["command"]

    # Must NOT route through hook_runner.py dispatcher
    assert "hook_runner.py" not in cmd, (
        f"PreToolUse should use standalone script, not hook_runner.py dispatcher. Got: {cmd}"
    )
    # Must reference the yadgar-db-lockdown-check.py standalone script
    assert "yadgar-db-lockdown-check.py" in cmd, (
        f"PreToolUse must reference yadgar-db-lockdown-check.py. Got: {cmd}"
    )


# Helpers that call the async list methods on FastMCP
import asyncio  # noqa: E402


def mcp_server_tools():
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(server.mcp_server.list_tools())
    finally:
        loop.close()


def mcp_server_resources():
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(server.mcp_server.list_resources())
    finally:
        loop.close()
