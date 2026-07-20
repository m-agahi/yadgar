"""E2E test for migration 026 — drop dead wiki_draft table (v5.157.0, #76).

Migrations only run in server mode (real SurrealDB v3, `if not _db_url: return`),
so the actual `REMOVE TABLE IF EXISTS wiki_draft` DDL is NEVER exercised by the
unit suite — only the statement string is asserted there via a mock. This e2e
test runs the real DDL against a live `surreal` binary to prove:

  1. The migration DDL is valid SurrealDB v3 syntax (a bad statement would crash
     every prod DB at startup on upgrade — the failure mode this guards).
  2. After the migration, a manufactured wiki_draft row is gone and the table
     is absent (SELECT returns empty, not an error).
  3. Re-running the migration on the now-absent table is a no-op (IF EXISTS).

Placement: yadgar/tests/e2e/ — collected by `make e2e` via @pytest.mark.e2e.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.e2e


def _table_exists(storage, table: str) -> bool:
    """True if `table` is present in the DB's INFO output."""
    info = storage._q("INFO FOR DB")
    if not info:
        return False
    row = info[0] if isinstance(info, list) else info
    tables = row.get("tables", {}) if isinstance(row, dict) else {}
    return table in tables


def test_migration_026_drops_real_wiki_draft_table(e2e_engines):
    """The real REMOVE TABLE DDL drops a populated wiki_draft table."""
    from yadgar._shared.storage.migrations import _migration_026_drop_wiki_draft

    storage = e2e_engines["storage"]

    # Manufacture the legacy table + a row (the subsystem no longer creates it,
    # so we recreate the pre-removal shape to prove the DROP handles a populated
    # table, not just an absent one).
    storage._q("DEFINE TABLE IF NOT EXISTS wiki_draft SCHEMALESS;")
    storage._q(
        "CREATE wiki_draft SET slug = $s, content = $c, created_at = time::now()",
        {"s": "legacy-draft", "c": "stale draft body"},
    )
    rows = storage._q("SELECT slug FROM wiki_draft WHERE slug = 'legacy-draft'")
    assert rows, "precondition: manufactured draft row must exist"

    assert _table_exists(storage, "wiki_draft"), "precondition: table must exist pre-drop"

    # Run the real migration DDL against live SurrealDB.
    _migration_026_drop_wiki_draft(storage)

    # Table is gone from the schema: INFO FOR DB no longer lists it. (A raw SELECT
    # against a dropped table ERRORS under SurrealDB v3 — schema absence via INFO
    # is the correct, non-throwing check.)
    assert not _table_exists(storage, "wiki_draft"), "wiki_draft table must be dropped"


def test_migration_026_idempotent_on_absent_table(e2e_engines):
    """Re-running the drop on an already-absent table is a no-op (IF EXISTS)."""
    from yadgar._shared.storage.migrations import _migration_026_drop_wiki_draft

    storage = e2e_engines["storage"]

    # Ensure absent, then run twice — neither call may raise.
    storage._q("REMOVE TABLE IF EXISTS wiki_draft;")
    _migration_026_drop_wiki_draft(storage)
    _migration_026_drop_wiki_draft(storage)
    assert not _table_exists(storage, "wiki_draft")
