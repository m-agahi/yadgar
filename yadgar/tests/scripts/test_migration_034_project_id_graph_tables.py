"""Car 1 — migration 034 declares ``project_id`` on the three GRAPH tables.

033 (C11) covered every table that carries a legacy ``directory`` /
``directory_context`` column. ``entity``, ``relationship`` and
``memory_cluster`` carry NEITHER — they are derived-graph tables whose owner
is inherited from the rows that produced them — so C11's directory-bearing
criterion never reached them and they were left with no ``project_id``
declaration at all.

They are SCHEMALESS, so ``stamp_project_id``'s ``UPDATE`` would create the
column untyped and the op would appear to work. That is precisely the shape
033's own docstring warns about: an untyped column is invisible to
``INFO FOR TABLE`` review and gets no index, so the scope predicate every
reader is about to move onto would table-scan 5,560 rows.

Schema statements ONLY — no data step. The backfill is the operator-invoked
op, deliberately separate (ADR-0227: migrations run in a container with no
git and no host mounts, which is why 031's in-migration backfill could not
stand).
"""

from __future__ import annotations

from yadgar._shared.storage.migrations import (
    _CAR1_GRAPH_PROJECT_ID_TABLES,
    _MIGRATIONS,
    _migration_034_project_id_graph_tables,
)

#: Restated INDEPENDENTLY of the production tuple — asserting the tuple equals
#: itself would be vacuous (ADR-0080).
_EXPECTED_TABLES = {"entity", "relationship", "memory_cluster"}


class _FakeStorage:
    """Records every ``_q`` call. 034 must never need a return value."""

    def __init__(self) -> None:
        self.statements: list[str] = []

    def _q(self, surql: str, params: dict | None = None) -> list[dict]:  # noqa: ARG002
        self.statements.append(surql.strip())
        return []


def _run() -> _FakeStorage:
    storage = _FakeStorage()
    _migration_034_project_id_graph_tables(storage)
    return storage


class TestRegistration:
    def test_in_migrations_list_after_033(self) -> None:
        versions = [m["version"] for m in _MIGRATIONS]
        assert "034_project_id_graph_tables" in versions
        assert versions.index("034_project_id_graph_tables") > versions.index(
            "033_project_id_other_tables"
        )

    def test_maps_to_the_callable(self) -> None:
        entry = next(m for m in _MIGRATIONS if m["version"] == "034_project_id_graph_tables")
        assert entry["fn"] is _migration_034_project_id_graph_tables


class TestTables:
    def test_table_tuple_matches_the_independent_expectation(self) -> None:
        assert set(_CAR1_GRAPH_PROJECT_ID_TABLES) == _EXPECTED_TABLES

    def test_does_not_redeclare_what_033_already_owns(self) -> None:
        """A second DEFINE FIELD on the same table is noise, not safety."""
        from yadgar._shared.storage.migrations import _C11_PROJECT_ID_TABLES

        assert not set(_CAR1_GRAPH_PROJECT_ID_TABLES) & set(_C11_PROJECT_ID_TABLES)


class TestStatements:
    def test_declares_an_optional_field_and_an_index_per_table(self) -> None:
        statements = _run().statements
        for table in _CAR1_GRAPH_PROJECT_ID_TABLES:
            assert any(
                f"DEFINE FIELD IF NOT EXISTS project_id ON TABLE {table} TYPE option<string>" in s
                for s in statements
            )
            assert any(
                f"DEFINE INDEX IF NOT EXISTS {table}_project_id_idx ON TABLE {table} "
                f"FIELDS project_id" in s
                for s in statements
            )

    def test_every_statement_is_a_schema_statement(self) -> None:
        """No row-touching statement exists to get wrong — 031's lesson."""
        for statement in _run().statements:
            assert statement.upper().startswith("DEFINE ")

    def test_replay_issues_an_identical_list(self) -> None:
        assert _run().statements == _run().statements

    def test_field_is_optional_not_required(self) -> None:
        """Pre-existing rows must read as ``None``, never as ``'global'``."""
        for statement in _run().statements:
            if "DEFINE FIELD" in statement:
                assert "option<string>" in statement
                assert "ASSERT" not in statement.upper()
