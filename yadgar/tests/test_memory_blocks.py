"""Unit + integration tests for memory_block storage layer and MCP tools.

TDD red-first: tests written before implementation per project convention.

Tests:
  Storage layer:
   1. test_block_create_inserts_row
   2. test_block_create_duplicate_errors
   3. test_block_create_exceeds_char_limit_errors
   4. test_block_create_exceeds_hard_char_limit_errors
   5. test_block_create_exceeds_per_scope_cap_errors
   6. test_block_get_returns_existing
   7. test_block_get_nonexistent_returns_none
   8. test_block_update_replaces_content
   9. test_block_update_over_limit_errors
  10. test_block_delete_removes_row
  11. test_block_delete_nonexistent_idempotent
  12. test_block_list_filters_by_scope
  13. test_block_list_includes_both_scopes_when_scope_none
  14. test_block_scopes_isolated_by_directory
  15. test_block_global_scope_visible_from_any_directory

  MCP tool layer:
  16. test_mcp_block_create_success
  17. test_mcp_block_create_duplicate_errors
  18. test_mcp_block_get_success
  19. test_mcp_block_get_missing_returns_error
  20. test_mcp_block_update_success
  21. test_mcp_block_delete_success
  22. test_mcp_block_list_returns_all
  23. test_mcp_block_create_secret_gate_rejects

  Bootstrap integration:
  24. test_bootstrap_seeds_default_blocks
  25. test_bootstrap_idempotent_does_not_overwrite_blocks
"""

from __future__ import annotations

import pytest

from yadgar import server
from yadgar.storage import StorageEngine

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

_PROJ_DIR = "/home/test/project"
_OTHER_DIR = "/home/test/other"


@pytest.fixture
def storage(tmp_path):
    """Isolated StorageEngine per test."""
    engine = StorageEngine(str(tmp_path / "blocks_test.db"))
    yield engine
    engine.close()


@pytest.fixture(autouse=True)
def _engines(tmp_path):
    """Full server engine stack with isolated DB per test (for MCP tool tests)."""
    server.init_engines(
        db_path=str(tmp_path / "blocks_server_test.db"),
        embedding_model="all-MiniLM-L6-v2",
    )
    yield
    server.shutdown()


# ---------------------------------------------------------------------------
# A. Storage layer tests
# ---------------------------------------------------------------------------


class TestBlockCreate:
    def test_block_create_inserts_row(self, storage: StorageEngine) -> None:
        """create_block returns a dict with id, name, scope, content, char_limit."""
        result = storage.create_block(
            "current_task", "initial content", scope="project", directory=_PROJ_DIR
        )
        assert result is not None
        assert result.get("name") == "current_task"
        assert result.get("scope") == "project"
        assert result.get("content") == "initial content"
        assert result.get("char_limit") == 2000
        assert result.get("id") is not None

    def test_block_create_duplicate_errors(self, storage: StorageEngine) -> None:
        """Second create with same (name, scope, directory) returns error dict."""
        storage.create_block("gotchas", "first", scope="project", directory=_PROJ_DIR)
        result = storage.create_block("gotchas", "second", scope="project", directory=_PROJ_DIR)
        assert result.get("ok") is False
        assert (
            "exists" in result.get("error", "").lower()
            or "duplicate" in result.get("error", "").lower()
        )

    def test_block_create_exceeds_char_limit_errors(self, storage: StorageEngine) -> None:
        """Content longer than char_limit rejected at create."""
        content = "x" * 2001
        result = storage.create_block(
            "big", content, scope="project", directory=_PROJ_DIR, char_limit=2000
        )
        assert result.get("ok") is False
        assert (
            "char_limit" in result.get("error", "").lower()
            or "limit" in result.get("error", "").lower()
        )

    def test_block_create_exceeds_hard_char_limit_errors(self, storage: StorageEngine) -> None:
        """char_limit > 8000 (HARD cap) rejected."""
        result = storage.create_block(
            "huge", "content", scope="project", directory=_PROJ_DIR, char_limit=9000
        )
        assert result.get("ok") is False
        assert "hard" in result.get("error", "").lower() or "8000" in result.get("error", "")

    def test_block_create_exceeds_per_scope_cap_errors(self, storage: StorageEngine) -> None:
        """11th block in same scope rejected (cap = 10)."""
        for i in range(10):
            r = storage.create_block(f"block_{i}", "content", scope="project", directory=_PROJ_DIR)
            assert r.get("ok") is not False, f"block_{i} creation failed: {r}"
        result = storage.create_block(
            "block_overflow", "content", scope="project", directory=_PROJ_DIR
        )
        assert result.get("ok") is False
        assert (
            "cap" in result.get("error", "").lower()
            or "max" in result.get("error", "").lower()
            or "10" in result.get("error", "")
        )


