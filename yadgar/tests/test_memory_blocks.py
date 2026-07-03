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


@pytest.fixture(scope="module")
def storage(module_storage):
    """Module-scoped shared StorageEngine (v5.104 P1B): schema inits ONCE per
    file (was a fresh per-test engine); per-test isolation via the registered
    data-wipe in conftest._wipe_surrealdb_data."""
    return module_storage


@pytest.fixture(autouse=True, scope="module")
def _engines(tmp_path_factory):
    """Full server engine stack with isolated DB per test (for MCP tool tests)."""
    tmp_path = tmp_path_factory.mktemp("memory_blocks")
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


# ---------------------------------------------------------------------------
# D. Config knob regression tests (I25 — v5.35.1)
# ---------------------------------------------------------------------------


class TestMemoryBlockConfigKnobs:
    """I25 regression: the four MEMORY_BLOCK_* knobs must be present in Settings."""

    def test_knobs_present_in_settings(self) -> None:
        """Settings class exposes all four MEMORY_BLOCK_* knobs with correct defaults."""
        from yadgar.config import Settings

        fields = Settings.model_fields
        assert "MEMORY_BLOCK_MAX_PER_SCOPE" in fields, "Missing MEMORY_BLOCK_MAX_PER_SCOPE"
        assert "MEMORY_BLOCK_DEFAULT_CHAR_LIMIT" in fields, (
            "Missing MEMORY_BLOCK_DEFAULT_CHAR_LIMIT"
        )
        assert "MEMORY_BLOCK_HARD_CHAR_LIMIT" in fields, "Missing MEMORY_BLOCK_HARD_CHAR_LIMIT"
        assert "MEMORY_BLOCK_TOTAL_BUDGET_CHARS" in fields, (
            "Missing MEMORY_BLOCK_TOTAL_BUDGET_CHARS"
        )

    def test_knob_defaults(self) -> None:
        """MEMORY_BLOCK_* knobs default to the values specified in the plan."""
        from yadgar.config import Settings

        s = Settings()
        assert s.MEMORY_BLOCK_MAX_PER_SCOPE == 10
        assert s.MEMORY_BLOCK_DEFAULT_CHAR_LIMIT == 2000
        assert s.MEMORY_BLOCK_HARD_CHAR_LIMIT == 8000
        assert s.MEMORY_BLOCK_TOTAL_BUDGET_CHARS == 12000

    def test_blocks_storage_reads_from_config(self, storage: StorageEngine) -> None:
        """create_block honours MEMORY_BLOCK_HARD_CHAR_LIMIT from config (not module constant)."""
        import os

        # Patch env so get_settings() returns a smaller hard cap for this test.
        orig = os.environ.get("YADGAR_MEMORY_BLOCK_HARD_CHAR_LIMIT")
        os.environ["YADGAR_MEMORY_BLOCK_HARD_CHAR_LIMIT"] = "100"
        # Bust the lru_cache so the patched env is picked up.
        from yadgar.config import get_settings

        get_settings.cache_clear()
        try:
            result = storage.create_block(
                "capped_knob", "x" * 50, scope="project", directory=_PROJ_DIR, char_limit=101
            )
            assert result.get("ok") is False, (
                "Expected rejection when char_limit > patched hard cap"
            )
            assert "101" in result.get("error", "") or "hard cap" in result.get("error", "").lower()
        finally:
            if orig is None:
                os.environ.pop("YADGAR_MEMORY_BLOCK_HARD_CHAR_LIMIT", None)
            else:
                os.environ["YADGAR_MEMORY_BLOCK_HARD_CHAR_LIMIT"] = orig
            get_settings.cache_clear()

    def test_i25_knobs_in_registry(self) -> None:
        """All four MEMORY_BLOCK_* env names appear in config_registry._REGISTRY."""
        from yadgar.config_registry import list_config

        names = {e.name for e in list_config()}
        assert "YADGAR_MEMORY_BLOCK_MAX_PER_SCOPE" in names
        assert "YADGAR_MEMORY_BLOCK_DEFAULT_CHAR_LIMIT" in names
        assert "YADGAR_MEMORY_BLOCK_HARD_CHAR_LIMIT" in names
        assert "YADGAR_MEMORY_BLOCK_TOTAL_BUDGET_CHARS" in names

    def test_i25_knobs_in_field_meta(self) -> None:
        """All four MEMORY_BLOCK_* lower-case keys appear in config_yaml.FIELD_META."""
        from yadgar.config_yaml import FIELD_META

        assert "memory_block_max_per_scope" in FIELD_META
        assert "memory_block_default_char_limit" in FIELD_META
        assert "memory_block_hard_char_limit" in FIELD_META
        assert "memory_block_total_budget_chars" in FIELD_META


