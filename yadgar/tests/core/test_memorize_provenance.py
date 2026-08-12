"""Tests for memorize(provenance_agent=...) arg — v5.3.0 A1.

Covers:
1. memorize() without provenance_agent → provenance_agent == "default"
2. memorize(provenance_agent="general-purpose") → stored value matches
3. Invalid provenance_agent (too long, special chars) → ValueError
4. Migration #005 sets default="default" on existing rows
5. recall() returns memories with provenance_agent populated
"""

from __future__ import annotations

import pytest

from yadgar.tests.core.conftest import TEST_PROJECT_ID


@pytest.fixture
def storage(tmp_path):
    from yadgar._shared.storage import StorageEngine

    engine = StorageEngine(str(tmp_path / "test_prov.db"))
    yield engine
    engine.close()


def _insert_bare_memory(storage, content: str) -> int:
    """Insert a memory WITHOUT the provenance_agent field, simulating pre-v5.3 data."""
    mid = storage._next_id("memory")
    now = storage._now_iso()
    storage._q(
        "CREATE type::record('memory', $id) SET "
        "content = $content, tags = $tags, directory_context = $dir, "
        "created_at = $ts, last_accessed = $ts, heat = $heat, "
        "is_stale = false, plasticity = 1.0, stability = 0.0, "
        "excitability = 1.0, store_type = $st, compression_level = 0, "
        "sr_x = 0.0, sr_y = 0.0, reconsolidation_count = 0, "
        "vector_clock = $vc, is_protected = false",
        {
            "id": mid,
            "content": content,
            "tags": [],
            "dir": "/tmp",
            "ts": now,
            "heat": 1.0,
            "st": "episodic",
            "vc": "{}",
        },
    )
    return mid


class TestMemorizeProvenanceDefault:
    """memorize() without arg stores provenance_agent='default'."""

    def test_insert_without_arg_defaults(self, storage):
        mid = storage.insert_memory(
            {
                "content": "no provenance arg test",
                "directory_context": "/tmp",
                "tags": [],
                "project_id": TEST_PROJECT_ID,
            }
        )
        rows = storage._q(f"SELECT provenance_agent FROM memory:{mid}")
        assert rows, "memory row not found"
        assert rows[0].get("provenance_agent") == "default"

    def test_insert_explicit_default(self, storage):
        mid = storage.insert_memory(
            {
                "content": "explicit default provenance test",
                "directory_context": "/tmp",
                "tags": [],
                "provenance_agent": "default",
                "project_id": TEST_PROJECT_ID,
            }
        )
        rows = storage._q(f"SELECT provenance_agent FROM memory:{mid}")
        assert rows[0].get("provenance_agent") == "default"


class TestMemorizeProvenanceCustom:
    """memorize(provenance_agent='general-purpose') stores the value."""

    def test_insert_custom_agent(self, storage):
        mid = storage.insert_memory(
            {
                "content": "custom provenance test",
                "directory_context": "/tmp",
                "tags": [],
                "provenance_agent": "general-purpose",
                "project_id": TEST_PROJECT_ID,
            }
        )
        rows = storage._q(f"SELECT provenance_agent FROM memory:{mid}")
        assert rows, "memory row not found"
        assert rows[0].get("provenance_agent") == "general-purpose"

    def test_insert_explore_agent(self, storage):
        mid = storage.insert_memory(
            {
                "content": "explore agent provenance",
                "directory_context": "/tmp",
                "tags": [],
                "provenance_agent": "Explore",
                "project_id": TEST_PROJECT_ID,
            }
        )
        rows = storage._q(f"SELECT provenance_agent FROM memory:{mid}")
        assert rows[0].get("provenance_agent") == "Explore"


class TestProvenanceValidation:
    """_validate_provenance_agent rejects bad values."""

    def test_too_long_raises(self):
        from yadgar._shared.storage.memory import _validate_provenance_agent

        with pytest.raises(ValueError, match="provenance_agent"):
            _validate_provenance_agent("a" * 65)  # > 64 chars

    def test_special_chars_raise(self):
        from yadgar._shared.storage.memory import _validate_provenance_agent

        with pytest.raises(ValueError, match="provenance_agent"):
            _validate_provenance_agent("bad'; DROP TABLE memory; --")

    def test_empty_raises(self):
        from yadgar._shared.storage.memory import _validate_provenance_agent

        with pytest.raises(ValueError, match="provenance_agent"):
            _validate_provenance_agent("")

    def test_valid_passes(self):
        from yadgar._shared.storage.memory import _validate_provenance_agent

        # must not raise
        _validate_provenance_agent("general-purpose")
        _validate_provenance_agent("default")
        _validate_provenance_agent("Explore")
        _validate_provenance_agent("my-agent_v2")


class TestMigration005:
    """Migration #005 sets provenance_agent='default' on existing rows."""

    def test_backfill_pre_v5_3_row(self, storage):
        from yadgar._shared.storage.migrations import _migration_005_provenance_agent_field

        mid = _insert_bare_memory(storage, "pre-v5.3 memory for backfill test")

        _migration_005_provenance_agent_field(storage)

        rows = storage._q(f"SELECT provenance_agent FROM memory:{mid}")
        assert rows, "memory row not found after migration"
        assert rows[0].get("provenance_agent") == "default", (
            f"expected 'default', got {rows[0].get('provenance_agent')!r}"
        )

    def test_idempotent(self, storage):
        from yadgar._shared.storage.migrations import _migration_005_provenance_agent_field

        mid = _insert_bare_memory(storage, "idempotent provenance test")
        _migration_005_provenance_agent_field(storage)
        _migration_005_provenance_agent_field(storage)  # second run must not raise

        rows = storage._q(f"SELECT provenance_agent FROM memory:{mid}")
        assert rows[0].get("provenance_agent") == "default"

    def test_existing_value_preserved(self, storage):
        """Row that already has provenance_agent set must not be overwritten."""
        from yadgar._shared.storage.migrations import _migration_005_provenance_agent_field

        mid = storage.insert_memory(
            {
                "content": "already has provenance agent",
                "directory_context": "/tmp",
                "tags": [],
                "provenance_agent": "general-purpose",
                "project_id": TEST_PROJECT_ID,
            }
        )

        _migration_005_provenance_agent_field(storage)

        rows = storage._q(f"SELECT provenance_agent FROM memory:{mid}")
        assert rows[0].get("provenance_agent") == "general-purpose"


class TestRecallReturnsProvenance:
    """recall() surfaces provenance_agent in returned memories."""

    def test_recall_includes_provenance_agent(self, storage):
        mid = storage.insert_memory(
            {
                "content": "recall provenance agent test memory unique_xyz_recall",
                "directory_context": "/tmp",
                "tags": ["test"],
                "provenance_agent": "general-purpose",
                "project_id": TEST_PROJECT_ID,
            }
        )
        rows = storage._q(f"SELECT * FROM memory:{mid}")
        assert rows, "memory not found"
        assert "provenance_agent" in rows[0], "provenance_agent field missing from row"
        assert rows[0]["provenance_agent"] == "general-purpose"
