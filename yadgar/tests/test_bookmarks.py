"""Unit + integration tests for wiki_bookmark storage layer and MCP tools.

TDD red-first: tests written before implementation per project convention.

Tests:
  1. test_bookmark_add_creates_row
  2. test_bookmark_add_idempotent
  3. test_bookmark_remove_idempotent
  4. test_bookmark_list_returns_ordered
  5. test_bookmark_reorder_shifts_others
  6. test_bookmark_add_normalizes_slug
  7. test_bookmark_label_override_optional
  8. test_bookmark_mcp_add_creates_row
  9. test_bookmark_mcp_add_idempotent
 10. test_bookmark_mcp_remove_idempotent
 11. test_bookmark_mcp_list_ordered
 12. test_bookmark_mcp_reorder
 13. test_bookmark_add_empty_slug_rejected
"""

from __future__ import annotations

import pytest

from yadgar._shared.storage import StorageEngine
from yadgar.core import server


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
@pytest.fixture(scope="module")
def storage(module_storage):
    """Module-scoped shared StorageEngine (v5.104 P1B): schema inits ONCE per
    file (was a fresh per-test engine); per-test isolation via the registered
    data-wipe in conftest._wipe_surrealdb_data."""
    return module_storage


@pytest.fixture(autouse=True, scope="module")
def _engines(tmp_path_factory):
    """Full server engine stack, initialized ONCE per module (for MCP tool tests).

    Module-scoped (v5.101 P1): init_engines() runs the SurrealDB schema init once
    per file instead of per test.  Per-test isolation is provided by the
    function-scoped autouse `_wipe_surrealdb_data` in conftest (DATA wipe on the
    shared per-file namespace).  Uses tmp_path_factory (session-scoped) — a
    module-scoped fixture cannot request the function-scoped tmp_path.
    """
    tmp_path = tmp_path_factory.mktemp("bookmarks")
    server.init_engines(
        db_path=str(tmp_path / "bm_server_test.db"),
        embedding_model="all-MiniLM-L6-v2",
    )
    yield
    server.shutdown()


# ---------------------------------------------------------------------------
# A. Storage layer tests
# ---------------------------------------------------------------------------


class TestBookmarkStorageAdd:
    def test_bookmark_add_creates_row(self, storage: StorageEngine) -> None:
        """add_bookmark returns a dict with slug + id; row exists afterward."""
        result = storage.add_bookmark("my-slug")
        assert result is not None
        assert result.get("slug") == "my-slug"
        row = storage.get_bookmark("my-slug")
        assert row is not None
        assert row["slug"] == "my-slug"

    def test_bookmark_add_idempotent(self, storage: StorageEngine) -> None:
        """Calling add_bookmark twice on same slug doesn't raise and returns one row."""
        storage.add_bookmark("dup-slug")
        storage.add_bookmark("dup-slug", label_override="New Label")
        rows = storage.list_bookmarks()
        assert sum(1 for r in rows if r["slug"] == "dup-slug") == 1

    def test_bookmark_add_label_override_updates(self, storage: StorageEngine) -> None:
        """Second add on same slug updates the label_override."""
        storage.add_bookmark("upd-slug", label_override="Old Label")
        storage.add_bookmark("upd-slug", label_override="New Label")
        row = storage.get_bookmark("upd-slug")
        assert row is not None
        assert row.get("label_override") == "New Label"

    def test_bookmark_add_empty_slug_raises(self, storage: StorageEngine) -> None:
        """add_bookmark with empty or whitespace-only slug raises ValueError."""
        with pytest.raises(ValueError):
            storage.add_bookmark("")
        with pytest.raises(ValueError):
            storage.add_bookmark("   ")


class TestBookmarkStorageRemove:
    def test_bookmark_remove_existing(self, storage: StorageEngine) -> None:
        """remove_bookmark deletes an existing row and returns True."""
        storage.add_bookmark("del-slug")
        result = storage.remove_bookmark("del-slug")
        assert result is True
        assert storage.get_bookmark("del-slug") is None

    def test_bookmark_remove_idempotent(self, storage: StorageEngine) -> None:
        """remove_bookmark on nonexistent slug returns False without raising."""
        result = storage.remove_bookmark("ghost-slug")
        assert result is False


class TestBookmarkStorageList:
    def test_bookmark_list_returns_ordered(self, storage: StorageEngine) -> None:
        """list_bookmarks returns items sorted by position ascending."""
        storage.add_bookmark("slug-a")
        storage.add_bookmark("slug-b")
        storage.add_bookmark("slug-c")
        rows = storage.list_bookmarks()
        assert len(rows) >= 3
        [r["slug"] for r in rows if r["slug"] in ("slug-a", "slug-b", "slug-c")]
        positions = [
            r.get("position", 0) for r in rows if r["slug"] in ("slug-a", "slug-b", "slug-c")
        ]
        for i in range(len(positions) - 1):
            assert positions[i] <= positions[i + 1], "list_bookmarks not ordered by position"

    def test_bookmark_list_empty(self, storage: StorageEngine) -> None:
        """list_bookmarks returns empty list when no bookmarks exist."""
        rows = storage.list_bookmarks()
        assert rows == []


