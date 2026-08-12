"""E2E for migration 032 — drop ``branch`` from ``wiki_page_version`` (ADR-0226, C12).

**This file exists because an ``INFO FOR TABLE`` assertion cannot see the bug.**
``wiki_page_version`` is SCHEMALESS and migration 013 never issued a
``DEFINE FIELD`` for ``branch`` — the writers simply set it on the row. So there
may be no FIELD DEFINITION to remove, and a 032 whose entire body is
``REMOVE FIELD IF EXISTS`` would be a **no-op that still passes** a
field-definition test. That is migration 031's failure shape (a filter on a
column it never projected) in different clothing.

Every assertion here is therefore on a **SELECTed row**, after a real row has
been written carrying a real ``branch`` value:

  1. The DDL + UPDATE are valid SurrealDB v3. A bad statement crashes every prod
     DB at startup on upgrade — the failure mode this guards.
  2. After 032, a stored ``wiki_page_version`` row no longer carries ``branch``.
  3. Replay is a no-op (guarded UPDATE + ``IF EXISTS``).
  4. 032's blast radius is ``wiki_page_version`` alone: ``memory`` and
     ``wiki_page`` are 029's, and their FIELD definitions are untouched here.

Contrast ``test_migration_029_drop_branch_column_e2e.py``, whose subject genuinely
IS the field definition (004 defined ``branch`` on both of its tables).

Placement: ``yadgar/tests/e2e/`` — collected by ``make e2e`` via @pytest.mark.e2e.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.e2e

_BRANCH_SCOPED_VALUE = "feat/some-car"


def _field_names(storage, table: str) -> set[str]:
    """FIELD definition names on *table*, from INFO FOR TABLE."""
    info = storage._q(f"INFO FOR TABLE {table}")
    if not info:
        return set()
    row = info[0] if isinstance(info, list) else info
    fields = row.get("fields", {}) if isinstance(row, dict) else {}
    return set(fields.keys())


def _seed_version_row(storage, vid: int, branch: str | None) -> None:
    """Write a wiki_page_version row carrying *branch*, as the pre-032 writers did."""
    storage._q("DEFINE TABLE IF NOT EXISTS wiki_page_version SCHEMALESS;")
    storage._q(
        "CREATE type::record('wiki_page_version', $id) SET "
        "page_id = $page_id, version = 1, title = 'seed', content = 'seed', "
        "branch = $branch, created_at = '2026-08-12T00:00:00+00:00'",
        {"id": vid, "page_id": vid * 10, "branch": branch},
    )


def _stored_branch(storage, vid: int):
    """Return the row's ``branch`` value, or the sentinel ``"<absent>"`` when unset."""
    rows = storage._q(f"SELECT * FROM wiki_page_version:{vid}")
    assert rows, f"wiki_page_version:{vid} was not written"
    row = rows[0]
    return row.get("branch", "<absent>")


def test_migration_032_removes_the_stored_branch_value(e2e_engines):
    """THE discriminating assertion — a no-op 032 body fails here, not on INFO FOR TABLE."""
    from yadgar._shared.storage.migrations import (
        _migration_032_drop_wiki_page_version_branch,
    )

    storage = e2e_engines["storage"]

    _seed_version_row(storage, 9032001, _BRANCH_SCOPED_VALUE)
    assert _stored_branch(storage, 9032001) == _BRANCH_SCOPED_VALUE, (
        "precondition: the seeded row must really carry a branch value"
    )

    _migration_032_drop_wiki_page_version_branch(storage)

    assert _stored_branch(storage, 9032001) in (None, "<absent>"), (
        "migration 032 must clear the stored branch VALUE — the field definition "
        "may never have existed, so REMOVE FIELD alone drops nothing"
    )


def test_migration_032_removes_the_field_definition_when_one_exists(e2e_engines):
    """Symmetry with 029: if a definition was ever created, it goes too."""
    from yadgar._shared.storage.migrations import (
        _migration_032_drop_wiki_page_version_branch,
    )

    storage = e2e_engines["storage"]

    storage._q("DEFINE TABLE IF NOT EXISTS wiki_page_version SCHEMALESS;")
    storage._q("DEFINE FIELD IF NOT EXISTS branch ON TABLE wiki_page_version TYPE option<string>;")
    assert "branch" in _field_names(storage, "wiki_page_version"), "precondition"

    _migration_032_drop_wiki_page_version_branch(storage)

    assert "branch" not in _field_names(storage, "wiki_page_version"), (
        "migration 032 must remove the wiki_page_version.branch field definition"
    )


def test_migration_032_is_idempotent_under_replay(e2e_engines):
    """Neither call may raise, and the second changes nothing."""
    from yadgar._shared.storage.migrations import (
        _migration_032_drop_wiki_page_version_branch,
    )

    storage = e2e_engines["storage"]

    _seed_version_row(storage, 9032002, _BRANCH_SCOPED_VALUE)
    _migration_032_drop_wiki_page_version_branch(storage)
    after_first = _stored_branch(storage, 9032002)

    _migration_032_drop_wiki_page_version_branch(storage)

    assert _stored_branch(storage, 9032002) == after_first
    assert "branch" not in _field_names(storage, "wiki_page_version")


def test_migration_032_preserves_the_rest_of_the_version_row(e2e_engines):
    """The audit trail survives — only the retired concept leaves."""
    from yadgar._shared.storage.migrations import (
        _migration_032_drop_wiki_page_version_branch,
    )

    storage = e2e_engines["storage"]

    _seed_version_row(storage, 9032003, _BRANCH_SCOPED_VALUE)
    _migration_032_drop_wiki_page_version_branch(storage)

    rows = storage._q("SELECT * FROM wiki_page_version:9032003")
    assert rows, "032 must not delete version rows"
    row = rows[0]
    assert row.get("version") == 1
    assert row.get("page_id") == 90320030
    assert row.get("title") == "seed"


def test_migration_032_does_not_touch_memory_or_wiki_page(e2e_engines):
    """029 owns those two tables; 032 must not reach across (the mirror of 029's boundary)."""
    from yadgar._shared.storage.migrations import (
        _migration_004_branch_field,
        _migration_032_drop_wiki_page_version_branch,
    )

    storage = e2e_engines["storage"]

    # Re-establish the pre-029 shape so a stray drop would be visible.
    _migration_004_branch_field(storage)
    assert "branch" in _field_names(storage, "memory"), "precondition"
    assert "branch" in _field_names(storage, "wiki_page"), "precondition"

    _migration_032_drop_wiki_page_version_branch(storage)

    assert "branch" in _field_names(storage, "memory"), (
        "032 must not drop memory.branch — that is 029's blast radius"
    )
    assert "branch" in _field_names(storage, "wiki_page"), (
        "032 must not drop wiki_page.branch — that is 029's blast radius"
    )
