"""E2E for ScopeFilter after ADR-0215 — the directory axis, which is all that is left.

Provenance: Car 1 of the branch-scoping-removal train deleted
``yadgar/tests/e2e/test_scope_filter_e2e.py`` (commit ``7bf28dda``) wholesale
because two of its four tests were built on the branch axis. That left
``ScopeFilter`` (``yadgar/_shared/storage/scope.py``) with ZERO test references
anywhere in the suite. This file restores the directory half.

WHAT IS PORTED, and what is not — from the deleted file's four tests:

  * ``test_scope_filter_none_is_legacy_noop`` — PORTED (``test_empty_filter_is_legacy_noop``).
    This was the deleted file's only assertion that touched ``ScopeFilter``
    directly rather than through the recall pipeline, so it is the one that
    left a real hole.

  * ``test_db_clause_includes_field_absent_or_proves_none_exist`` — its DB
    invariant is PORTED (``test_schema_forbids_the_null_and_empty_sentinels_on_memory``).
    The deleted test branched at runtime between two possible outcomes and
    documented whichever the live DB showed; this file asserts the one that is
    actually true today ("option (b)") rather than re-deriving it, and records
    the consequence for the clause's sentinel arms. Its recall-pipeline half is
    dropped as duplicate (see below).

  * ``test_db_clause_excludes_other_dir`` — DROPPED as duplicate. It asserted
    that ``recall(directory=A)`` excludes a memory stamped with directory B.
    ``test_phase1_db_layer.py::TestBCB1::test_excludes_other_project`` asserts
    exactly that (``assert mid_other not in result_ids``), and
    ``TestBCB1::test_global_memory_included`` covers its global-sentinel arm.
    Re-adding it here would test the recall pipeline a third time, not ScopeFilter.

  * ``test_branch_and_directory_compose`` — DROPPED, not portable. It seeded
    rows via ``insert_memory(..., branch="feature-x")`` and asserted a branch
    predicate excluded them. ADR-0215 removed the branch axis from
    ``ScopeFilter`` entirely, so there is no longer a second predicate to
    compose with. Nothing about it survives translation.

The deleted file's ``_run_fanout_recall`` helper also monkeypatched
``_detect_branch`` and ``_get_default_branch``; both functions no longer exist
anywhere in the tree, so a verbatim port could not have run.

WHY THESE LIVE IN ``e2e/`` rather than a unit file: ``_build_directory_clause``
emits a raw SurrealQL fragment that its own docstring notes is "NOT wired into
any SurrealQL query" — no production caller executes it today, and
``ScopeFilter`` has no production caller either (``providers/memory.py`` only
mentions it in a docstring). A mock test asserting the fragment's *string* shape
would therefore prove nothing about whether it is executable. These tests run
the generated fragment against a live SurrealDB, the same rationale
``test_migration_029_drop_branch_column_e2e.py`` gives for testing DDL live.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.e2e


def _seed(storage, marker: str, label: str, directory_context: str | None) -> None:
    """Insert one memory row tagged with *marker*.

    ``directory_context=None`` means the KEY IS OMITTED (a field-absent row),
    which is not the same as storing an explicit null — the distinction the
    ``directory_context IS NONE`` arm of the clause exists to handle.
    """
    if directory_context is None:
        storage._q(
            "INSERT INTO memory {content: $c, heat: 1.0, tags: [$tok]}",
            {"c": f"{label} {marker}", "tok": marker},
        )
    else:
        storage._q(
            "INSERT INTO memory {content: $c, heat: 1.0, tags: [$tok], directory_context: $dc}",
            {"c": f"{label} {marker}", "tok": marker, "dc": directory_context},
        )


def _select_dirs(storage, clause: str, params: dict, marker: str) -> list:
    """Run *clause* against the live DB, scoped to rows tagged with *marker*.

    Returns the ``directory_context`` of every selected row. Executing the
    fragment is the point: a syntactically invalid clause raises here.
    """
    rows = storage._q(
        f"SELECT directory_context FROM memory WHERE tags CONTAINS $tok AND ({clause})",
        {**params, "tok": marker},
    )
    return [r.get("directory_context") for r in rows]


class TestScopeFilterDirectoryContract:
    """The ``ScopeFilter`` API surface — pure, no DB."""

    def test_empty_filter_is_legacy_noop(self):
        """``ScopeFilter()`` → ``('', {})``.

        Ported from the deleted file's ``test_scope_filter_none_is_legacy_noop``.
        Callers branch on the empty string to decide whether to append an AND,
        so an accidental non-empty fragment here would inject a predicate into
        every unscoped query.
        """
        from yadgar._shared.storage.scope import ScopeFilter

        sql, params = ScopeFilter().build_clause()
        assert sql == "", f"Empty ScopeFilter must produce empty SQL, got: {sql!r}"
        assert params == {}, f"Empty ScopeFilter must produce empty params, got: {params}"

    def test_from_scope_populates_the_directory_filter(self):
        """``from_scope`` lifts ``Scope.directory`` into a ``DirectoryFilter``."""
        from yadgar._shared.storage.directory import DirectoryFilter
        from yadgar._shared.storage.scope import ScopeFilter
        from yadgar.backend.retrieval.providers.base import Scope

        sf = ScopeFilter.from_scope(Scope(directory="/home/test/yadgar-project"))

        assert isinstance(sf.directory, DirectoryFilter)
        assert sf.directory.caller_dir == "/home/test/yadgar-project"

    def test_from_scope_binds_the_caller_dir_as_a_param(self):
        """The caller directory is a bound param, never interpolated into SQL.

        If ``$df_caller`` were ever inlined into the fragment, a directory path
        would become SQL text. Assert the path appears in params and NOT in the
        clause string.
        """
        from yadgar._shared.storage.scope import ScopeFilter
        from yadgar.backend.retrieval.providers.base import Scope

        caller_dir = "/home/test/yadgar-project"
        clause, params = ScopeFilter.from_scope(Scope(directory=caller_dir)).build_clause()

        assert params == {"df_caller": caller_dir}
        assert caller_dir not in clause, (
            f"caller_dir must be bound as a param, not interpolated into {clause!r}"
        )

    def test_falsy_scope_directory_yields_the_noop_clause(self):
        """``Scope(directory="")`` → no filter, matching the ``None`` case.

        ``from_scope`` guards on truthiness, so an empty directory must degrade
        to the legacy no-op rather than producing a clause bound to ``''``.
        """
        from yadgar._shared.storage.scope import ScopeFilter
        from yadgar.backend.retrieval.providers.base import Scope

        sf = ScopeFilter.from_scope(Scope(directory=""))

        assert sf.directory is None
        assert sf.build_clause() == ("", {})


class TestScopeFilterClauseAgainstLiveDB:
    """The generated fragment is executable SurrealQL with the documented semantics."""

    def test_clause_selects_caller_dir_and_sentinels_and_excludes_others(self, e2e_engines):
        """Live-DB proof of the sentinel contract.

        ``_ALWAYS_ELIGIBLE`` is ``{'global', '', None}``; the caller directory is
        additionally eligible. A row in another project directory must not be
        selected. This executes the fragment, so an invalid clause fails here
        rather than silently passing a string comparison.
        """
        from yadgar._shared.storage.scope import ScopeFilter
        from yadgar.backend.retrieval.providers.base import Scope

        storage = e2e_engines["storage"]
        caller_dir = e2e_engines["yadgar_dir"]
        other_dir = e2e_engines["other_dir"]
        marker = "scope-dir-case-a"

        _seed(storage, marker, "caller", caller_dir)
        _seed(storage, marker, "other", other_dir)
        _seed(storage, marker, "global", "global")

        clause, params = ScopeFilter.from_scope(Scope(directory=caller_dir)).build_clause()
        assert clause, "a populated ScopeFilter must produce a non-empty clause"

        selected = _select_dirs(storage, clause, params, marker)

        assert other_dir not in selected, (
            f"row stamped {other_dir!r} must be excluded when caller_dir={caller_dir!r}; "
            f"selected={selected}"
        )
        assert sorted(selected) == sorted(["global", caller_dir]), (
            f"expected caller dir + the 'global' sentinel; selected={selected}"
        )

    def test_schema_forbids_the_null_and_empty_sentinels_on_memory(self, e2e_engines):
        """Two of the clause's three sentinel arms are unreachable on ``memory``.

        This is the ported substance of the deleted
        ``test_db_clause_includes_field_absent_or_proves_none_exist``, which
        branched on whether a schema ASSERT rejects a field-absent INSERT and
        documented whichever outcome the live DB exhibited. Run against the
        current schema, the answer is its "option (b)": migration 016 Phase E
        defines

            directory_context ON TABLE memory TYPE string
            ASSERT $value != NONE AND string::len($value) > 0

        so a field-absent row and an empty-string row are BOTH rejected at
        INSERT. (Phase B applies the identical constraint to ``wiki_page``.)

        Consequence, and the reason this is worth asserting: the
        ``directory_context IS NONE`` and ``directory_context = ''`` arms of
        ``_build_directory_clause`` cannot match anything in a post-016 corpus,
        and ``DirectoryFilter._ALWAYS_ELIGIBLE`` (``{'global', '', None}``) is
        correspondingly wider than the DB permits — only ``'global'`` is
        reachable. Both are harmless today, but a future "simplify the clause"
        change needs this recorded, and if a later migration relaxes the
        constraint this test fails and says so.
        """
        storage = e2e_engines["storage"]
        marker = "scope-dir-case-b"

        with pytest.raises(RuntimeError, match="directory_context"):
            _seed(storage, marker, "field-absent", None)

        with pytest.raises(RuntimeError, match="directory_context"):
            _seed(storage, marker, "empty", "")

        remaining = storage._q(
            "SELECT directory_context FROM memory WHERE tags CONTAINS $tok",
            {"tok": marker},
        )
        assert remaining == [], f"neither rejected INSERT may leave a row behind; got {remaining}"

    def test_noop_clause_means_no_filtering(self, e2e_engines):
        """The empty clause is a genuine no-op: every row remains reachable.

        Pairs with ``test_empty_filter_is_legacy_noop`` — that one asserts the
        contract, this one asserts the consequence against real rows.
        """
        storage = e2e_engines["storage"]
        caller_dir = e2e_engines["yadgar_dir"]
        other_dir = e2e_engines["other_dir"]
        marker = "scope-dir-case-c"

        _seed(storage, marker, "caller", caller_dir)
        _seed(storage, marker, "other", other_dir)

        rows = storage._q(
            "SELECT directory_context FROM memory WHERE tags CONTAINS $tok",
            {"tok": marker},
        )
        selected = sorted(r.get("directory_context") for r in rows)

        assert selected == sorted([caller_dir, other_dir]), (
            f"unfiltered select must return both rows; got {selected}"
        )
