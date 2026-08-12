"""Tests for migration_032 — drop ``branch`` from ``wiki_page_version`` (ADR-0226, C12).

ADR-0215 retired branch scoping but Car 9 kept ``wiki_page_version.branch`` on
purpose, as an "audit-trail snapshot", with a boundary test asserting migration
029 leaves it alone. ADR-0226 revokes that survivor: *"a history table holding a
column the system has otherwise retired is a second source of truth for a concept
that no longer exists."*

**Why this migration has a DATA step and 033 deliberately did not.**
``wiki_page_version`` is SCHEMALESS and migration 013 never issued a
``DEFINE FIELD`` for ``branch`` — the writers simply set it. So the FIELD
DEFINITION this migration removes may never have existed, and a migration whose
whole body is ``REMOVE FIELD IF EXISTS`` would be a **no-op that still passes an
``INFO FOR TABLE`` assertion**. That is exactly 031's failure shape (a filter on
a column it never projected) wearing a different hat. The substance is therefore
the stored VALUES, mirroring 029: count → null → assert nulled → ``REMOVE FIELD``
for symmetry.

``SET branch = NONE`` assigns the literal SurrealDB ``NONE``, which is what
``IS NONE`` / ``!= NONE`` match. Passing a Python ``None`` through a parameter
would store an explicit null and miss — the trap documented on
``_m029_null_branch`` and ``StorageEngine.set_wiki_page_metadata``.

Coverage here is the LOGIC and the ORDER, driven by a ``_FakeStorage`` that
understands the four statement shapes the migration emits. The real DDL, and the
proof that the value actually disappears from a stored row, run against a live
SurrealDB in ``yadgar/tests/e2e/test_migration_032_wiki_page_version_branch_e2e.py``.
"""

from __future__ import annotations

import pytest

from yadgar._shared.storage.migrations import (
    _MIGRATIONS,
    Migration032Abort,
    _migration_032_drop_wiki_page_version_branch,
)

_BRANCH_SCOPED_VALUE = "feat/some-car"


def _ver(vid: int, branch: str | None = None) -> dict:
    return {"id": vid, "page_id": vid * 10, "version": 1, "branch": branch}


class _FakeStorage:
    """In-memory stand-in understanding the statement shapes migration 032 emits.

    Recognises exactly three query shapes — the ``SELECT count()`` probe, the
    nulling ``UPDATE`` and the ``REMOVE FIELD`` DDL — and evaluates the predicate
    in Python. ``nulling_is_a_noop`` simulates the failure the post-nulling assert
    exists to catch: an UPDATE whose predicate matched nothing while rows still
    carry a value.
    """

    def __init__(self, versions=None, *, nulling_is_a_noop: bool = False):
        self.versions: list[dict] = list(versions or [])
        self.statements: list[str] = []
        self._nulling_is_a_noop = nulling_is_a_noop

    def _q(self, surql: str, params: dict | None = None):  # noqa: ARG002
        stmt = surql.strip()
        self.statements.append(stmt)

        if stmt.startswith("SELECT count()"):
            n = sum(1 for v in self.versions if v.get("branch") is not None)
            return [{"n": n}] if n else []

        if stmt.startswith("UPDATE wiki_page_version SET branch = NONE"):
            if self._nulling_is_a_noop:
                return []
            for v in self.versions:
                if v.get("branch") is not None:
                    v["branch"] = None
            return []

        if stmt.startswith("REMOVE FIELD"):
            return []

        raise AssertionError(f"_FakeStorage saw an unrecognised statement: {stmt!r}")


def _corpus() -> _FakeStorage:
    return _FakeStorage(
        versions=[
            _ver(1, branch=_BRANCH_SCOPED_VALUE),
            _ver(2, branch="master"),
            _ver(3, branch=None),
        ]
    )


class TestMigration032Registration:
    """032 is registered, callable, and ordered between 031 and 033."""

    def test_in_migrations_list(self) -> None:
        versions = [m["version"] for m in _MIGRATIONS]
        assert "032_drop_wiki_page_version_branch" in versions

    def test_maps_to_callable(self) -> None:
        entry = next(m for m in _MIGRATIONS if m["version"] == "032_drop_wiki_page_version_branch")
        assert callable(entry["fn"])
        assert entry["fn"] is _migration_032_drop_wiki_page_version_branch

    def test_ordered_between_031_and_033(self) -> None:
        """List order must match version order — the registry is applied in list order."""
        versions = [m["version"] for m in _MIGRATIONS]
        i032 = versions.index("032_drop_wiki_page_version_branch")
        assert i032 > versions.index("031_project_id_backfill")
        assert i032 < versions.index("033_project_id_other_tables")