class TestBlockGetUpdateDelete:
    def test_block_get_returns_existing(self, storage: StorageEngine) -> None:
        """get_block returns block dict for existing block."""
        storage.create_block("my_block", "hello world", scope="project", directory=_PROJ_DIR)
        result = storage.get_block("my_block", scope="project", directory=_PROJ_DIR)
        assert result is not None
        assert result.get("content") == "hello world"
        assert result.get("name") == "my_block"

    def test_block_get_nonexistent_returns_none(self, storage: StorageEngine) -> None:
        """get_block returns None for unknown block."""
        result = storage.get_block("nonexistent", scope="project", directory=_PROJ_DIR)
        assert result is None

    def test_block_update_replaces_content(self, storage: StorageEngine) -> None:
        """update_block replaces content and returns updated dict."""
        storage.create_block("task", "old content", scope="project", directory=_PROJ_DIR)
        result = storage.update_block("task", "new content", scope="project", directory=_PROJ_DIR)
        assert result.get("ok") is not False
        assert result.get("content") == "new content"
        fetched = storage.get_block("task", scope="project", directory=_PROJ_DIR)
        assert fetched is not None
        assert fetched.get("content") == "new content"

    def test_block_update_over_limit_errors(self, storage: StorageEngine) -> None:
        """update_block with content > char_limit returns error, content unchanged."""
        storage.create_block("capped", "short", scope="project", directory=_PROJ_DIR, char_limit=10)
        result = storage.update_block("capped", "x" * 11, scope="project", directory=_PROJ_DIR)
        assert result.get("ok") is False
        fetched = storage.get_block("capped", scope="project", directory=_PROJ_DIR)
        assert fetched is not None
        assert fetched.get("content") == "short"

    def test_block_delete_removes_row(self, storage: StorageEngine) -> None:
        """delete_block removes block; get_block returns None afterward."""
        storage.create_block("to_delete", "bye", scope="project", directory=_PROJ_DIR)
        storage.delete_block("to_delete", scope="project", directory=_PROJ_DIR)
        result = storage.get_block("to_delete", scope="project", directory=_PROJ_DIR)
        assert result is None

    def test_block_delete_nonexistent_idempotent(self, storage: StorageEngine) -> None:
        """delete_block on missing block does not raise."""
        # Should not raise
        storage.delete_block("missing", scope="project", directory=_PROJ_DIR)


class TestBlockList:
    def test_block_list_filters_by_scope(self, storage: StorageEngine) -> None:
        """list_blocks with scope='global' returns only global blocks."""
        storage.create_block("global_one", "g", scope="global", directory=None)
        storage.create_block("project_one", "p", scope="project", directory=_PROJ_DIR)
        result = storage.list_blocks(scope="global", directory=None)
        names = [r["name"] for r in result]
        assert "global_one" in names
        assert "project_one" not in names

    def test_block_list_includes_both_scopes_when_scope_none(self, storage: StorageEngine) -> None:
        """list_blocks with scope=None returns both global + project blocks."""
        storage.create_block("global_two", "g", scope="global", directory=None)
        storage.create_block("project_two", "p", scope="project", directory=_PROJ_DIR)
        result = storage.list_blocks(scope=None, directory=_PROJ_DIR)
        names = [r["name"] for r in result]
        assert "global_two" in names
        assert "project_two" in names

    def test_block_scopes_isolated_by_directory(self, storage: StorageEngine) -> None:
        """Project block in one directory not visible from another directory."""
        storage.create_block("proj_block", "content", scope="project", directory=_PROJ_DIR)
        result = storage.list_blocks(scope="project", directory=_OTHER_DIR)
        names = [r["name"] for r in result]
        assert "proj_block" not in names

    def test_block_global_scope_visible_from_any_directory(self, storage: StorageEngine) -> None:
        """Global block visible regardless of query directory."""
        storage.create_block("shared", "for all", scope="global", directory=None)
        for d in [_PROJ_DIR, _OTHER_DIR, "/some/random/path"]:
            result = storage.list_blocks(scope="global", directory=d)
            names = [r["name"] for r in result]
            assert "shared" in names, f"Expected 'shared' visible from {d}"


# ---------------------------------------------------------------------------
# B. MCP tool layer tests
# ---------------------------------------------------------------------------


