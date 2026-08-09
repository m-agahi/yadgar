"""Tests for migration_029 — retire branch scoping: null the data, drop the column.

ADR-0215, Car 9. The migration performs the data steps and the structural drop in
one ordered unit:

  1/2. DELETE unprotected branch-scoped memories, assert the protected ones survived
  3.   resolve the one reviewed ``(slug, directory_context)`` collision, BY ID
  4/5. null every remaining ``branch`` value on wiki_page + memory
  -.   assert both tables hold exactly one branch group — before the drop
  6.   ``REMOVE FIELD IF EXISTS branch`` on both tables

Coverage here is the LOGIC and the ABORT paths, driven by ``_FakeStorage`` (an
in-memory stand-in that understands the seven statement shapes the migration
emits). The real DDL runs against a live SurrealDB in
``yadgar/tests/e2e/test_migration_029_drop_branch_column_e2e.py``.

Why the abort tests matter more than the happy path: a safety assert with no test
proving it fires is decoration. Each ``Migration029Abort`` case below corresponds
to a way the DELETE could go wrong against the live corpus.

Also asserted: no live code path can still WRITE the column. The tables are
SCHEMALESS, so ``REMOVE FIELD`` drops only the type definition — a surviving
writer would silently re-create ``branch`` as an untyped field on rows this
migration just nulled, and ``INFO FOR TABLE`` would still look clean.
"""

from __future__ import annotations

import pytest

from yadgar._shared.storage.migrations import (
    _M029_COLLISION_DROP_ID,
    _M029_COLLISION_KEEP_ID,
    _M029_COLLISION_SLUG,
    _M029_DELETE_CEILING,
    _MIGRATIONS,
    Migration029Abort,
    _migration_029_drop_branch_column,
)

_BRANCH_SCOPED_VALUE = "feat/some-car"


def _mem(mid: int, branch: str | None, is_protected: bool = False) -> dict:
    return {"id": mid, "branch": branch, "is_protected": is_protected}


def _page(pid: int, slug: str, directory: str = "/proj", branch: str | None = None) -> dict:
    return {"id": pid, "slug": slug, "directory_context": directory, "branch": branch}


class _FakeStorage:
    """In-memory stand-in understanding the statement shapes migration 029 emits.

    Not a general SurrealDB emulator — it recognises exactly the queries the
    migration issues and evaluates their predicates in Python. ``over_broad_delete``
    simulates the catastrophe the post-DELETE assert exists to catch: a DELETE
    whose predicate wrongly matched protected rows too.
    """

    def __init__(self, memories=None, pages=None, *, over_broad_delete: bool = False):
        self.memories: list[dict] = list(memories or [])
        self.pages: list[dict] = list(pages or [])
        self.statements: list[str] = []
        self._over_broad_delete = over_broad_delete

    # -- helpers the migration calls directly -------------------------------
    @staticmethod
    def _extract_id(record_id):
        if isinstance(record_id, str) and ":" in record_id:
            return int(record_id.split(":")[1])
        return int(record_id)

    # -- predicate evaluation ------------------------------------------------
    @staticmethod
    def _is_branch_scoped(row: dict) -> bool:
        b = row.get("branch")
        return b is not None and b not in ("master", "main")

    def _rows_for(self, table: str) -> list[dict]:
        return self.memories if table == "memory" else self.pages

    def _matches(self, row: dict, where: str) -> bool:
        if "branch != 'master'" in where:
            if not self._is_branch_scoped(row):
                return False
            if "is_protected = false" in where:
                return not row.get("is_protected", False)
            if "is_protected = true" in where:
                return bool(row.get("is_protected", False))
            return True
        # bare "branch != NONE"
        return row.get("branch") is not None

    # -- the query surface ---------------------------------------------------
    def _q(self, surql: str, params: dict | None = None):
        self.statements.append(surql)

        if surql.startswith("SELECT count() AS n FROM "):
            table = surql.split()[5]
            where = surql.split(" WHERE ", 1)[1].rsplit(" GROUP ALL", 1)[0]
            n = sum(1 for r in self._rows_for(table) if self._matches(r, where))
            return [{"n": n}] if n else []

        if surql.startswith("SELECT slug, directory_context, count() AS n"):
            groups: dict[tuple, int] = {}
            for p in self.pages:
                groups[(p["slug"], p["directory_context"])] = (
                    groups.get((p["slug"], p["directory_context"]), 0) + 1
                )
            return [
                {"slug": s, "directory_context": d, "n": n} for (s, d), n in groups.items() if n
            ]

        if surql.startswith("SELECT id FROM wiki_page WHERE slug"):
            slug = (params or {}).get("s")
            return [{"id": p["id"]} for p in self.pages if p["slug"] == slug]

        if surql.startswith("DELETE memory WHERE "):
            where = surql.split(" WHERE ", 1)[1]
            if self._over_broad_delete:
                self.memories = [m for m in self.memories if not self._is_branch_scoped(m)]
            else:
                self.memories = [m for m in self.memories if not self._matches(m, where)]
            return []

        if surql.startswith("DELETE type::record('wiki_page'"):
            drop = (params or {}).get("id")
            self.pages = [p for p in self.pages if p["id"] != drop]
            return []

        if surql.startswith("UPDATE ") and "SET branch = NONE" in surql:
            table = surql.split()[1]
            for r in self._rows_for(table):
                r["branch"] = None
            return []

        if surql.startswith("REMOVE FIELD"):
            return []

        raise AssertionError(f"_FakeStorage saw an unrecognised statement: {surql!r}")


