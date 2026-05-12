"""Tests for the Yadgar MCP server tool functions."""

import json
import tempfile
from pathlib import Path

import pytest

from yadgar import server
from yadgar.tests.conftest import memorize_sync

pytestmark = pytest.mark.xdist_group("server_globals")


@pytest.fixture(autouse=True)
def _engines(tmp_path):
    """Initialize global engines with a temp database for each test."""
    db_path = str(tmp_path / "test.db")
    storage, embeddings, buffer, consolidation, staleness = server.init_engines(
        db_path=db_path, embedding_model="all-MiniLM-L6-v2"
    )
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


def test_recall_finds_by_fts(flush_queue):
    server.memorize("SQLite full text search is useful", "/tmp", ["db"])
    server.memorize("Python asyncio event loop", "/tmp", ["async"])
    flush_queue()

    results = server.recall("SQLite search")
    assert len(results) >= 1
    assert any("SQLite" in r["content"] for r in results)


def test_recall_boosts_heat():
    result = memorize_sync("heat boost test", "/tmp", ["test"])
    mid = result["id"]

    # Set heat to 0.5 so we can observe the boost
    server._get_storage().update_memory_heat(mid, 0.5)

    results = server.recall("heat boost test")
    assert len(results) >= 1
    # Heat should be 0.5 + 0.1 = 0.6
    boosted = [r for r in results if r["id"] == mid]
    assert len(boosted) == 1
    assert abs(boosted[0]["heat"] - 0.6) < 0.01


def test_recall_respects_min_heat():
    r = memorize_sync("low heat memory", "/tmp", ["test"])
    server._get_storage().update_memory_heat(r["id"], 0.05)

    results = server.recall("low heat memory", min_heat=0.5)
    matching = [r for r in results if r["content"] == "low heat memory"]
    assert len(matching) == 0


def test_recall_max_results(flush_queue):
    for i in range(10):
        server.memorize(f"memory number {i} test recall", "/tmp", ["bulk"])
    flush_queue()

    results = server.recall("memory number test recall", max_results=3)
    assert len(results) <= 3


def test_recall_no_embedding_in_results(flush_queue):
    server.memorize("no embedding leak", "/tmp", ["test"])
    flush_queue()
    results = server.recall("no embedding leak")
    for r in results:
        assert "embedding" not in r


# ── forget ─────────────────────────────────────────────────────────────


def test_forget_deletes_memory():
    result = memorize_sync("to be forgotten", "/tmp", ["test"])
    mid = result["id"]

    resp = server.forget(mid)
    assert resp["status"] == "deleted"
    assert resp["memory_id"] == mid

    # Verify it's gone
    assert server._get_storage().get_memory(mid) is None


def test_forget_not_found():
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


# ── get_project_context ────────────────────────────────────────────────


def test_get_project_context_filters_by_directory(flush_queue):
    server.memorize("project A memory", "/projects/a", ["a"])
    server.memorize("project B memory", "/projects/b", ["b"])
    flush_queue()

    result = server.get_project_context("/projects/a")
    assert "memories" in result
    assert all(r["directory_context"] == "/projects/a" for r in result["memories"])


def test_get_project_context_filters_by_heat():
    r = memorize_sync("cold memory", "/projects/c", ["test"])
    server._get_storage().update_memory_heat(r["id"], 0.005)

    result = server.get_project_context("/projects/c")
    assert len(result["memories"]) == 0  # 0.005 < PROJECT_CONTEXT_MIN_HEAT (0.01)


def test_get_project_context_returns_hot(flush_queue):
    server.memorize("hot memory", "/projects/d", ["test"])  # heat=1.0
    flush_queue()

    result = server.get_project_context("/projects/d")
    assert len(result["memories"]) == 1
    assert result["memories"][0]["content"] == "hot memory"
    assert "embedding" not in result["memories"][0]


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
        "remember",
        "recall",
        "forget",
        "validate_memory",
        "get_project_context",
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
    from yadgar.server import _run_check_invariants

    result = _run_check_invariants(server._get_storage())
    assert result["ok"] is True
    assert result["violations"] == []
    assert "counts" in result


def test_check_invariants_detects_dangling_similarity_link():
    """Inserting a memory_similarity_link pointing to a non-existent memory triggers a violation."""
    from yadgar.server import _run_check_invariants

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
    assert result["ok"] is False
    assert any("memory_similarity_link" in v for v in result["violations"])


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
