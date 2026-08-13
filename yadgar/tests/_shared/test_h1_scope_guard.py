"""Car H1 §1.3 — a falsy project can no longer SILENTLY produce an unscoped read.

Before this car the whole cross-project isolation guarantee rested on one
resolver never returning empty. ``build_project_scope_clause`` answered a falsy
``project_id`` with ``("", {})`` — no WHERE arm, i.e. the entire corpus — and
``is_project_eligible`` answered a falsy caller with ``True`` — every row
admitted. Both arms of the scoping predicate therefore FAILED OPEN, and they
did it quietly: a bug upstream in C5's resolver would not raise, would not log,
and would return MORE results rather than fewer, which reads as the system
working well.

The guard is ADR-0227's rule applied one layer lower: a missing identity fails
loud. What replaces the falsy value is an EXPLICIT ``unscoped=True`` opt-in, so
the two meanings that used to share one representation ("I deliberately want
the whole corpus" vs "I have no project and did not notice") are now different
calls. Only the first is spellable.

WHY THESE TESTS CANNOT GO VACUOUS. Every ``pytest.raises`` here is paired with
a POSITIVE assertion proving the same call site still produces a real clause
for a real project — a guard that rejected everything would pass the negative
half and fail the positive half. The end-to-end test in
``TestNoSilentUnscopedRead`` captures the SQL a storage read actually emits and
asserts the scoped form binds the project, so "the arm is present" is measured
on emitted SQL rather than on the builder's return value.
"""

from __future__ import annotations

import pytest

from yadgar._shared.errors import UnresolvedProjectError
from yadgar._shared.storage.directory import (
    RecallScope,
    build_project_scope_clause,
    build_recall_scope_clause,
    is_project_eligible,
)

_PROJECT = "m-agahi/yadgar"


# ── 1. The SQL arm ───────────────────────────────────────────────────────────


class TestClauseBuilderRefusesAFalsyProject:
    """``("", {})`` on a falsy project is the unscoped read. It must not be free."""

    @pytest.mark.parametrize("falsy", [None, ""])
    def test_falsy_project_raises(self, falsy):
        with pytest.raises(UnresolvedProjectError) as exc:
            build_project_scope_clause(falsy)
        assert exc.value.payload["error"] == "unresolved_project"

    def test_a_real_project_still_builds_the_two_armed_clause(self):
        """The bucket is non-empty: the guard did not simply break the builder."""
        sql, params = build_project_scope_clause(_PROJECT)
        assert "project_id" in sql and " OR " in sql and "IN tags" in sql, sql
        assert _PROJECT in params.values(), params

    def test_explicit_unscoped_is_the_only_way_to_get_no_filtering(self):
        assert build_project_scope_clause(None, unscoped=True) == ("", {})

    def test_unscoped_is_ignored_when_a_project_is_supplied(self):
        """``unscoped=True`` must not WIDEN a call that named a project.

        Otherwise a caller that threads the flag from a config default would
        silently unscope every scoped read it makes.
        """
        sql, params = build_project_scope_clause(_PROJECT, unscoped=True)
        assert "project_id" in sql, sql
        assert _PROJECT in params.values(), params


class TestRecallClauseBuilderRefusesAFalsyProject:
    """The composite builder must not launder the falsy value past the guard."""

    def test_falsy_project_raises(self):
        with pytest.raises(UnresolvedProjectError):
            build_recall_scope_clause(None)

    def test_falsy_project_raises_even_with_the_wiki_only_arms_disabled(self):
        """``page_types=False`` is the MEMORY path — the one with no other arm.

        With the wiki arms off, the project predicate is the ONLY thing in the
        clause, so a falsy project here is an unfiltered corpus-wide memory
        read with nothing else narrowing it.
        """
        with pytest.raises(UnresolvedProjectError):
            build_recall_scope_clause(None, page_types=False)

    def test_a_real_project_still_reaches_the_emitted_clause(self):
        sql, params = build_recall_scope_clause(_PROJECT, page_types=False)
        assert "project_id" in sql, sql
        assert _PROJECT in params.values(), params

    def test_explicit_unscoped_keeps_the_wiki_only_arms(self):
        """An unscoped WIKI read still excludes the policy-hidden page types.

        The project arm is what the caller opted out of; arms 3 and 4 are not
        project-scoped and dropping them here would silently surface page types
        no recall is meant to return.
        """
        sql, _ = build_recall_scope_clause(None, unscoped=True, page_types=True)
        assert "page_type" in sql, (
            f"unscoped must drop the PROJECT arm only, not the policy exclusion: {sql!r}"
        )

    def test_explicit_unscoped_with_no_other_arm_is_empty(self):
        assert build_recall_scope_clause(None, unscoped=True, page_types=False) == ("", {})