# ---------------------------------------------------------------------------
# E. block_replace + block_append patch semantics (v5.35.1, Phase 3)
# ---------------------------------------------------------------------------


class TestBlockReplace:
    """block_replace MCP tool: string replacement, error on 0 or >1 matches."""

    def test_replace_success(self) -> None:
        """replace_block replaces exactly-one occurrence, returns updated content."""
        from yadgar.server.tools.blocks import block_create, block_replace

        block_create(
            name="replace_me", content="Hello World!", scope="project", directory=_PROJ_DIR
        )
        result = block_replace(
            name="replace_me",
            old_text="World",
            new_text="Yadgar",
            scope="project",
            directory=_PROJ_DIR,
        )
        assert result.get("ok") is not False
        assert result.get("content") == "Hello Yadgar!"

    def test_replace_not_found_errors(self) -> None:
        """block_replace errors if old_text not found in block content."""
        from yadgar.server.tools.blocks import block_create, block_replace

        block_create(name="no_match", content="Hello World", scope="project", directory=_PROJ_DIR)
        result = block_replace(
            name="no_match", old_text="Missing", new_text="X", scope="project", directory=_PROJ_DIR
        )
        assert result.get("ok") is False
        assert "not found" in result.get("error", "").lower()

    def test_replace_ambiguous_errors(self) -> None:
        """block_replace errors if old_text appears more than once."""
        from yadgar.server.tools.blocks import block_create, block_replace

        block_create(name="ambiguous", content="foo bar foo", scope="project", directory=_PROJ_DIR)
        result = block_replace(
            name="ambiguous", old_text="foo", new_text="baz", scope="project", directory=_PROJ_DIR
        )
        assert result.get("ok") is False
        assert (
            "ambiguous" in result.get("error", "").lower()
            or "more than once" in result.get("error", "").lower()
            or "multiple" in result.get("error", "").lower()
            or "2" in result.get("error", "")
        )

    def test_replace_block_not_found_errors(self) -> None:
        """block_replace on nonexistent block returns {ok: False}."""
        from yadgar.server.tools.blocks import block_replace

        result = block_replace(
            name="ghost", old_text="x", new_text="y", scope="project", directory=_PROJ_DIR
        )
        assert result.get("ok") is False

    def test_replace_secret_gate(self) -> None:
        """block_replace rejects new_text containing secrets (I26)."""
        from yadgar.server.tools.blocks import block_create, block_replace

        block_create(name="safe_r", content="initial content", scope="project", directory=_PROJ_DIR)
        result = block_replace(
            name="safe_r",
            old_text="initial",
            new_text="sk-ant-api03-FAKEFAKEFAKEFAKE",
            scope="project",
            directory=_PROJ_DIR,
        )
        assert result.get("stored") is False or result.get("ok") is False


class TestBlockAppend:
    """block_append MCP tool: append with newline, respect HARD_CHAR_LIMIT."""

    def test_append_success(self) -> None:
        """block_append appends text with newline separator."""
        from yadgar.server.tools.blocks import block_append, block_create

        block_create(name="appendable", content="line one", scope="project", directory=_PROJ_DIR)
        result = block_append(
            name="appendable", text="line two", scope="project", directory=_PROJ_DIR
        )
        assert result.get("ok") is not False
        assert "line one" in result.get("content", "")
        assert "line two" in result.get("content", "")
        # Newline separator
        assert "\n" in result.get("content", "")

    def test_append_block_not_found_errors(self) -> None:
        """block_append on nonexistent block returns {ok: False}."""
        from yadgar.server.tools.blocks import block_append

        result = block_append(name="ghost_append", text="x", scope="project", directory=_PROJ_DIR)
        assert result.get("ok") is False

    def test_append_exceeds_char_limit_errors(self) -> None:
        """block_append respects char_limit — rejects append that would overflow."""
        from yadgar.server.tools.blocks import block_append, block_create

        # char_limit=20, content fills it up so append overflows
        block_create(
            name="full_block",
            content="1234567890",
            scope="project",
            directory=_PROJ_DIR,
            char_limit=10,
        )
        result = block_append(
            name="full_block", text="overflow", scope="project", directory=_PROJ_DIR
        )
        assert result.get("ok") is False
        assert (
            "char_limit" in result.get("error", "").lower()
            or "limit" in result.get("error", "").lower()
            or "exceed" in result.get("error", "").lower()
        )

    def test_append_secret_gate(self) -> None:
        """block_append rejects text containing secrets (I26)."""
        from yadgar.server.tools.blocks import block_append, block_create

        block_create(
            name="safe_append", content="safe content", scope="project", directory=_PROJ_DIR
        )
        result = block_append(
            name="safe_append",
            text="sk-ant-api03-FAKEFAKEFAKEFAKE",
            scope="project",
            directory=_PROJ_DIR,
        )
        assert result.get("stored") is False or result.get("ok") is False
