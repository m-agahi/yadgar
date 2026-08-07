"""Tests for migration_027 — the runtime_config table (ADR-0163, #34).

Coverage:
- migration_027 is registered in _MIGRATIONS with a callable fn
- migration_027 is the final migration in the list (highest number) — the
  'latest migration is appended at the tail' guard, moved forward from 026
- migration_027 defines the runtime_config SCHEMALESS table
"""

from __future__ import annotations

from unittest.mock import MagicMock

from yadgar._shared.storage.migrations import (
    _MIGRATIONS,
    _migration_027_runtime_config_table,
)


class TestMigration027Registration:
    def test_migration_027_in_list(self):
        """027 is registered in _MIGRATIONS."""
        versions = [m["version"] for m in _MIGRATIONS]
        assert "027_runtime_config_table" in versions

    def test_migration_027_fn_is_callable(self):
        """027 entry maps to the callable table-create fn."""
        entry = next(m for m in _MIGRATIONS if m["version"] == "027_runtime_config_table")
        assert callable(entry["fn"])
        assert entry["fn"] is _migration_027_runtime_config_table

    def test_latest_migration_is_the_known_tail(self):
        """The registered migrations' highest version number matches the known tail.

        This does NOT assert 027 specifically is last anymore — 028
        (agent page_type split, ADR-0209) was appended after 027. The guard's
        PURPOSE survives: a hardcoded expected value that must be bumped by
        hand whenever a new migration is appended, so an unnoticed append
        still fails CI. Update the literal below to the new tail whenever a
        later migration is appended; do NOT replace it with something
        computed from `_MIGRATIONS` itself (e.g. asserting the list's max
        equals its own max) — that would never fail and stops being a guard.
        """
        nums = [int(m["version"].split("_")[0]) for m in _MIGRATIONS]
        assert max(nums) == 28


class TestMigration027DefinesTable:
    def test_defines_runtime_config_table(self):
        """The migration issues DEFINE TABLE ... runtime_config."""
        fake = MagicMock()
        _migration_027_runtime_config_table(fake)
        stmts = [c.args[0] for c in fake._q.call_args_list if c.args]
        assert any("DEFINE TABLE" in s and "runtime_config" in s for s in stmts), (
            f"migration_027 must define runtime_config; issued: {stmts}"
        )

    def test_idempotent_uses_if_not_exists(self):
        """Table + index creation is guarded by IF NOT EXISTS (re-run safe)."""
        fake = MagicMock()
        _migration_027_runtime_config_table(fake)
        stmts = [c.args[0] for c in fake._q.call_args_list if c.args]
        assert any("IF NOT EXISTS" in s for s in stmts), (
            f"migration_027 must be idempotent (IF NOT EXISTS); issued: {stmts}"
        )
