"""Tests for migration 033 — ``project_id`` on the other directory-bearing tables.

C11 (0047 PR#40 §5). 033 is the schema half of the car: it declares
``project_id`` plus an index on every table that carries a ``directory`` or
``directory_context`` scoping value and that migration 031 did NOT reach (031
covered ``wiki_page`` and ``memory`` only).

**The idempotency proof here is STRUCTURAL, not behavioural.** 031's phase
filter was dead code because it filtered on a column it never projected — a
defect that only exists when a migration touches rows. 033 issues nothing but
``DEFINE FIELD IF NOT EXISTS`` / ``DEFINE INDEX IF NOT EXISTS``, so the tests
below assert the *absence* of any row-touching statement as well as the
replay-equality, which together mean the 031 shape cannot recur here.

Driven by an in-memory fake storage recording every ``_q`` call, mirroring
``test_migration_031_project_id_backfill.py``.
"""

from __future__ import annotations

import pytest

from yadgar._shared.storage.migrations import (
    _C11_PROJECT_ID_TABLES,
    _MIGRATIONS,
    _migration_033_project_id_other_tables,
)

#: The tables C11 owns, restated INDEPENDENTLY of the production tuple. Asserting
#: ``set(_C11_PROJECT_ID_TABLES) == set(_C11_PROJECT_ID_TABLES)`` would be
#: vacuous (ADR-0080); this list is the test's own expectation, so silently
#: dropping a table from the migration fails here.
_EXPECTED_TABLES = {
    # legacy ``directory`` COLUMN owners
    "memory_block",
    "episode",
    "action_log",
    "runtime_config",
    # SCHEMALESS ``directory_context`` users no DEFINE FIELD ever covered
    "checkpoint",
    "narrative_entry",
    "user_profile",
    "derived_belief",
    "wiki_page_version",
}


class _FakeStorage:
    """Records every ``_q`` call. 033 must never need a return value."""

    def __init__(self) -> None:
        self.statements: list[str] = []

    def _q(self, surql: str, params: dict | None = None) -> list[dict]:  # noqa: ARG002
        self.statements.append(surql.strip())
        return []


def _run() -> _FakeStorage:
    storage = _FakeStorage()
    _migration_033_project_id_other_tables(storage)
    return storage


class TestMigration033Registration:
    """033 is registered, callable, and ordered after 031."""

    def test_in_migrations_list(self) -> None:
        versions = [m["version"] for m in _MIGRATIONS]
        assert "033_project_id_other_tables" in versions

    def test_maps_to_callable(self) -> None:
        entry = next(m for m in _MIGRATIONS if m["version"] == "033_project_id_other_tables")
        assert callable(entry["fn"])
        assert entry["fn"] is _migration_033_project_id_other_tables

    def test_registered_after_031(self) -> None:
        versions = [m["version"] for m in _MIGRATIONS]
        assert versions.index("033_project_id_other_tables") > versions.index(
            "031_project_id_backfill"
        )

    def test_032_is_owned_by_c12_and_ordered_before_033(self) -> None:
        """C12 owns 032 (drop ``wiki_page_version.branch``). 033 must not claim it.

        RE-POINTED by C12, exactly as the pre-landing form instructed: this used to
        assert no ``032*`` was registered, because 033 landed first and deliberately
        skipped the number. C12 has now landed, so the reservation is FILLED — and
        the assertion still earns its place by pinning that 033 did not renumber
        onto 032 and that list order still matches version order.
        """
        versions = [m["version"] for m in _MIGRATIONS]
        taken = [v for v in versions if v.startswith("032")]
        assert taken == ["032_drop_wiki_page_version_branch"], (
            f"032 belongs to C12's wiki_page_version.branch drop; found {taken!r}"
        )
        assert versions.index("032_drop_wiki_page_version_branch") < versions.index(
            "033_project_id_other_tables"
        )