def _healthy_corpus(**kwargs) -> _FakeStorage:
    """All four (is_protected, tier) buckets plus canonical + master rows."""
    return _FakeStorage(
        memories=[
            _mem(1, _BRANCH_SCOPED_VALUE, is_protected=False),  # delete
            _mem(2, _BRANCH_SCOPED_VALUE, is_protected=False),  # delete
            _mem(3, _BRANCH_SCOPED_VALUE, is_protected=True),  # keep, null
            _mem(4, _BRANCH_SCOPED_VALUE, is_protected=True),  # keep, null
            _mem(5, "master", is_protected=False),  # keep, null
            _mem(6, None, is_protected=False),  # untouched
        ],
        pages=[
            _page(10, "durable-branch-page", branch=_BRANCH_SCOPED_VALUE),
            _page(11, "master-page", branch="master"),
            _page(12, "canonical-page", branch=None),
        ],
        **kwargs,
    )


class TestMigration029Registration:
    def test_migration_029_in_list(self):
        assert "029_drop_branch_column" in [m["version"] for m in _MIGRATIONS]

    def test_migration_029_fn_is_callable(self):
        entry = next(m for m in _MIGRATIONS if m["version"] == "029_drop_branch_column")
        assert callable(entry["fn"])
        assert entry["fn"] is _migration_029_drop_branch_column

    def test_migration_029_is_at_the_tail(self):
        # Car J appended migration 030 (mutability_override) after 029, so 029
        # is no longer at the absolute tail. The assertion below stays correct
        # as long as 029 is the most recent migration registered BEFORE the
        # next car adds one — but to avoid coupling 029's test to that
        # invariant, we only check 029 is in the list (and 030 is the tail).
        assert "029_drop_branch_column" in [m["version"] for m in _MIGRATIONS]
        assert _MIGRATIONS[-1]["version"] != "029_drop_branch_column"

    def test_029_registered_after_004_and_015(self):
        versions = [m["version"] for m in _MIGRATIONS]
        i = versions.index("029_drop_branch_column")
        assert i > versions.index("004_branch_field")
        assert i > versions.index("015_wiki_draft_branch")


