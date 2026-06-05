"""v5.46.6 — Carryover: empty-string directory_context normalization.

Verifies that insert_memory normalises directory_context='' to 'global'
at write time, so SurrealDB equality queries (`= ''`) do not break
anchor surfacing in embedded mode where empty-string round-trips are
unreliable in SurrealDB 2.x.

This guards against regression of the carryover fix that unblocked
test_anchor_surfacing.test_empty_string_directory_context_treated_as_global.
"""

from __future__ import annotations

import pytest


@pytest.fixture
def storage(tmp_path):
    from yadgar.storage import StorageEngine

    engine = StorageEngine(str(tmp_path / "dc_norm.db"), embedding_dim=384)
    yield engine
    engine.close()


class TestEmptyStringDCNormalization:
    """insert_memory normalises '' directory_context to 'global'."""

    def test_empty_string_stored_as_global(self, storage):
        mid = storage.insert_memory(
            {"content": "empty dc test", "directory_context": "", "tags": []}
        )
        rows = storage._q(f"SELECT directory_context FROM memory:{mid}")
        assert rows, "memory row not found"
        assert rows[0].get("directory_context") == "global", (
            f"expected 'global' after normalisation, got {rows[0].get('directory_context')!r}"
        )

    def test_none_dc_not_affected(self, storage):
        """None directory_context (absent field) is not coerced to 'global'."""
        mid = storage.insert_memory(
            {"content": "no dc test", "directory_context": None, "tags": []}
        )
        rows = storage._q(f"SELECT directory_context FROM memory:{mid}")
        assert rows, "memory row not found"
        # None/NONE is acceptable — normalisation only targets empty string ''
        dc = rows[0].get("directory_context")
        assert dc != "", "None should not become empty string"

    def test_nonempty_dc_preserved(self, storage):
        """Non-empty directory_context is stored verbatim."""
        mid = storage.insert_memory(
            {"content": "explicit dc test", "directory_context": "/repos/myproject", "tags": []}
        )
        rows = storage._q(f"SELECT directory_context FROM memory:{mid}")
        assert rows, "memory row not found"
        assert rows[0].get("directory_context") == "/repos/myproject", (
            f"expected '/repos/myproject', got {rows[0].get('directory_context')!r}"
        )

    def test_global_dc_preserved(self, storage):
        """'global' directory_context is stored verbatim (no double-normalisation)."""
        mid = storage.insert_memory(
            {"content": "global dc test", "directory_context": "global", "tags": []}
        )
        rows = storage._q(f"SELECT directory_context FROM memory:{mid}")
        assert rows, "memory row not found"
        assert rows[0].get("directory_context") == "global"

    def test_empty_dc_anchor_surfaces_in_global_bucket(self, storage):
        """Anchor inserted with dc='' appears via get_anchored_memories_scoped global bucket."""
        storage.insert_memory(
            {
                "content": "empty-dc global anchor",
                "directory_context": "",
                "tags": ["_anchor"],
                "is_protected": True,
                "is_stale": False,
            }
        )
        result = storage.get_anchored_memories_scoped(directory="/some/project", limit=20)
        contents = [r["content"] for r in result]
        assert "empty-dc global anchor" in contents, (
            "anchor with dc='' should appear in global bucket after normalisation to 'global'"
        )
