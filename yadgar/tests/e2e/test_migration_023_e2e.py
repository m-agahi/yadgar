"""E2E tests for migration 023 — memory directory_context pre-flip backfill.

Design:
  Migration 023 is a defensive pre-flip gate: on any DB that ran migration 018
  the DEFINE FIELD ASSERT on memory.directory_context already prevents field-absent
  inserts, so the migration finds 0 rows to fix.  The e2e test must therefore
  *manufacture* a field-absent row, then call the migration directly and assert:

    1. The field-absent row is backfilled to 'global'.
    2. The row is subsequently returned by a directory-scoped fan-out recall
       (because 'global' is an always-eligible sentinel).
    3. A row already stamped with a non-empty directory_context is NOT touched
       (idempotency).
    4. The ASSERT constraint is correctly re-tightened after migration (a raw
       INSERT without directory_context is rejected again).

Manufacturing a field-absent row (see _seed_field_absent_memory):
  insert_memory() first (assigns the integer id the migration + recall key on,
  plus a real vector), then relax the schema (DEFINE FIELD OVERWRITE ...
  option<string>) and UPDATE the row to clear directory_context (= NONE). A raw
  INSERT is unusable — it yields a random SurrealDB record id that _extract_id
  cannot parse as the integer the migration + recall both require.

Placement: yadgar/tests/e2e/ — collected by `make e2e` via @pytest.mark.e2e.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.e2e

YADGAR_DIR = "/home/test/yadgar-project"

#: The identity every seed and every recall in this file names. C5/ADR-0227
#: made ``project_id`` mandatory at the storage write chokepoint, so even the
#: field-absent row this file manufactures must be INSERTED under an identity
#: before its ``directory_context`` is cleared. The two dimensions are
#: independent: migration 023 repairs ``directory_context`` and never touches
#: ``project_id``.
_TEST_PROJECT = "m-agahi/yadgar"


def _run_fanout_recall(monkeypatch, query: str, directory: str, max_results: int = 20):
    """Run fan-out recall (UNIFIED_RECALL_ENABLED=True) via the MCP tool."""
    import sys

    _rm = sys.modules.get("yadgar.core.server.tools.recall")
    if _rm is None:
        import yadgar.core.server.tools.recall as _rm

    return _rm.recall(
        query=query, directory=directory, max_results=max_results, project=_TEST_PROJECT
    )


def _seed_field_absent_memory(storage, embeddings, content: str) -> int:
    """Manufacture a field-absent memory row with a real yadgar integer id.

    insert_memory() assigns the integer id the migration + recall key on, and a
    real vector (needed for the recall-surfacing test). We then relax the schema
    and UPDATE the row to clear directory_context (NONE), reproducing the legacy
    field-absent state the migration repairs.

    A raw INSERT cannot be used: it yields a random SurrealDB record id that
    storage._extract_id cannot parse as an integer (the migration + recall both
    key on integer ids).

    Leaves the schema RELAXED — migration 023 Phase A relaxes it again (OVERWRITE)
    and Phase C re-tightens, so no restore is needed here.
    """
    emb = embeddings.encode(content)  # bytes — insert_memory serializes internally
    mid = storage.insert_memory(
        {
            "content": content,
            "embedding": emb,
            "directory_context": YADGAR_DIR,
            "project_id": _TEST_PROJECT,
            "tags": [],
            "heat": 1.0,
        }
    )
    assert mid is not None, "insert_memory must return an integer id"
    # Relax the ASSERT so we can null the field, then clear directory_context.
    storage._q("DEFINE FIELD OVERWRITE directory_context ON TABLE memory TYPE option<string>;")
    storage._q(
        "UPDATE type::record('memory', $id) SET directory_context = NONE",
        {"id": mid},
    )
    return mid


class TestMigration023E2E:
    """Live-DB e2e tests for migration_023 memory directory_context backfill."""

    def test_field_absent_row_backfilled_to_global(self, e2e_engines):
        """A field-absent memory row is backfilled to directory_context='global'.

        Manufactures a field-absent row by temporarily relaxing the schema
        constraint, then runs the migration and asserts the row is stamped.
        """
        storage = e2e_engines["storage"]
        embeddings = e2e_engines["embeddings"]
        unique_token = "xzmig023a"

        # Manufacture a field-absent row with a real integer id (see helper).
        mem_id = _seed_field_absent_memory(
            storage, embeddings, f"migration023 field-absent test {unique_token}"
        )

        # Verify the row is genuinely field-absent before migration (guards that
        # SET directory_context = NONE produced the filter-matching state).
        row_before = storage._q(
            "SELECT id, directory_context FROM memory WHERE id = type::record('memory', $id)",
            {"id": mem_id},
        )
        assert row_before, f"Inserted row not found: id={mem_id}"
        assert row_before[0].get("directory_context") in (None, ""), (
            f"Expected field-absent, got {row_before[0].get('directory_context')!r}"
        )

        # Call the migration directly (it relaxes Phase A + re-tightens Phase C).
        from yadgar._shared.storage.migrations import (
            _migration_023_memory_directory_context_backfill,
        )

        _migration_023_memory_directory_context_backfill(storage)

        # Assert the row is now stamped 'global'.
        row_after = storage._q(
            "SELECT id, directory_context FROM memory WHERE id = type::record('memory', $id)",
            {"id": mem_id},
        )
        assert row_after, f"Row not found after migration: id={mem_id}"
        assert row_after[0].get("directory_context") == "global", (
            f"Expected 'global', got {row_after[0].get('directory_context')!r} for id={mem_id}"
        )

    def test_backfilled_row_surfaces_in_directory_recall(
        self, e2e_engines, monkeypatch, recall_backend_bypass
    ):
        """After backfill to 'global', the row is still returned by a scoped recall.

        C13 (e) corrects a now-stale premise. This used to read "'global' is an
        always-eligible sentinel — it must surface for any directory query".
        Car C7 retired that: reach is decided by ``project_id`` plus the
        ``'global'`` reach TAG in the stage-1 WHERE, and ``directory_context``
        scopes nothing. The assertion is unchanged and still load-bearing — what
        it proves is that migration 023's backfill does not render the row
        unreachable — but it passes on the row's PROJECT, not on the literal
        ``directory_context='global'`` this docstring once credited.
        """
        storage = e2e_engines["storage"]
        embeddings = e2e_engines["embeddings"]
        unique_token = "xzmig023b"

        # Manufacture a field-absent row with a real integer id + real vector
        # (the vector is needed for the recall below to surface it).
        mem_id = _seed_field_absent_memory(
            storage, embeddings, f"migration023 recall test {unique_token}"
        )

        from yadgar._shared.storage.migrations import (
            _migration_023_memory_directory_context_backfill,
        )

        _migration_023_memory_directory_context_backfill(storage)

        # Step f: fan-out recall scoped to the row's own project (C7: the
        # stage-1 WHERE keys on project_id; directory_context scopes nothing).
        results = _run_fanout_recall(
            monkeypatch,
            f"migration023 recall test {unique_token}",
            YADGAR_DIR,
        )
        result_ids = {r.get("id") for r in results}
        assert mem_id in result_ids, (
            f"Backfilled row id={mem_id} (directory_context='global') should surface "
            f"in recall(directory=YADGAR_DIR); got result_ids={result_ids}"
        )

    def test_already_stamped_row_unchanged(self, e2e_engines):
        """Migration 023 is idempotent — already-stamped rows are NOT modified.

        Rows with a non-empty directory_context are skipped.
        """
        storage = e2e_engines["storage"]
        embeddings = e2e_engines["embeddings"]
        unique_token = "xzmig023c"
        target_dir = "/home/test/stamped-project"

        # Insert a row with a real directory_context (not field-absent).
        # emb stays bytes — insert_memory serializes the embedding internally.
        emb = embeddings.encode(f"migration023 stamped test {unique_token}")
        mem_id = storage.insert_memory(
            {
                "content": f"migration023 stamped test {unique_token}",
                "embedding": emb,
                "directory_context": target_dir,
                "project_id": _TEST_PROJECT,
                "tags": [],
                "heat": 1.0,
            }
        )
        assert mem_id is not None

        # Run migration 023
        from yadgar._shared.storage.migrations import (
            _migration_023_memory_directory_context_backfill,
        )

        _migration_023_memory_directory_context_backfill(storage)

        # The stamped row must retain its original directory_context, not be overwritten
        rows = storage._q(
            "SELECT directory_context FROM memory WHERE id = type::record('memory', $id)",
            {"id": mem_id},
        )
        assert rows, f"Row {mem_id} not found"
        assert rows[0].get("directory_context") == target_dir, (
            f"Already-stamped row must retain {target_dir!r}; "
            f"got {rows[0].get('directory_context')!r}"
        )

    def test_assert_constraint_restored_after_migration(self, e2e_engines):
        """After migration 023, field-absent inserts are rejected by the ASSERT.

        Migration Phase C re-tightens the schema. This test verifies the constraint
        is back in place by attempting a raw INSERT without directory_context.
        """
        storage = e2e_engines["storage"]
        embeddings = e2e_engines["embeddings"]
        unique_token = "xzmig023d"

        # Relax → insert one dummy row (to have at least one row) → restore
        storage._q("DEFINE FIELD OVERWRITE directory_context ON TABLE memory TYPE option<string>;")
        emb = storage._bytes_to_floats(
            embeddings.encode(f"migration023 constraint test {unique_token}")
        )
        storage._q(
            "INSERT INTO memory {content: $c, heat: 1.0, embedding: $emb, tags: [], directory_context: 'global'}",
            {"c": f"migration023 constraint test {unique_token}", "emb": emb},
        )
        storage._q(
            "DEFINE FIELD OVERWRITE directory_context ON TABLE memory TYPE string "
            "ASSERT $value != NONE AND string::len($value) > 0;"
        )

        # Run migration 023 — Phase C will re-tighten the constraint
        from yadgar._shared.storage.migrations import (
            _migration_023_memory_directory_context_backfill,
        )

        _migration_023_memory_directory_context_backfill(storage)

        # Attempt field-absent insert: should be rejected post-migration
        constraint_enforced = False
        try:
            storage._q(
                "INSERT INTO memory {content: $c, heat: 1.0, embedding: $emb, tags: []}",
                {
                    "c": f"migration023 constraint probe {unique_token}",
                    "emb": emb,
                },
            )
        except Exception:
            constraint_enforced = True

        # Migration Phase C re-tightened the schema, so the field-absent INSERT
        # must now be rejected by the directory_context ASSERT. (This is the same
        # constraint the whole directory contract — and the other migration tests —
        # rely on; the target SurrealDB enforces ASSERT on INSERT.)
        assert constraint_enforced, (
            "field-absent INSERT was NOT rejected after migration Phase C re-tighten — "
            "the directory_context ASSERT constraint was not restored"
        )