class TestMigration029HappyPath:
    def test_deletes_only_unprotected_branch_scoped_memories(self):
        s = _healthy_corpus()
        _migration_029_drop_branch_column(s)
        assert sorted(m["id"] for m in s.memories) == [3, 4, 5, 6], (
            "unprotected branch-scoped memories (1, 2) must go; protected (3, 4), "
            "master (5) and canonical (6) must survive"
        )

    def test_protected_survivors_are_nulled_not_deleted(self):
        s = _healthy_corpus()
        _migration_029_drop_branch_column(s)
        survivors = {m["id"]: m["branch"] for m in s.memories}
        assert survivors[3] is None and survivors[4] is None, (
            "protected branch-scoped memories must survive with branch nulled — "
            "that is what makes them globally reachable"
        )

    def test_branch_scoped_wiki_pages_are_nulled_never_deleted(self):
        s = _healthy_corpus()
        _migration_029_drop_branch_column(s)
        ids = {p["id"] for p in s.pages}
        assert 10 in ids, "branch-scoped wiki pages must NOT be deleted (no is_protected escape)"
        assert all(p["branch"] is None for p in s.pages)

    def test_drops_branch_on_both_tables(self):
        s = _healthy_corpus()
        _migration_029_drop_branch_column(s)
        drops = [x for x in s.statements if "REMOVE FIELD" in x]
        assert any("memory" in x for x in drops), f"no memory drop; issued {drops}"
        assert any("wiki_page" in x for x in drops), f"no wiki_page drop; issued {drops}"

    def test_drops_are_idempotent_if_exists(self):
        s = _healthy_corpus()
        _migration_029_drop_branch_column(s)
        drops = [x for x in s.statements if "REMOVE FIELD" in x]
        assert drops and all("IF EXISTS" in x for x in drops)

    def test_drop_happens_after_the_data_steps(self):
        """Reversed, the nulling UPDATE would run against an undefined column."""
        s = _healthy_corpus()
        _migration_029_drop_branch_column(s)
        first_drop = next(i for i, x in enumerate(s.statements) if "REMOVE FIELD" in x)
        last_update = max(i for i, x in enumerate(s.statements) if x.startswith("UPDATE "))
        last_delete = max(i for i, x in enumerate(s.statements) if x.startswith("DELETE "))
        assert first_drop > last_update
        assert first_drop > last_delete

    def test_does_not_touch_wiki_page_version(self):
        """029's scope is wiki_page + memory. Version rows are an audit trail."""
        s = _healthy_corpus()
        _migration_029_drop_branch_column(s)
        assert not any("wiki_page_version" in x for x in s.statements)

    def test_rerun_is_idempotent(self):
        s = _healthy_corpus()
        _migration_029_drop_branch_column(s)
        snapshot = ([dict(m) for m in s.memories], [dict(p) for p in s.pages])
        _migration_029_drop_branch_column(s)
        assert (s.memories, s.pages) == snapshot

    def test_empty_corpus_is_a_noop_not_an_error(self):
        s = _FakeStorage()
        _migration_029_drop_branch_column(s)
        assert any("REMOVE FIELD" in x for x in s.statements)


class TestMigration029Collision:
    def _colliding(self) -> _FakeStorage:
        return _FakeStorage(
            pages=[
                _page(_M029_COLLISION_KEEP_ID, _M029_COLLISION_SLUG, "/home/max/aws-work"),
                _page(_M029_COLLISION_DROP_ID, _M029_COLLISION_SLUG, "/home/max/aws-work"),
            ]
        )

    def test_deletes_the_loser_by_id_and_keeps_the_winner(self):
        s = self._colliding()
        _migration_029_drop_branch_column(s)
        assert [p["id"] for p in s.pages] == [_M029_COLLISION_KEEP_ID]

    def test_deletes_by_record_id_never_by_slug(self):
        """Both rows share the slug — a slug-keyed delete would remove both."""
        s = self._colliding()
        _migration_029_drop_branch_column(s)
        deletes = [x for x in s.statements if x.startswith("DELETE ")]
        assert deletes == ["DELETE type::record('wiki_page', $id)"], (
            f"collision must be resolved by record id; issued {deletes}"
        )

    def test_already_resolved_collision_converges_silently(self):
        s = _FakeStorage(
            pages=[_page(_M029_COLLISION_KEEP_ID, _M029_COLLISION_SLUG, "/home/max/aws-work")]
        )
        _migration_029_drop_branch_column(s)
        assert [p["id"] for p in s.pages] == [_M029_COLLISION_KEEP_ID]

    def test_drop_id_absent_but_collision_present_still_aborts(self):
        """The 'converged' early-return must not skip the no-collisions assert.

        Reached when the reviewed slug collides between two ids that are neither
        the recorded keep-id nor the recorded drop-id. Returning on the drop-id's
        absence alone would let the collision survive silently — through the one
        step whose entire purpose is not silently skipping.
        """
        s = _FakeStorage(
            pages=[
                _page(_M029_COLLISION_KEEP_ID, _M029_COLLISION_SLUG, "/home/max/aws-work"),
                _page(7777, _M029_COLLISION_SLUG, "/home/max/aws-work"),
            ]
        )
        with pytest.raises(Migration029Abort, match="collisions remain"):
            _migration_029_drop_branch_column(s)
        assert not any("REMOVE FIELD" in x for x in s.statements)

    def test_unreviewed_collision_aborts(self):
        s = _FakeStorage(
            pages=[_page(90, "some-other-slug", "/proj"), _page(91, "some-other-slug", "/proj")]
        )
        with pytest.raises(Migration029Abort, match="unreviewed"):
            _migration_029_drop_branch_column(s)

    def test_missing_keep_row_aborts_rather_than_erasing_the_slug(self):
        s = _FakeStorage(
            pages=[
                _page(_M029_COLLISION_DROP_ID, _M029_COLLISION_SLUG, "/home/max/aws-work"),
                _page(999, _M029_COLLISION_SLUG, "/home/max/aws-work"),
            ]
        )
        with pytest.raises(Migration029Abort, match="row to KEEP"):
            _migration_029_drop_branch_column(s)