class TestRecallScopeCarriesTheOptIn:
    """``RecallScope()`` defaults to project_id=None — the accidental shape."""

    def test_default_scope_clause_raises(self):
        with pytest.raises(UnresolvedProjectError):
            RecallScope().clause()

    def test_explicit_unscoped_scope_does_not_raise(self):
        sql, _ = RecallScope(unscoped=True).clause(page_types=False)
        assert sql == ""

    def test_a_real_scope_still_emits_the_project_arm(self):
        sql, params = RecallScope(project_id=_PROJECT).clause(page_types=False)
        assert "project_id" in sql, sql
        assert _PROJECT in params.values(), params

    def test_with_default_opt_in_preserves_the_unscoped_flag(self):
        """``dataclasses.replace`` must carry the new field across the hop.

        ``with_default_opt_in`` is on the hot path and returns a COPY; a copy
        that dropped ``unscoped`` would turn a deliberate corpus read into a
        raise at the very end of the chain, far from the caller that chose it.
        """
        copied = RecallScope(unscoped=True).with_default_opt_in(["some-tag"])
        assert copied is not RecallScope(unscoped=True), "expected a distinct copy"
        assert copied.unscoped is True
        assert copied.clause(page_types=False) == ("", {})


# ── 2. The residual (row-level) arm ──────────────────────────────────────────


class TestRowGuardRefusesAFalsyCaller:
    """``is_project_eligible`` returning True on a falsy caller admits EVERYTHING.

    This arm covers the graph-walk candidates the SQL clause never sees, so a
    falsy caller here is a whole-corpus leak through the one path that has no
    WHERE clause behind it.
    """

    @pytest.mark.parametrize("falsy", [None, ""])
    def test_falsy_caller_raises(self, falsy):
        with pytest.raises(UnresolvedProjectError):
            is_project_eligible(_PROJECT, ["global"], falsy)

    def test_the_guard_still_admits_and_rejects_for_a_real_caller(self):
        """Bucket proof — both outcomes are still reachable."""
        assert is_project_eligible(_PROJECT, [], _PROJECT) is True
        assert is_project_eligible("m-agahi/other", ["global"], _PROJECT) is True
        assert is_project_eligible("m-agahi/other", ["yadgar"], _PROJECT) is False


# ── 3. End to end: the emitted read ──────────────────────────────────────────


class _CapturingStorage:
    """Minimal ``MemoryStore`` host that records the SQL its reads emit."""

    def __init__(self) -> None:
        self.sql: str = ""
        self.params: dict = {}

    def _q(self, sql, params=None):  # noqa: D102 - test double
        self.sql = sql
        self.params = dict(params or {})
        return []

    def _rows_to_dicts(self, rows):  # noqa: D102 - test double
        return list(rows)


class TestNoSilentUnscopedRead:
    """A falsy project_id can no longer reach the DB as an unfiltered SELECT."""

    @staticmethod
    def _read(storage, **kwargs):
        from yadgar._shared.storage.memory import _MemoryMixin

        return _MemoryMixin.get_memories_by_store_type(storage, "episodic", **kwargs)

    def test_scoped_read_binds_the_project(self):
        """Prove the bucket first: the scoped call DOES emit a project arm."""
        st = _CapturingStorage()
        self._read(st, project_id=_PROJECT)
        assert "project_id" in st.sql, st.sql
        assert _PROJECT in st.params.values(), st.params

    def test_falsy_project_raises_instead_of_emitting_an_unfiltered_select(self):
        st = _CapturingStorage()
        with pytest.raises(UnresolvedProjectError):
            self._read(st, project_id=None)
        assert st.sql == "", (
            f"the guard must fire BEFORE the query is issued; a corpus-wide "
            f"SELECT was emitted: {st.sql!r}"
        )

    def test_deliberate_corpus_read_is_spellable_and_emits_no_project_arm(self):
        st = _CapturingStorage()
        self._read(st, project_id=None, unscoped=True)
        assert st.sql, "expected a query to be emitted for the unscoped read"
        assert "project_id" not in st.sql, st.sql
