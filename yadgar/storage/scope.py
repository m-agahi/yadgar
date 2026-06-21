"""ScopeFilter — bundled branch + directory predicate for fan-out recall (v6 T6 Step 3).

Replaces the separate branch_filter + directory_filter params in storage methods
for the fan-out path.  The legacy scoring mixin continues to use BranchFilter
directly (unchanged). Only the fan-out path (MemoryProvider, WikiProvider) uses
ScopeFilter.

Design (per plan unified-scoped-recall-v2-steps3-5.md §2):
  - ScopeFilter(branch, directory) bundles both scoping predicates.
  - build_clause() composes _build_branch_clause + _build_directory_clause,
    ANDs the non-empty fragments, merges param dicts.
  - Empty when both are None → ('', {}) — exact legacy no-op.
  - ScopeFilter.from_scope(scope) factory builds from a Scope dataclass.

I30 note: bundling deletes the need for separate branch_filter + directory_filter
params on storage methods, keeping per-method param counts within allowlist limits.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from yadgar.storage.branch import BranchFilter, _build_branch_clause
from yadgar.storage.directory import DirectoryFilter, _build_directory_clause

if TYPE_CHECKING:
    from yadgar.retrieval.providers.base import Scope


@dataclass(frozen=True)
class ScopeFilter:
    """Bundle of branch + directory predicates for DB-level scoping.

    Attributes:
        branch: BranchFilter for branch-aware WHERE predicate, or None (no branch filter).
        directory: DirectoryFilter for directory-aware WHERE predicate, or None (no filter).

    Usage:
        sf = ScopeFilter(branch=BranchFilter(...), directory=DirectoryFilter(...))
        sql_fragment, params = sf.build_clause()
        # sql_fragment is '' when both are None (exact legacy no-op)
    """

    branch: BranchFilter | None = None
    directory: DirectoryFilter | None = None

    def build_clause(self) -> tuple[str, dict]:
        """Return (sql_fragment, params) combining both predicates with AND.

        The fragment is suitable for injection into a WHERE clause as:
          WHERE <existing_conditions> AND <sql_fragment>

        When both filters are None (or produce empty clauses), returns ('', {}).
        The caller must check for '' and omit the AND.

        Returns:
            Tuple of (sql_fragment, params_dict). sql_fragment is '' when no
            filtering is needed.
        """
        branch_clause, branch_params = _build_branch_clause(self.branch)
        dir_clause, dir_params = _build_directory_clause(self.directory)

        parts = [c for c in (branch_clause, dir_clause) if c]
        if not parts:
            return "", {}

        merged_params: dict = {}
        merged_params.update(branch_params)
        merged_params.update(dir_params)

        combined = " AND ".join(parts)
        return combined, merged_params

    @classmethod
    def from_scope(cls, scope: Scope) -> ScopeFilter:
        """Build a ScopeFilter from a provider Scope.

        Constructs BranchFilter and DirectoryFilter from the Scope's
        branch/default_branch/directory fields.

        Args:
            scope: Scope carrying directory, branch, and default_branch.

        Returns:
            ScopeFilter with both predicates populated (or None when fields are absent).
        """
        # Branch filter: only when both branch and default_branch are known
        bf: BranchFilter | None = None
        if scope.default_branch is not None:
            bf = BranchFilter(
                current_branch=scope.branch,
                default_branch=scope.default_branch,
            )
        elif scope.branch is not None:
            # Have current branch but no default — use branch only (no default param)
            bf = BranchFilter(
                current_branch=scope.branch,
                default_branch=scope.branch,  # treat current as default when unknown
            )

        # Directory filter: always present when scope.directory is set
        df: DirectoryFilter | None = None
        if scope.directory:
            df = DirectoryFilter(caller_dir=scope.directory)

        return cls(branch=bf, directory=df)