class TestMigration029AbortPaths:
    """Each assert must be provable to FIRE, or it is decoration."""

    def test_over_broad_delete_aborts(self):
        """The headline safety property: protected rows vanishing halts the migration."""
        s = _healthy_corpus(over_broad_delete=True)
        with pytest.raises(Migration029Abort, match="over-broad"):
            _migration_029_drop_branch_column(s)

    def test_over_broad_delete_never_reaches_the_column_drop(self):
        s = _healthy_corpus(over_broad_delete=True)
        with pytest.raises(Migration029Abort):
            _migration_029_drop_branch_column(s)
        assert not any("REMOVE FIELD" in x for x in s.statements), (
            "aborting must leave the column in place so the backup can be restored"
        )

    def test_circuit_breaker_aborts_before_any_delete(self):
        """A predicate matching far more than measured is refused, not executed."""
        s = _FakeStorage(
            memories=[
                _mem(i, _BRANCH_SCOPED_VALUE, is_protected=False)
                for i in range(_M029_DELETE_CEILING + 1)
            ]
        )
        with pytest.raises(Migration029Abort, match="circuit breaker"):
            _migration_029_drop_branch_column(s)
        assert not any(x.startswith("DELETE ") for x in s.statements), (
            "the circuit breaker must fire BEFORE the DELETE, not after"
        )
        assert len(s.memories) == _M029_DELETE_CEILING + 1

    def test_circuit_breaker_allows_the_measured_volume(self):
        """87 rows were measured 2026-08-08; the ceiling must not block real data."""
        s = _FakeStorage(
            memories=[_mem(i, _BRANCH_SCOPED_VALUE, is_protected=False) for i in range(87)]
        )
        _migration_029_drop_branch_column(s)
        assert s.memories == []


class TestHistoricalMigrationsUntouched:
    """004 / 015 stay exactly as shipped — replay on a fresh DB depends on them."""

    def test_migration_004_still_defines_branch(self):
        import inspect

        from yadgar._shared.storage.migrations import _migration_004_branch_field

        src = inspect.getsource(_migration_004_branch_field)
        assert "DEFINE FIELD IF NOT EXISTS branch ON TABLE memory" in src
        assert "DEFINE FIELD IF NOT EXISTS branch ON TABLE wiki_page" in src

    def test_migration_015_still_defines_wiki_draft_branch(self):
        import inspect

        from yadgar._shared.storage.migrations import _migration_015_wiki_draft_branch

        src = inspect.getsource(_migration_015_wiki_draft_branch)
        assert "DEFINE FIELD IF NOT EXISTS branch ON TABLE wiki_draft" in src


class TestNoSurvivingColumnWriters:
    """SCHEMALESS tables accept undefined fields — the writers must be gone."""

    def test_wiki_add_options_has_no_branch_field(self):
        from yadgar._shared.wiki.contract import WikiAddOptions

        assert not hasattr(WikiAddOptions(), "branch"), (
            "WikiAddOptions.branch would let a queued pre-train payload re-create "
            "wiki_page.branch after migration 029"
        )

    def test_wiki_set_metadata_rejects_branch(self):
        from yadgar._shared.wiki.store import WikiStore

        assert "branch" not in WikiStore._METADATA_FIELDS, (
            "wiki_set_metadata(field='branch') is a live MCP write that would "
            "re-create the dropped column"
        )

    def test_memory_update_rejects_branch(self):
        from yadgar._shared.storage.client import _MEMORY_UPDATABLE_FIELDS

        assert "branch" not in _MEMORY_UPDATABLE_FIELDS, (
            "update_memory_fields(branch=...) would re-create the dropped column"
        )


class TestExportSchemaHasNoBranch:
    """The DuckDB export must not project a column that no longer exists."""

    def test_no_branch_column_in_export_schema(self):
        from yadgar.core.export.schema import TABLE_COLUMNS

        offenders = [
            (table, c.duckdb_col)
            for table, cols in TABLE_COLUMNS.items()
            for c in cols
            if c.surreal_field == "branch" or c.duckdb_col == "branch"
        ]
        assert not offenders, f"export schema still projects branch: {offenders}"

    def test_views_sql_has_no_branch_view(self):
        from pathlib import Path

        import yadgar.core.export as _export

        views = (Path(_export.__file__).parent / "views.sql").read_text(encoding="utf-8")
        assert "v_branch_distribution" not in views
        assert "branch" not in views
