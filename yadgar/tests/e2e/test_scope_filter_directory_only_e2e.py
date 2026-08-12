"""E2E for the recall scope clause after Car C7 — RE-POINTED, not deleted.

PROVENANCE, in two steps, because the second step is easy to misread as a deletion:

1. Car 1 of the branch-scoping-removal train deleted
   ``yadgar/tests/e2e/test_scope_filter_e2e.py`` (commit ``7bf28dda``) wholesale
   because two of its four tests were built on the branch axis. That left
   ``ScopeFilter`` with ZERO test references anywhere in the suite, and this file
   was written to restore the directory half.

2. Car C7 (0047 §5 C7) then re-keyed recall scoping from ``directory_context``
   onto ``project_id``, moved it from a Python post-filter INTO the stage-1 SQL
   ``WHERE``, and deleted ``ScopeFilter`` / ``DirectoryFilter`` /
   ``_build_directory_clause`` / ``is_directory_eligible`` outright.

   This file is RE-POINTED onto the replacement (``build_project_scope_clause`` /
   ``build_recall_scope_clause``) rather than removed. Its SUBJECT is unchanged —
   "the scope clause, executed against a live SurrealDB" — and that subject still
   exists; only its key changed. Deleting it would have dropped live-DB coverage
   of the one predicate the whole read path now depends on, at exactly the moment
   that predicate became load-bearing.

WHY THESE LIVE IN ``e2e/`` rather than a unit file — and the reason is STRONGER
after C7, not weaker. Pre-C7 the fragment was documented as "NOT wired into any
SurrealQL query", so only an e2e run could prove it was even executable. Post-C7
it IS the production predicate on every scoped recall: a clause that parses but
selects the wrong set is now a silent correctness failure across the whole read
path. The pure-string assertions live in
``yadgar/tests/_shared/test_c7_recall_scope_clause.py``; what belongs HERE is
executing the fragment against a real DB.

Companion file: ``test_c7_where_clause_equivalence_e2e.py`` carries the
result-set-equivalence and anti-starvation gates.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.e2e

_PROJECT = "m-agahi/yadgar"
_OTHER = "m-agahi/aws-work"


def _seed(storage, marker: str, label: str, project_id: str | None, tags=None) -> None:
    """Insert one memory row tagged with *marker*.

    ``project_id=None`` means the KEY IS OMITTED (a field-absent row). That is
    not the same as storing an explicit null, and the distinction is the whole
    point post-C6: ``project_id`` is ``option<string>``, so a row the backfill
    has not reached reads as ``None`` — NOT as ``"global"``.
    """
    fields = {
        "content": f"{label} {marker}",
        "heat": 1.0,
        "tags": [marker, *(tags or [])],
        "directory_context": "global",
    }
    if project_id is not None:
        fields["project_id"] = project_id
    assignments = ", ".join(f"{k}: ${k}" for k in fields)
    _q_retry(storage, f"INSERT INTO memory {{{assignments}}}", fields)


def _q_retry(storage, surql: str, params: dict, attempts: int = 6):
    """Run *surql*, retrying SurrealKV's own retryable write conflict.

    SurrealKV raises ``Transaction conflict: … This transaction can be retried``
    under concurrent load, and its message says so explicitly. Retrying honours
    that contract: a conflict is a signal about WRITE contention, while every
    assertion in this file is about the READ predicate. A non-conflict error is
    re-raised immediately, so a malformed clause still fails loudly on the first
    attempt — which is the whole reason these tests execute the fragment.
    """
    import time as _time

    for attempt in range(attempts):
        try:
            return storage._q(surql, params)
        except RuntimeError as exc:
            if "onflict" not in str(exc) or attempt == attempts - 1:
                raise
            _time.sleep(0.05 * (attempt + 1))
    return None


def _select_labels(storage, clause: str, params: dict, marker: str) -> list:
    """Run *clause* against the live DB, scoped to rows tagged with *marker*.

    Executing the fragment is the point: a syntactically invalid clause raises
    here rather than passing a string comparison.
    """
    rows = storage._q(
        f"SELECT content FROM memory WHERE tags CONTAINS $tok AND ({clause})",
        {**params, "tok": marker},
    )
    return [(r.get("content") or "").split(" ")[0] for r in rows]


class TestScopeClauseContract:
    """The clause-builder API surface — pure, no DB."""

    def test_empty_scope_is_legacy_noop(self):
        """PORTED from the deleted file's ``test_scope_filter_none_is_legacy_noop``.

        An absent project must produce NO predicate at all, not a predicate that
        matches nothing — the difference between "no filtering requested" and
        "filter everything out".
        """
        from yadgar._shared.storage.directory import build_project_scope_clause

        sql, params = build_project_scope_clause(None)
        assert sql == "", f"An empty scope must produce empty SQL, got: {sql!r}"
        assert params == {}, f"An empty scope must produce empty params, got: {params}"

    def test_empty_string_project_is_also_a_noop(self):
        from yadgar._shared.storage.directory import build_project_scope_clause

        sql, params = build_project_scope_clause("")
        assert sql == ""
        assert params == {}

    def test_populated_scope_names_both_arms(self):
        """Both arms, or the predicate silently narrows the corpus."""
        from yadgar._shared.storage.directory import build_project_scope_clause

        sql, params = build_project_scope_clause(_PROJECT)
        assert "project_id" in sql, f"project arm absent: {sql!r}"
        assert "IN tags" in sql, f"'global' reach-tag arm absent: {sql!r}"
        assert _PROJECT in params.values(), f"project id not bound: {params}"


class TestScopeClauseAgainstLiveDB:
    """The generated fragment is executable SurrealQL with the documented semantics."""

    def test_clause_selects_own_project_and_global_reach_and_excludes_others(self, e2e_engines):
        """Live-DB proof of the post-C7 contract.

        Eligible: rows owned by the caller's project, plus rows carrying the
        ``global`` REACH tag regardless of owner. A row owned by another project
        without that tag must not be selected.
        """
        from yadgar._shared.storage.directory import build_project_scope_clause

        storage = e2e_engines["storage"]
        marker = "scope-proj-case-a"

        _seed(storage, marker, "mine", _PROJECT)
        _seed(storage, marker, "theirs", _OTHER)
        _seed(storage, marker, "reach", _OTHER, tags=["global"])

        clause, params = build_project_scope_clause(_PROJECT)
        assert clause, "a populated project scope must produce a non-empty clause"

        selected = _select_labels(storage, clause, params, marker)

        assert "theirs" not in selected, (
            f"a row owned by {_OTHER!r} must be excluded when scoped to {_PROJECT!r}; "
            f"selected={selected}"
        )
        assert sorted(selected) == ["mine", "reach"], (
            f"expected the caller's own rows plus the 'global'-tagged reach row; "
            f"selected={selected}"
        )

    def test_unstamped_rows_are_excluded_not_treated_as_global(self, e2e_engines):
        """The C7 decision, executed against the real ``option<string>`` column.

        This REPLACES the pre-C7 assertion about ``directory_context IS NONE``
        sentinels. Under the old scheme a field-absent row was ALWAYS eligible
        (``_ALWAYS_ELIGIBLE = {'global', '', None}``). Under C7 it is eligible
        nowhere unless it carries the reach tag — because admitting it would
        rebuild the permissive fallback ADR-0227 exists to delete, and every
        un-backfilled row would leak into every project's recall.

        The cost is bounded and sanctioned: §8 step 5b names ZERO RESULTS as an
        acceptable outcome for the window between boot and the C6 backfill.
        """
        from yadgar._shared.storage.directory import build_project_scope_clause

        storage = e2e_engines["storage"]
        marker = "scope-proj-case-b"

        _seed(storage, marker, "mine", _PROJECT)
        _seed(storage, marker, "unstamped", None)
        _seed(storage, marker, "unstampedreach", None, tags=["global"])

        clause, params = build_project_scope_clause(_PROJECT)
        selected = _select_labels(storage, clause, params, marker)

        assert "unstamped" not in selected, (
            f"an unstamped row surfaced in a project-scoped read — that is the "
            f"permissive fallback ADR-0227 deletes; selected={selected}"
        )
        assert "unstampedreach" in selected, (
            "an unstamped row carrying the 'global' reach tag must still be "
            f"reachable — the reach arm does not depend on project_id; selected={selected}"
        )
        assert "mine" in selected

    def test_composed_recall_clause_is_executable_on_memory_without_page_types(self, e2e_engines):
        """``page_types=False`` is mandatory for memory — it has no such column.

        Emitting the ``page_type`` arm against ``memory`` would raise at query
        time, so this executes the memory-shaped clause to prove the split is
        real rather than documentary.
        """
        from yadgar._shared.storage.directory import build_recall_scope_clause

        storage = e2e_engines["storage"]
        marker = "scope-proj-case-c"

        _seed(storage, marker, "mine", _PROJECT)
        _seed(storage, marker, "theirs", _OTHER)

        clause, params = build_recall_scope_clause(_PROJECT, page_types=False)
        assert "page_type" not in clause, (
            f"the memory-shaped clause must omit the page_type arm: {clause!r}"
        )

        selected = _select_labels(storage, clause, params, marker)
        assert sorted(selected) == ["mine"], f"selected={selected}"

    def test_wiki_shaped_clause_carries_the_page_type_arm(self, e2e_engines):
        """The wiki variant keeps arm 3; it is executable on ``wiki_page``."""
        from yadgar._shared.storage.directory import build_recall_scope_clause

        storage = e2e_engines["storage"]
        clause, params = build_recall_scope_clause(_PROJECT)
        assert "page_type" in clause, f"wiki clause must carry the exclusion arm: {clause!r}"

        # Executes it — an invalid fragment raises rather than silently passing.
        rows = storage._q(f"SELECT slug FROM wiki_page WHERE ({clause})", params)
        assert isinstance(rows, list), "the composed wiki clause must execute cleanly"
