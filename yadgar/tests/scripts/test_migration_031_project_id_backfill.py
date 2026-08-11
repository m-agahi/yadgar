"""Tests for migration_031 — add ``project_id`` + ``legacy_directory`` to wiki_page + memory.

Car L (0047 §7 D32 ①) shipped this as a one-shot offline backfill that derived
a per-row ``project_id`` from ``directory_context``. **C4 removed the backfill
and C5 removed the classifier it called** (ADR-0227): the migration executes
inside a container with no git binary and no host project mounts, so every
derived value it could produce would be manufactured. What 031 does now is
declare the two columns plus their indexes and touch no row; the real backfill
is an operator-invoked path carrying a host-resolved value (C6 owns it).

Coverage here is therefore the SCHEMA statements and the derives-nothing
contract, driven by an in-memory fake storage that records every ``_q`` call.
There is no classifier seam to stub any more — its absence is itself asserted
(``test_the_classifier_seam_no_longer_exists``).

Idempotency still matters: re-running after the live write paths stamp
``project_id`` MUST be a no-op.
"""

from __future__ import annotations

from typing import Any

from yadgar._shared.storage.migrations import (
    _MIGRATIONS,
    _migration_031_project_id_backfill,
)


class _FakeStorage:
    """In-memory storage double — records every ``_q`` call + holds rows.

    Two tables: ``wiki_page`` and ``memory``. ``SELECT id, directory_context FROM <table>``
    returns the current rows; ``UPDATE type::record(...) SET ...`` mutates
    in place. Records every other ``_q`` call so assertions can inspect
    the schema statements (DEFINE FIELD, DEFINE INDEX).
    """

    def __init__(
        self,
        wiki_rows: list[dict] | None = None,
        mem_rows: list[dict] | None = None,
    ) -> None:
        self.wiki_rows: list[dict] = list(wiki_rows or [])
        self.mem_rows: list[dict] = list(mem_rows or [])
        self.statements: list[tuple[str, str]] = []  # (kind, sql)

    def _q(self, surql: str, params: dict | None = None) -> list[dict]:  # noqa: ARG002
        sql = surql.strip()
        self.statements.append(("q", sql))
        upper = sql.upper()
        params = params or {}

        # DEFINE FIELD / INDEX — record-and-noop.
        if upper.startswith("DEFINE "):
            return []

        # SELECT id, directory_context FROM <table>
        if "FROM wiki_page" in sql and "directory_context" in sql and "SELECT" in upper:
            return [dict(r) for r in self.wiki_rows]
        if "FROM memory" in sql and "directory_context" in sql and "SELECT" in upper:
            return [dict(r) for r in self.mem_rows]

        # UPDATE type::record('<table>', $id) SET project_id = $project_id
        # — apply the patch to the in-memory mirror so postcondition tests
        # can read back the stamped value.
        if upper.startswith("UPDATE"):
            table = "wiki_page" if "WIKI_PAGE" in upper else "memory" if "MEMORY" in upper else None
            target_id = params.get("id")
            rows = (
                self.wiki_rows
                if table == "wiki_page"
                else self.mem_rows
                if table == "memory"
                else []
            )
            for r in rows:
                if r.get("id") == target_id:
                    if "project_id" in params:
                        r["project_id"] = params["project_id"]
                    if "legacy_directory" in params:
                        r["legacy_directory"] = params["legacy_directory"]
                    break
            return []

        return []

    def _extract_id(self, raw: Any) -> int | None:
        if raw is None:
            return None
        if isinstance(raw, str) and ":" in raw:
            return int(raw.rsplit(":", 1)[1])
        return int(raw)

    # Mutation helpers — the migration uses _q() for UPDATE so the fake
    # needs an in-memory mirror. Surface the per-row state via ``get_rows``.
    def get_wiki(self) -> list[dict]:
        return [dict(r) for r in self.wiki_rows]

    def get_mem(self) -> list[dict]:
        return [dict(r) for r in self.mem_rows]


class TestMigration031Registration:
    """Migration 031 is registered in _MIGRATIONS list."""

    def test_in_migrations_list(self) -> None:
        versions = [m["version"] for m in _MIGRATIONS]
        assert "031_project_id_backfill" in versions

    def test_maps_to_callable(self) -> None:
        entry = next(m for m in _MIGRATIONS if m["version"] == "031_project_id_backfill")
        assert callable(entry["fn"])
        assert entry["fn"] is _migration_031_project_id_backfill

    def test_registered_after_030(self) -> None:
        versions = [m["version"] for m in _MIGRATIONS]
        assert versions.index("031_project_id_backfill") > versions.index(
            "030_wiki_mutability_override"
        )


class TestMigration031SchemaStatements:
    """The migration issues DEFINE FIELD/INDEX for project_id + legacy_directory."""

    def test_defines_project_id_on_wiki_page(self) -> None:
        storage = _FakeStorage()
        _migration_031_project_id_backfill(storage)
        schema_stmts = [
            sql
            for kind, sql in storage.statements
            if kind == "q" and "DEFINE FIELD" in sql and "project_id" in sql
        ]
        assert any("wiki_page" in s for s in schema_stmts)

    def test_defines_legacy_directory_on_wiki_page(self) -> None:
        storage = _FakeStorage()
        _migration_031_project_id_backfill(storage)
        schema_stmts = [
            sql
            for kind, sql in storage.statements
            if kind == "q" and "DEFINE FIELD" in sql and "legacy_directory" in sql
        ]
        assert any("wiki_page" in s for s in schema_stmts)

    def test_defines_project_id_on_memory(self) -> None:
        storage = _FakeStorage()
        _migration_031_project_id_backfill(storage)
        schema_stmts = [
            sql
            for kind, sql in storage.statements
            if kind == "q" and "DEFINE FIELD" in sql and "project_id" in sql
        ]
        assert any("memory" in s for s in schema_stmts)

    def test_creates_project_id_index(self) -> None:
        storage = _FakeStorage()
        _migration_031_project_id_backfill(storage)
        index_stmts = [
            sql
            for kind, sql in storage.statements
            if kind == "q" and "DEFINE INDEX" in sql and "project_id" in sql
        ]
        assert len(index_stmts) >= 2  # one for wiki_page, one for memory