class TestBookmarkStorageReorder:
    def test_bookmark_reorder_shifts_others(self, storage: StorageEngine) -> None:
        """Moving slug-b to position 0 shifts slug-a to position 1."""
        storage.add_bookmark("slug-a")
        storage.add_bookmark("slug-b")
        storage.reorder_bookmark("slug-b", 0)
        rows = storage.list_bookmarks()
        by_slug = {r["slug"]: r["position"] for r in rows}
        assert by_slug["slug-b"] == 0
        assert by_slug["slug-a"] == 1

    def test_bookmark_reorder_nonexistent_slug(self, storage: StorageEngine) -> None:
        """reorder_bookmark on nonexistent slug returns False without raising."""
        result = storage.reorder_bookmark("ghost-slug", 0)
        assert result is False


class TestBookmarkStorageSlugNormalization:
    def test_slug_stripped_of_whitespace(self, storage: StorageEngine) -> None:
        """add_bookmark strips leading/trailing whitespace from slug."""
        result = storage.add_bookmark("  trimmed-slug  ")
        assert result.get("slug") == "trimmed-slug"
        row = storage.get_bookmark("trimmed-slug")
        assert row is not None


class TestBookmarkLabelOverride:
    def test_label_override_optional(self, storage: StorageEngine) -> None:
        """add_bookmark without label_override stores label_override as None or empty."""
        storage.add_bookmark("no-label-slug")
        row = storage.get_bookmark("no-label-slug")
        assert row is not None
        # label_override should be None or empty string — frontend falls back to wiki title
        assert row.get("label_override") in (None, "")


# ---------------------------------------------------------------------------
# B. MCP tool tests (via server layer)
# ---------------------------------------------------------------------------


class TestBookmarkMCPAdd:
    def test_bookmark_mcp_add_creates_row(self) -> None:
        """bookmark_add MCP tool returns {added: true, slug: ...}."""
        from yadgar.core.server.tools.bookmarks import bookmark_add

        result = bookmark_add("mcp-slug")
        assert result.get("added") is True
        assert result.get("slug") == "mcp-slug"

    def test_bookmark_mcp_add_idempotent(self) -> None:
        """bookmark_add called twice returns {added: true} both times without error."""
        from yadgar.core.server.tools.bookmarks import bookmark_add

        r1 = bookmark_add("idem-slug")
        r2 = bookmark_add("idem-slug", label_override="New")
        assert r1.get("added") is True
        assert r2.get("added") is True

    def test_bookmark_mcp_add_empty_slug_rejected(self) -> None:
        """bookmark_add with empty slug returns {added: false, reason: ...}."""
        from yadgar.core.server.tools.bookmarks import bookmark_add

        result = bookmark_add("")
        assert result.get("added") is False
        assert "reason" in result


class TestBookmarkMCPRemove:
    def test_bookmark_mcp_remove_existing(self) -> None:
        """bookmark_remove returns {removed: true} for existing bookmark."""
        from yadgar.core.server.tools.bookmarks import bookmark_add, bookmark_remove

        bookmark_add("rm-slug")
        result = bookmark_remove("rm-slug")
        assert result.get("removed") is True

    def test_bookmark_mcp_remove_idempotent(self) -> None:
        """bookmark_remove on nonexistent slug returns {removed: false}."""
        from yadgar.core.server.tools.bookmarks import bookmark_remove

        result = bookmark_remove("ghost")
        assert result.get("removed") is False


class TestBookmarkMCPList:
    def test_bookmark_mcp_list_ordered(self) -> None:
        """bookmark_list returns list ordered by position ascending."""
        from yadgar.core.server.tools.bookmarks import bookmark_add, bookmark_list

        bookmark_add("list-a")
        bookmark_add("list-b")
        results = bookmark_list()
        assert isinstance(results, list)
        positions = [r.get("position", 0) for r in results]
        for i in range(len(positions) - 1):
            assert positions[i] <= positions[i + 1]

    def test_bookmark_mcp_list_empty(self) -> None:
        """bookmark_list returns [] when no bookmarks."""
        from yadgar.core.server.tools.bookmarks import bookmark_list

        results = bookmark_list()
        assert results == []


class TestBookmarkMCPReorder:
    def test_bookmark_mcp_reorder(self) -> None:
        """bookmark_reorder moves slug to new_position and returns {reordered: true}."""
        from yadgar.core.server.tools.bookmarks import bookmark_add, bookmark_list, bookmark_reorder

        bookmark_add("reord-a")
        bookmark_add("reord-b")
        result = bookmark_reorder("reord-b", 0)
        assert result.get("reordered") is True
        rows = bookmark_list()
        by_slug = {r["slug"]: r["position"] for r in rows}
        assert by_slug["reord-b"] == 0
        assert by_slug["reord-a"] == 1

    def test_bookmark_mcp_reorder_nonexistent(self) -> None:
        """bookmark_reorder on nonexistent slug returns {reordered: false}."""
        from yadgar.core.server.tools.bookmarks import bookmark_reorder

        result = bookmark_reorder("ghost", 0)
        assert result.get("reordered") is False