class TestMigration033Tables:
    """The declared table set is exactly C11's, and it is not empty."""

    def test_table_tuple_matches_the_independent_expectation(self) -> None:
        assert set(_C11_PROJECT_ID_TABLES) == _EXPECTED_TABLES

    def test_table_tuple_has_no_duplicates(self) -> None:
        assert len(_C11_PROJECT_ID_TABLES) == len(set(_C11_PROJECT_ID_TABLES))

    def test_queue_is_not_a_table(self) -> None:
        """The plan's fourth table does not exist — see the migration docstring.

        ``_shared/storage/queue.py`` is the action_log + file_hash MIXIN module;
        the queue itself is file-backed (``_shared/file_queue/``). There is no
        ``DEFINE TABLE queue``, no writer and no row, so a ``project_id`` column
        on it would be a phantom declared to satisfy a table count.
        """
        assert "queue" not in _C11_PROJECT_ID_TABLES

    @pytest.mark.parametrize("table", sorted(_EXPECTED_TABLES))
    def test_defines_project_id_on_every_table(self, table: str) -> None:
        stmts = _run().statements
        assert any(
            "DEFINE FIELD" in s and "project_id" in s and f"ON TABLE {table} " in s for s in stmts
        ), f"no DEFINE FIELD project_id for {table}"

    @pytest.mark.parametrize("table", sorted(_EXPECTED_TABLES))
    def test_defines_an_index_on_every_table(self, table: str) -> None:
        stmts = _run().statements
        assert any(
            "DEFINE INDEX" in s and "project_id" in s and f"ON TABLE {table} " in s for s in stmts
        ), f"no DEFINE INDEX on project_id for {table}"

    def test_the_column_is_nullable(self) -> None:
        """``option<string>`` — a pre-existing row reads as None, never 'global'.

        A non-optional type would make the DEFINE fail on a populated table, and
        a defaulted one would mint the sentinel ADR-0227 deletes.
        """
        field_stmts = [s for s in _run().statements if "DEFINE FIELD" in s]
        assert field_stmts
        for s in field_stmts:
            assert "TYPE option<string>" in s, s


class TestMigration033IsSchemaOnly:
    """No data step — the structural reason the 031 defect cannot recur."""

    def test_issues_no_select(self) -> None:
        offenders = [s for s in _run().statements if "SELECT" in s.upper()]
        assert offenders == [], f"033 scans rows: {offenders}"

    def test_issues_no_row_write(self) -> None:
        offenders = [
            s
            for s in _run().statements
            if any(kw in s.upper() for kw in ("UPDATE ", "CREATE ", "DELETE ", "INSERT "))
        ]
        assert offenders == [], f"033 writes rows: {offenders}"

    def test_every_statement_is_a_define(self) -> None:
        stmts = _run().statements
        assert stmts, "033 issued no statements at all (ADR-0080 anti-vacuity)"
        for s in stmts:
            assert s.upper().startswith("DEFINE "), s

    def test_every_statement_is_if_not_exists(self) -> None:
        """The idempotency mechanism, asserted per statement rather than inferred."""
        for s in _run().statements:
            assert "IF NOT EXISTS" in s, s

    def test_removes_no_field(self) -> None:
        """033 ADDS. The legacy columns are dropped by the NEXT PR, not here.

        Dropping them now would strand the C6-shaped backfill that has to derive
        ``project_id`` from them, and would break three live column consumers
        (``causal_discovery/pc.py``, ``consolidation/cls.py``,
        ``consolidation/cleanup.py``).
        """
        offenders = [s for s in _run().statements if "REMOVE" in s.upper()]
        assert offenders == [], offenders


class TestMigration033Idempotency:
    """Replay proof: a second run issues the identical statement list."""

    def test_second_run_issues_the_same_statements(self) -> None:
        storage = _FakeStorage()
        _migration_033_project_id_other_tables(storage)
        first = list(storage.statements)
        _migration_033_project_id_other_tables(storage)
        second = storage.statements[len(first) :]
        assert second == first

    def test_replay_still_touches_no_row(self) -> None:
        storage = _FakeStorage()
        for _ in range(3):
            _migration_033_project_id_other_tables(storage)
        assert all(s.upper().startswith("DEFINE ") for s in storage.statements)

    def test_statement_count_is_two_per_table(self) -> None:
        """One field + one index each — an anti-vacuity floor on the replay test."""
        stmts = _run().statements
        assert len(stmts) == 2 * len(_C11_PROJECT_ID_TABLES)
