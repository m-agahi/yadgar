"""E2E for migration 029 — drop the branch column (ADR-0215, Car 9).

Migrations only run in server mode (real SurrealDB v3, ``if not _db_url: return``),
so the actual ``REMOVE FIELD IF EXISTS`` DDL is NEVER exercised by the unit suite
— only the statement string is asserted there via a mock. This file runs the real
DDL against a live ``surreal`` binary to prove:

  1. The DDL is valid SurrealDB v3 syntax. A bad statement crashes every prod DB
     at startup on upgrade — that is the failure mode this guards.
  2. After 029, ``INFO FOR TABLE memory`` / ``INFO FOR TABLE wiki_page`` contain
     no ``branch`` FIELD DEFINITION. This is the plan's fresh-DB exit criterion.
  3. Re-running 029 on already-dropped fields is a no-op (IF EXISTS).

**Deliberately NOT asserted: that stored ``branch`` VALUES disappear.** The
tables are SCHEMALESS (``DEFINE TABLE ... SCHEMALESS`` in ``_init_schema``), so
``REMOVE FIELD`` removes the type definition and nothing else — existing row data
survives as untyped fields. Nulling the values is Car 8's job, not 029's. A test
asserting value removal here would be asserting a behaviour SurrealDB does not
have.

C12 (ADR-0226) note on that paragraph: it remains true of ``REMOVE FIELD``, and of
029, which is why the value-level assertions for the sibling migration live in
``test_migration_032_wiki_page_version_branch_e2e.py`` — 032 nulls the values
EXPLICITLY, precisely because the DDL alone cannot. The one value-shaped assertion
added to this file (``test_no_writer_re_creates_branch_after_the_pair_has_run``)
is about the WRITERS, not the DDL: it proves nothing puts the column back.

Placement: ``yadgar/tests/e2e/`` — collected by ``make e2e`` via @pytest.mark.e2e.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.e2e


def _field_names(storage, table: str) -> set[str]:
    """FIELD definition names on *table*, from INFO FOR TABLE."""
    info = storage._q(f"INFO FOR TABLE {table}")
    if not info:
        return set()
    row = info[0] if isinstance(info, list) else info
    fields = row.get("fields", {}) if isinstance(row, dict) else {}
    return set(fields.keys())


def test_migration_029_removes_branch_field_definitions(e2e_engines):
    """The real REMOVE FIELD DDL drops branch on both tables."""
    from yadgar._shared.storage.migrations import (
        _migration_004_branch_field,
        _migration_029_drop_branch_column,
    )

    storage = e2e_engines["storage"]

    # Re-establish the pre-029 shape: migration 004 is what defined the field.
    # (Running it here also proves 029 undoes exactly what 004 did.)
    _migration_004_branch_field(storage)
    assert "branch" in _field_names(storage, "memory"), (
        "precondition: memory.branch must be defined pre-drop"
    )
    assert "branch" in _field_names(storage, "wiki_page"), (
        "precondition: wiki_page.branch must be defined pre-drop"
    )

    _migration_029_drop_branch_column(storage)

    assert "branch" not in _field_names(storage, "memory"), (
        "migration 029 must remove the memory.branch field definition"
    )
    assert "branch" not in _field_names(storage, "wiki_page"), (
        "migration 029 must remove the wiki_page.branch field definition"
    )


def test_migration_029_idempotent_on_absent_field(e2e_engines):
    """Re-running the drop on already-absent fields is a no-op (IF EXISTS)."""
    from yadgar._shared.storage.migrations import _migration_029_drop_branch_column

    storage = e2e_engines["storage"]

    # Neither call may raise, regardless of starting state.
    _migration_029_drop_branch_column(storage)
    _migration_029_drop_branch_column(storage)

    assert "branch" not in _field_names(storage, "memory")
    assert "branch" not in _field_names(storage, "wiki_page")


def test_migration_029_leaves_wiki_page_version_alone(e2e_engines):
    """029's blast radius is memory + wiki_page only — UNCHANGED by C12.

    REWRITTEN, not deleted (ADR-0226 says so explicitly: *"The test asserting 029's
    blast radius excludes wiki_page_version must be rewritten to assert the new
    boundary rather than deleted, or the coverage is lost silently."*).

    What changed is only the REASON. This used to assert the boundary because
    ``wiki_page_version.branch`` was a deliberate survivor. That survivor is revoked
    — migration **032** drops it. But 029's own reach genuinely is unchanged and
    still worth pinning: a later edit that widened 029 to a third table would be a
    silent scope creep in a shipped migration. The sibling below asserts the other
    half of the new boundary — that 032 is what does the dropping.
    """
    from yadgar._shared.storage.migrations import _migration_029_drop_branch_column

    storage = e2e_engines["storage"]

    storage._q("DEFINE TABLE IF NOT EXISTS wiki_page_version SCHEMALESS;")
    storage._q("DEFINE FIELD IF NOT EXISTS branch ON TABLE wiki_page_version TYPE option<string>;")
    assert "branch" in _field_names(storage, "wiki_page_version"), "precondition"

    _migration_029_drop_branch_column(storage)

    assert "branch" in _field_names(storage, "wiki_page_version"), (
        "029 must not touch wiki_page_version.branch — 032 owns that table"
    )


def test_migration_032_completes_the_boundary_029_stopped_at(e2e_engines):
    """The other half of the new boundary: what 029 leaves, 032 takes (ADR-0226).

    Runs both migrations in registry order against one database, so the pair is
    asserted as the sequence a real upgrade actually applies — not as two
    independent facts that could both hold while the combination did not.

    Value-level assertions live in
    ``test_migration_032_wiki_page_version_branch_e2e.py``; this file's subject is
    the field-definition boundary between the two migrations.
    """
    from yadgar._shared.storage.migrations import (
        _migration_029_drop_branch_column,
        _migration_032_drop_wiki_page_version_branch,
    )

    storage = e2e_engines["storage"]

    storage._q("DEFINE TABLE IF NOT EXISTS wiki_page_version SCHEMALESS;")
    storage._q("DEFINE FIELD IF NOT EXISTS branch ON TABLE wiki_page_version TYPE option<string>;")
    assert "branch" in _field_names(storage, "wiki_page_version"), "precondition"

    _migration_029_drop_branch_column(storage)
    assert "branch" in _field_names(storage, "wiki_page_version"), (
        "029 still stops short of wiki_page_version"
    )

    _migration_032_drop_wiki_page_version_branch(storage)

    assert "branch" not in _field_names(storage, "wiki_page_version"), (
        "032 must finish what 029's scope deliberately excluded"
    )


def test_no_writer_re_creates_branch_after_the_pair_has_run(e2e_engines):
    """The safety property ADR-0226 names — the schema statement alone never was.

    All three tables are SCHEMALESS, so ``REMOVE FIELD`` drops only the type
    definition: a surviving writer re-creates the column untyped and
    ``INFO FOR TABLE`` still reports clean. This runs a real wiki write through the
    storage chokepoint AFTER both migrations and asserts nothing came back — on the
    stored ROWS, since the re-created column would have no definition to show up in
    ``INFO FOR TABLE``.
    """
    from yadgar._shared.storage.migrations import (
        _migration_029_drop_branch_column,
        _migration_032_drop_wiki_page_version_branch,
    )

    storage = e2e_engines["storage"]

    _migration_029_drop_branch_column(storage)
    _migration_032_drop_wiki_page_version_branch(storage)

    pid = storage.insert_wiki_page(
        {
            "title": "C12 re-creation probe",
            "slug": "c12-recreation-probe",
            "content": "body",
            "category": "reference",
            "tags": [],
            "links": [],
            "confidence": 1.0,
            "embedding": None,
            "source_memory_ids": [],
            "directory_context": "/home/max/git/yadgar",
            "project_id": "m-agahi/yadgar",
        }
    )
    storage.update_wiki_page(pid, {"content": "second version"})

    pages = storage._q("SELECT * FROM wiki_page WHERE slug = 'c12-recreation-probe'")
    assert pages, "the probe page was not written"
    assert "branch" not in pages[0], "a writer re-created wiki_page.branch untyped"

    versions = storage._q(f"SELECT * FROM wiki_page_version WHERE page_id = {int(pid)}")
    assert versions, "no version rows written for the probe page"
    for row in versions:
        assert "branch" not in row, "a writer re-created wiki_page_version.branch untyped"
