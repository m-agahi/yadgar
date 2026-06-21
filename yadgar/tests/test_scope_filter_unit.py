"""Unit tests for ScopeFilter (v6 T6 Step 3a).

Fast, no-DB tests covering:
  - build_clause() string composition and param merging
  - empty-case (both None → empty string)
  - AND-composition (branch + directory clauses ANDed together)
  - Legacy no-op invariant: ScopeFilter(branch=None, directory=None) == ('', {})

These are supplementary to the live-DB e2e tests in tests/e2e/test_scope_filter_e2e.py.
They do NOT gate the feature (the e2e is the gate); they exist for fast feedback.
"""

from __future__ import annotations

import pytest


class TestScopeFilterBuildClause:
    """Unit tests for ScopeFilter.build_clause()."""

    def setup_method(self):
        from yadgar.storage.branch import BranchFilter
        from yadgar.storage.directory import DirectoryFilter
        from yadgar.storage.scope import ScopeFilter

        self.ScopeFilter = ScopeFilter
        self.BranchFilter = BranchFilter
        self.DirectoryFilter = DirectoryFilter

    def test_both_none_returns_empty(self):
        """ScopeFilter(branch=None, directory=None) → ('', {}) — exact legacy no-op."""
        sf = self.ScopeFilter()
        sql, params = sf.build_clause()
        assert sql == ""
        assert params == {}

    def test_branch_only(self):
        """Branch filter alone: produces branch clause."""
        bf = self.BranchFilter(current_branch="feature-x", default_branch="master")
        sf = self.ScopeFilter(branch=bf)
        sql, params = sf.build_clause()
        assert "bf_default" in params
        assert params["bf_default"] == "master"
        assert "bf_current" in params
        assert params["bf_current"] == "feature-x"
        assert "branch" in sql

    def test_directory_only(self):
        """Directory filter alone: produces directory clause."""
        df = self.DirectoryFilter("/home/max/git/yadgar")
        sf = self.ScopeFilter(directory=df)
        sql, params = sf.build_clause()
        assert "df_caller" in params
        assert params["df_caller"] == "/home/max/git/yadgar"
        assert "directory_context" in sql
        assert "global" in sql

    def test_both_clauses_anded(self):
        """Both filters: clauses are ANDed together."""
        bf = self.BranchFilter(current_branch=None, default_branch="master")
        df = self.DirectoryFilter("/home/max/aws-work")
        sf = self.ScopeFilter(branch=bf, directory=df)
        sql, params = sf.build_clause()
        # Both present
        assert "branch" in sql
        assert "directory_context" in sql
        # ANDed
        assert " AND " in sql
        # Params merged
        assert "bf_default" in params
        assert "df_caller" in params
        assert params["df_caller"] == "/home/max/aws-work"

    def test_param_dict_merge_no_collision(self):
        """Branch and directory params don't collide (different key prefixes)."""
        bf = self.BranchFilter(current_branch="feat", default_branch="main")
        df = self.DirectoryFilter("/some/dir")
        sf = self.ScopeFilter(branch=bf, directory=df)
        _, params = sf.build_clause()
        # All 3 params present without collision
        assert set(params.keys()) >= {"bf_default", "bf_current", "df_caller"}

    def test_directory_none_caller_dir_returns_empty(self):
        """DirectoryFilter(caller_dir=None) → directory clause is empty."""
        df = self.DirectoryFilter(None)
        sf = self.ScopeFilter(directory=df)
        sql, params = sf.build_clause()
        # directory clause is empty when caller_dir=None
        assert sql == ""
        assert params == {}

    def test_frozen_dataclass(self):
        """ScopeFilter is frozen (immutable)."""
        import dataclasses

        sf = self.ScopeFilter()
        with pytest.raises(dataclasses.FrozenInstanceError):
            sf.branch = self.BranchFilter(None, "master")  # type: ignore[misc]

    def test_clause_is_string_and_params_dict(self):
        """Return types are (str, dict) regardless of inputs."""
        sf = self.ScopeFilter(directory=self.DirectoryFilter("/p"))
        sql, params = sf.build_clause()
        assert isinstance(sql, str)
        assert isinstance(params, dict)


class TestScopeFilterFromScope:
    """Test ScopeFilter.from_scope() factory."""

    def test_from_scope_with_directory_and_branch(self):
        from yadgar.retrieval.providers.base import Scope
        from yadgar.storage.scope import ScopeFilter

        scope = Scope(directory="/home/max/git/yadgar", branch="master", default_branch="master")
        sf = ScopeFilter.from_scope(scope)
        assert sf.directory is not None
        assert sf.branch is not None

    def test_from_scope_no_branch(self):
        from yadgar.retrieval.providers.base import Scope
        from yadgar.storage.scope import ScopeFilter

        scope = Scope(directory="/home/max/git/yadgar", branch=None, default_branch=None)
        sf = ScopeFilter.from_scope(scope)
        # No branch → branch filter should be None
        assert sf.branch is None
        assert sf.directory is not None

    def test_from_scope_builds_valid_clause(self):
        from yadgar.retrieval.providers.base import Scope
        from yadgar.storage.scope import ScopeFilter

        scope = Scope(directory="/test/dir", branch="feat", default_branch="master")
        sf = ScopeFilter.from_scope(scope)
        sql, params = sf.build_clause()
        assert "directory_context" in sql
        assert "df_caller" in params
        assert params["df_caller"] == "/test/dir"