class TestMcpBlockTools:
    def test_mcp_block_create_success(self) -> None:
        """block_create MCP tool returns id + name on success."""
        from yadgar.server.tools.blocks import block_create

        result = block_create(
            name="my_task", content="do the thing", scope="project", directory=_PROJ_DIR
        )
        assert result.get("ok") is not False
        assert result.get("name") == "my_task"
        assert result.get("content") == "do the thing"

    def test_mcp_block_create_duplicate_errors(self) -> None:
        """block_create returns {ok: False, error: ...} on duplicate."""
        from yadgar.server.tools.blocks import block_create

        block_create(name="dup", content="first", scope="project", directory=_PROJ_DIR)
        result = block_create(name="dup", content="second", scope="project", directory=_PROJ_DIR)
        assert result.get("ok") is False
        assert "error" in result

    def test_mcp_block_get_success(self) -> None:
        """block_get returns content of existing block."""
        from yadgar.server.tools.blocks import block_create, block_get

        block_create(name="fetched", content="hello", scope="project", directory=_PROJ_DIR)
        result = block_get(name="fetched", scope="project", directory=_PROJ_DIR)
        assert result.get("content") == "hello"

    def test_mcp_block_get_missing_returns_error(self) -> None:
        """block_get on nonexistent block returns {ok: False}."""
        from yadgar.server.tools.blocks import block_get

        result = block_get(name="ghost", scope="project", directory=_PROJ_DIR)
        assert result.get("ok") is False

    def test_mcp_block_update_success(self) -> None:
        """block_update replaces content; block_get reflects new value."""
        from yadgar.server.tools.blocks import block_create, block_get, block_update

        block_create(name="updatable", content="original", scope="project", directory=_PROJ_DIR)
        result = block_update(
            name="updatable", content="updated", scope="project", directory=_PROJ_DIR
        )
        assert result.get("ok") is not False
        fetched = block_get(name="updatable", scope="project", directory=_PROJ_DIR)
        assert fetched.get("content") == "updated"

    def test_mcp_block_delete_success(self) -> None:
        """block_delete removes block; block_get returns error afterward."""
        from yadgar.server.tools.blocks import block_create, block_delete, block_get

        block_create(name="killme", content="bye", scope="project", directory=_PROJ_DIR)
        result = block_delete(name="killme", scope="project", directory=_PROJ_DIR)
        assert result.get("deleted") is True or result.get("ok") is not False
        missing = block_get(name="killme", scope="project", directory=_PROJ_DIR)
        assert missing.get("ok") is False

    def test_mcp_block_list_returns_all(self) -> None:
        """block_list returns all blocks for given scope+directory."""
        from yadgar.server.tools.blocks import block_create, block_list

        block_create(name="block_a", content="a", scope="project", directory=_PROJ_DIR)
        block_create(name="block_b", content="b", scope="project", directory=_PROJ_DIR)
        result = block_list(scope="project", directory=_PROJ_DIR)
        names = [r["name"] for r in result]
        assert "block_a" in names
        assert "block_b" in names

    def test_mcp_block_create_secret_gate_rejects(self) -> None:
        """block_create rejects content with secret tokens via gate_or_reject."""
        from yadgar.server.tools.blocks import block_create

        result = block_create(
            name="secret_block",
            content="my key is sk-ant-api03-FAKEFAKEFAKEFAKE",
            scope="project",
            directory=_PROJ_DIR,
        )
        # gate_or_reject returns {stored: False, reason: ..., pattern_preview: ...}
        # The tool passes this through directly on secret detection.
        assert result.get("stored") is False or result.get("ok") is False
        reason = result.get("reason", "") or result.get("error", "")
        assert "secret" in reason.lower() or "blocked" in reason.lower()


# ---------------------------------------------------------------------------
# C. Bootstrap integration tests
# ---------------------------------------------------------------------------


class TestBootstrapSeedsBlocks:
    def test_bootstrap_seeds_default_blocks(self) -> None:
        """bootstrap_project seeds current_task + gotchas blocks on first call."""
        from yadgar.server.tools.blocks import block_list
        from yadgar.server.tools.project import bootstrap_project

        bootstrap_project(directory=_PROJ_DIR, content="# Test project")
        blocks = block_list(scope="project", directory=_PROJ_DIR)
        names = [b["name"] for b in blocks]
        assert "current_task" in names
        assert "gotchas" in names

    def test_bootstrap_idempotent_does_not_overwrite_blocks(self) -> None:
        """Re-running bootstrap_project does not clobber existing block content."""
        from yadgar.server.tools.blocks import block_get, block_update
        from yadgar.server.tools.project import bootstrap_project

        bootstrap_project(directory=_PROJ_DIR, content="# First bootstrap")
        # Manually update current_task

        block_update(
            name="current_task", content="Working on feat/foo", scope="project", directory=_PROJ_DIR
        )
        # Re-run bootstrap
        bootstrap_project(directory=_PROJ_DIR, content="# Second bootstrap")
        block = block_get(name="current_task", scope="project", directory=_PROJ_DIR)
        assert block.get("content") == "Working on feat/foo"