class TestMigration032NullsTheValues:
    """The VALUES are the substance — the FIELD DEFINITION may never have existed."""

    def test_nulls_every_branch_value(self) -> None:
        s = _corpus()
        _migration_032_drop_wiki_page_version_branch(s)
        assert all(v["branch"] is None for v in s.versions)

    def test_issues_the_nulling_update(self) -> None:
        s = _corpus()
        _migration_032_drop_wiki_page_version_branch(s)
        updates = [x for x in s.statements if x.startswith("UPDATE ")]
        assert updates, "no nulling UPDATE issued — the migration would be a no-op"
        assert all("wiki_page_version" in x for x in updates)

    def test_nulling_uses_literal_none_not_a_parameter(self) -> None:
        """A Python ``None`` param stores an explicit null, which ``!= NONE`` still matches."""
        s = _corpus()
        _migration_032_drop_wiki_page_version_branch(s)
        update = next(x for x in s.statements if x.startswith("UPDATE "))
        assert "SET branch = NONE" in update
        assert "$branch" not in update

    def test_skips_the_update_when_no_row_carries_a_value(self) -> None:
        s = _FakeStorage(versions=[_ver(1), _ver(2)])
        _migration_032_drop_wiki_page_version_branch(s)
        assert not [x for x in s.statements if x.startswith("UPDATE ")]


class TestMigration032Order:
    """Reversed, the nulling UPDATE would run against an undefined column."""

    def test_drop_happens_after_the_nulling(self) -> None:
        s = _corpus()
        _migration_032_drop_wiki_page_version_branch(s)
        first_drop = next(i for i, x in enumerate(s.statements) if "REMOVE FIELD" in x)
        last_update = max(i for i, x in enumerate(s.statements) if x.startswith("UPDATE "))
        assert first_drop > last_update

    def test_drop_is_guarded_by_if_exists(self) -> None:
        """013 never DEFINE FIELD-ed ``branch``, so the definition may not exist."""
        s = _corpus()
        _migration_032_drop_wiki_page_version_branch(s)
        drops = [x for x in s.statements if "REMOVE FIELD" in x]
        assert drops and all("IF EXISTS" in x for x in drops)
        assert all("wiki_page_version" in x for x in drops)

    def test_does_not_touch_memory_or_wiki_page(self) -> None:
        """032's blast radius is ``wiki_page_version`` alone — 029 owned the other two."""
        s = _corpus()
        _migration_032_drop_wiki_page_version_branch(s)
        for stmt in s.statements:
            assert " TABLE memory" not in stmt
            assert " TABLE wiki_page;" not in stmt


class TestMigration032Aborts:
    """A safety assert with no test proving it fires is decoration."""

    def test_aborts_when_values_survive_the_nulling(self) -> None:
        s = _FakeStorage(versions=[_ver(1, branch=_BRANCH_SCOPED_VALUE)], nulling_is_a_noop=True)
        with pytest.raises(Migration032Abort):
            _migration_032_drop_wiki_page_version_branch(s)

    def test_does_not_drop_the_field_when_it_aborts(self) -> None:
        s = _FakeStorage(versions=[_ver(1, branch=_BRANCH_SCOPED_VALUE)], nulling_is_a_noop=True)
        with pytest.raises(Migration032Abort):
            _migration_032_drop_wiki_page_version_branch(s)
        assert not [x for x in s.statements if "REMOVE FIELD" in x]


class TestMigration032Idempotency:
    """Replay changes nothing — every statement is re-issuable."""

    def test_rerun_is_idempotent(self) -> None:
        s = _corpus()
        _migration_032_drop_wiki_page_version_branch(s)
        snapshot = [dict(v) for v in s.versions]
        _migration_032_drop_wiki_page_version_branch(s)
        assert s.versions == snapshot

    def test_rerun_issues_no_second_update(self) -> None:
        """After the first pass no row carries a value, so the guarded UPDATE is skipped."""
        s = _corpus()
        _migration_032_drop_wiki_page_version_branch(s)
        before = len([x for x in s.statements if x.startswith("UPDATE ")])
        _migration_032_drop_wiki_page_version_branch(s)
        after = len([x for x in s.statements if x.startswith("UPDATE ")])
        assert after == before

    def test_empty_corpus_is_a_noop_not_an_error(self) -> None:
        s = _FakeStorage(versions=[])
        _migration_032_drop_wiki_page_version_branch(s)
        assert [x for x in s.statements if "REMOVE FIELD" in x]