class TestMigration031DerivesNothing:
    """C4 (0047 PR#40 §5): 031 declares columns and stops. **No backfill.**

    CONTRACT FLIP. These tests replace ``TestMigration031BackfillPostconditions``
    + ``TestMigration031Idempotency``, which asserted the per-row classification
    Car L shipped: ``owner/repo`` for a git remote, ``local/<basename>`` for a
    path with none, ``'unresolved'`` + ``legacy_directory`` for a path that no
    longer exists. Every one of those outcomes came from
    ``derive_project_id`` running INSIDE the container — which installs no git
    and mounts no host project directory, so in production the classifier could
    only ever return ``local/<basename>``, silently and always, on every row of
    the corpus (ADR-0227). The old tests passed because the classifier was
    mocked; the mock was the only reason the behaviour looked correct.

    ADR-0227: "Migration 031's in-migration backfill cannot stand […] the
    backfill moves to an operator-invoked path with the host-resolved value."
    C6 owns that op. What 031 must still do — and what these tests pin — is
    declare the columns and their indexes, and touch no row.
    """

    def _run(self, wiki_rows: list[dict], mem_rows: list[dict]) -> _FakeStorage:
        storage = _FakeStorage(wiki_rows=wiki_rows, mem_rows=mem_rows)
        _migration_031_project_id_backfill(storage)
        return storage

    def test_no_row_is_updated(self) -> None:
        storage = self._run(
            wiki_rows=[{"id": 1, "slug": "doc-a", "directory_context": "/home/max/git/yadgar"}],
            mem_rows=[{"id": 10, "directory_context": "/tmp/old-deleted-proj"}],
        )
        updates = [
            sql for kind, sql in storage.statements if kind == "q" and "UPDATE" in sql.upper()
        ]
        assert updates == [], f"031 still writes rows: {updates}"

    def test_rows_keep_their_pre_migration_state(self) -> None:
        storage = self._run(
            wiki_rows=[{"id": 1, "slug": "doc-a", "directory_context": "/home/max/git/yadgar"}],
            mem_rows=[],
        )
        row = storage.get_wiki()[0]
        assert "project_id" not in row, "031 stamped a project_id it cannot derive"
        assert "legacy_directory" not in row

    def test_sentinel_rows_are_not_stamped_global(self) -> None:
        """The ``'global'`` stamp was a mint too — §1.4 deletes that sentinel."""
        storage = self._run(
            wiki_rows=[
                {"id": 4, "slug": "doc-d", "directory_context": "global"},
                {"id": 5, "slug": "doc-e", "directory_context": ""},
            ],
            mem_rows=[],
        )
        for row in storage.get_wiki():
            assert row.get("project_id") is None

    def test_the_classifier_seam_no_longer_exists(self) -> None:
        """Not merely uncalled — absent. C5 deleted the seam, not just its callers.

        C4 asserted this by patching ``_classify_directory_for_migration`` with a
        function that raised if reached. C5 (ADR-0227) deleted the classifier and
        that seam along with it, so the patch target itself is gone — which makes
        the old test error on ``AttributeError`` from ``patch`` rather than pass.
        Re-pointed rather than deleted, and strictly stronger: a mocked seam only
        proves *this* migration did not call it, while an absent one proves no
        future edit can. A migration runs inside a container with no git binary
        and no host project mounts, so a re-introduced classifier could only ever
        return a manufactured key for every row of the corpus.
        """
        from yadgar._shared.storage import migrations as _m

        assert not hasattr(_m, "_classify_directory_for_migration")
        assert not hasattr(_m, "derive_project_id")

    def test_the_migration_still_runs_without_any_classifier(self) -> None:
        """The absence is not merely tolerated — 031 completes and stamps nothing."""
        storage = self._run(
            wiki_rows=[{"id": 1, "directory_context": "/home/max/git/yadgar"}],
            mem_rows=[{"id": 2, "directory_context": "/tmp/gone"}],
        )
        assert all(r.get("project_id") is None for r in storage.get_wiki())
        assert all(r.get("project_id") is None for r in storage.get_mem())

    def test_no_row_read_is_issued_either(self) -> None:
        """A migration that derives nothing has no reason to scan the corpus."""
        storage = self._run(
            wiki_rows=[{"id": 1, "directory_context": "/home/max/git/yadgar"}],
            mem_rows=[],
        )
        selects = [
            sql for kind, sql in storage.statements if kind == "q" and "SELECT" in sql.upper()
        ]
        assert selects == [], f"031 still scans rows for a backfill it no longer does: {selects}"

    def test_second_run_is_a_no_op(self) -> None:
        storage = _FakeStorage(
            wiki_rows=[{"id": 1, "slug": "doc-a", "directory_context": "/home/max/git/yadgar"}],
            mem_rows=[],
        )
        _migration_031_project_id_backfill(storage)
        first = list(storage.statements)
        _migration_031_project_id_backfill(storage)
        second = storage.statements[len(first) :]
        assert [sql for _k, sql in second] == [sql for _k, sql in first]
        assert storage.get_wiki()[0].get("project_id") is None
