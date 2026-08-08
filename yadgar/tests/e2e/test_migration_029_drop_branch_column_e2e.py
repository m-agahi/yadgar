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
    """029's blast radius is memory + wiki_page only.

    ``wiki_page_version`` carries its own ``branch`` snapshot column (an audit
    trail). ADR-0215's Car 9 scope does not include it; this asserts 029 does
    not reach across and drop it as a side effect.
    """
    from yadgar._shared.storage.migrations import _migration_029_drop_branch_column

    storage = e2e_engines["storage"]

    storage._q("DEFINE TABLE IF NOT EXISTS wiki_page_version SCHEMALESS;")
    storage._q("DEFINE FIELD IF NOT EXISTS branch ON TABLE wiki_page_version TYPE option<string>;")
    assert "branch" in _field_names(storage, "wiki_page_version"), "precondition"

    _migration_029_drop_branch_column(storage)

    assert "branch" in _field_names(storage, "wiki_page_version"), (
        "029 must not touch wiki_page_version.branch"
    )
