"""Tests for migration_026 — drop the dead wiki_draft table (v5.157.0, #76).

Coverage:
- migration_026 is registered in _MIGRATIONS list with a callable fn
- migration_026 runs the REMOVE TABLE statement (idempotent — safe on absent table)
- the wiki_draft table is no longer defined in _init_schema's SCHEMALESS table loop
- the dead draft storage methods (insert/get/list/delete_wiki_draft) are gone
"""

from __future__ import annotations

from unittest.mock import MagicMock

from yadgar._shared.storage import StorageEngine
from yadgar._shared.storage.migrations import (
    _MIGRATIONS,
    _migration_026_drop_wiki_draft,
)


class TestMigration026Registration:
    def test_migration_026_in_list(self):
        """026 is registered in _MIGRATIONS."""
        versions = [m["version"] for m in _MIGRATIONS]
        assert "026_drop_wiki_draft" in versions

    def test_migration_026_fn_is_callable(self):
        """026 entry maps to the callable drop fn."""
        entry = next(m for m in _MIGRATIONS if m["version"] == "026_drop_wiki_draft")
        assert callable(entry["fn"])
        assert entry["fn"] is _migration_026_drop_wiki_draft

    def test_migration_026_is_last(self):
        """026 is the final migration in the list (highest number)."""
        nums = [int(m["version"].split("_")[0]) for m in _MIGRATIONS]
        assert max(nums) == 26


class TestMigration026Drop:
    def test_runs_remove_table_statement(self):
        """The migration issues REMOVE TABLE IF EXISTS wiki_draft."""
        fake = MagicMock()
        _migration_026_drop_wiki_draft(fake)
        stmts = [c.args[0] for c in fake._q.call_args_list if c.args]
        assert any("REMOVE TABLE" in s and "wiki_draft" in s for s in stmts), (
            f"migration_026 must drop wiki_draft; issued: {stmts}"
        )

    def test_idempotent_uses_if_exists(self):
        """The drop is guarded by IF EXISTS so re-running is a no-op."""
        fake = MagicMock()
        _migration_026_drop_wiki_draft(fake)
        stmts = [c.args[0] for c in fake._q.call_args_list if c.args]
        assert any("IF EXISTS" in s for s in stmts), (
            f"migration_026 drop must be idempotent (IF EXISTS); issued: {stmts}"
        )


class TestWikiDraftSubsystemRemoved:
    def test_wiki_draft_not_in_init_schema_tables(self):
        """_init_schema no longer defines a wiki_draft SCHEMALESS table."""
        import inspect

        src = inspect.getsource(StorageEngine._init_schema)
        assert '"wiki_draft"' not in src, "wiki_draft still defined in _init_schema"

    def test_draft_storage_methods_gone(self):
        """The dead draft CRUD storage methods no longer exist."""
        for name in (
            "insert_wiki_draft",
            "get_wiki_draft_by_slug",
            "list_wiki_drafts",
            "delete_wiki_draft",
        ):
            assert not hasattr(StorageEngine, name), f"{name} should be removed"
